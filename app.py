"""Local PostgreSQL table browser with JSON and CSV endpoints for Power BI."""

import csv
import io
import json
import os
from datetime import date, datetime, time
from decimal import Decimal

import psycopg
from psycopg import sql
from psycopg.types.json import Json, Jsonb
from flask import Flask, Response, jsonify, render_template_string, request


app = Flask(__name__)

DB_CONFIG = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": int(os.getenv("PGPORT", "5432")),
    "dbname": os.getenv("PGDATABASE", "K12_Staging_Local_Restore"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
}


def connect_db():
    return psycopg.connect(**DB_CONFIG, connect_timeout=5)


def json_value(value):
    if isinstance(value, (date, datetime, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


def requested_table():
    schema = request.args.get("schema", "").strip()
    table = request.args.get("table", "").strip()
    if not schema or not table:
        return None, None, (jsonify({"error": "Cần truyền schema và table."}), 400)
    return schema, table, None


def table_exists(conn, schema, table):
    row = conn.execute(
        """SELECT EXISTS (
               SELECT 1 FROM information_schema.tables
               WHERE table_schema = %s AND table_name = %s
                 AND table_type IN ('BASE TABLE', 'VIEW', 'FOREIGN')
           )""",
        (schema, table),
    ).fetchone()
    return row[0]


@app.get("/api/tables")
def list_tables():
    search = request.args.get("q", "").strip()
    pattern = f"%{search}%"
    try:
        with connect_db() as conn:
            rows = conn.execute(
                """SELECT n.nspname AS schema, c.relname AS name,
                          CASE c.relkind WHEN 'r' THEN 'table'
                                         WHEN 'p' THEN 'partitioned table'
                                         WHEN 'v' THEN 'view' ELSE c.relkind::text END AS kind,
                          GREATEST(c.reltuples, 0)::bigint AS estimated_rows
                   FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE c.relkind IN ('r', 'p', 'v')
                     AND n.nspname NOT IN ('pg_catalog', 'information_schema')
                     AND (%s = '%%' OR n.nspname ILIKE %s OR c.relname ILIKE %s)
                   ORDER BY n.nspname, c.relname""",
                (pattern, pattern, pattern),
            ).fetchall()
        return jsonify([
            {"schema": r[0], "table": r[1], "kind": r[2], "estimated_rows": r[3]}
            for r in rows
        ])
    except psycopg.Error as exc:
        return jsonify({"error": f"Không kết nối được PostgreSQL: {exc}"}), 503


@app.get("/api/columns")
def list_columns():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        with connect_db() as conn:
            rows = conn.execute(
                """SELECT column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema = %s AND table_name = %s
                   ORDER BY ordinal_position""",
                (schema, table),
            ).fetchall()
        if not rows:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        return jsonify([{"name": r[0], "type": r[1], "nullable": r[2] == "YES"} for r in rows])
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 503


def table_info(conn, schema, table):
    if schema in ("pg_catalog", "information_schema") or schema.startswith("pg_"):
        return None
    relation = conn.execute(
        """SELECT c.oid, c.relkind FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = %s AND c.relname = %s""",
        (schema, table),
    ).fetchone()
    if not relation or relation[1] not in ("r", "p"):
        return None
    pk = conn.execute(
        """SELECT a.attname
           FROM pg_index i
           CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, position)
           JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
           WHERE i.indrelid = %s AND i.indisprimary
             AND k.position <= i.indnkeyatts
           ORDER BY k.position""",
        (relation[0],),
    ).fetchall()
    columns = conn.execute(
        """SELECT column_name, data_type, is_nullable, column_default,
                  is_identity, is_generated
           FROM information_schema.columns
           WHERE table_schema = %s AND table_name = %s
           ORDER BY ordinal_position""",
        (schema, table),
    ).fetchall()
    if not columns:
        return None
    primary_key = [r[0] for r in pk]
    details = []
    for name, data_type, nullable, default, identity, generated in columns:
        can_write = generated == "NEVER" and identity == "NO"
        can_insert = can_write and not (name in primary_key and default is not None)
        details.append({
            "name": name, "type": data_type, "nullable": nullable == "YES",
            "has_default": default is not None, "primary_key": name in primary_key,
            "insertable": can_insert,
            "updatable": can_write and name not in primary_key,
        })
    return {"columns": details, "primary_key": primary_key}


def adapt_input(value, data_type):
    if value == "":
        return None
    if data_type == "jsonb":
        return Jsonb(json.loads(value) if isinstance(value, str) else value)
    if data_type == "json":
        return Json(json.loads(value) if isinstance(value, str) else value)
    if (data_type.endswith("[]") or data_type == "ARRAY") and isinstance(value, str):
        return json.loads(value)
    if data_type == "bytea" and isinstance(value, str):
        return bytes.fromhex(value)
    return value


def body_table_info(body):
    schema = str(body.get("schema", "")).strip()
    table = str(body.get("table", "")).strip()
    values = body.get("values", {})
    if not schema or not table or not isinstance(values, dict):
        return None, None, None, (jsonify({"error": "Thiếu schema, table hoặc values không hợp lệ."}), 400)
    return schema, table, values, None


@app.get("/api/table-info")
def get_table_info():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
        if not info:
            return jsonify({"error": "Không tìm thấy bảng có thể chỉnh sửa."}), 404
        return jsonify(info)
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/row")
def get_row():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        key = json.loads(request.args.get("key", "{}"))
        if not isinstance(key, dict) or not key:
            return jsonify({"error": "Cần truyền khóa chính của bản ghi."}), 400
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info or not info["primary_key"] or set(key) != set(info["primary_key"]):
                return jsonify({"error": "Bảng không có khóa chính phù hợp."}), 400
            where = sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in info["primary_key"]
            )
            query = sql.SQL("SELECT * FROM {}.{} WHERE {} LIMIT 1").format(
                sql.Identifier(schema), sql.Identifier(table), where
            )
            row = conn.execute(query, [key[name] for name in info["primary_key"]]).fetchone()
            cols = conn.execute(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position""",
                (schema, table),
            ).fetchall()
        if row is None:
            return jsonify({"error": "Không tìm thấy bản ghi."}), 404
        return jsonify({name[0]: json_value(value) for name, value in zip(cols, row)})
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/rows")
def insert_row():
    body = request.get_json(silent=True) or {}
    schema, table, values, error = body_table_info(body)
    if error:
        return error
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info:
                return jsonify({"error": "Chỉ có thể ghi vào bảng PostgreSQL thông thường."}), 400
            allowed = {c["name"]: c for c in info["columns"] if c["insertable"] or (c["primary_key"] and c["updatable"])}
            unknown = set(values) - set(allowed)
            if unknown:
                return jsonify({"error": "Cột không thể ghi: " + ", ".join(sorted(unknown))}), 400
            if values:
                names = list(values)
                query = sql.SQL("INSERT INTO {}.{} ({}) VALUES ({}) RETURNING 1").format(
                    sql.Identifier(schema), sql.Identifier(table),
                    sql.SQL(", ").join(map(sql.Identifier, names)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in names),
                )
                params = [adapt_input(values[n], allowed[n]["type"]) for n in names]
            else:
                query = sql.SQL("INSERT INTO {}.{} DEFAULT VALUES RETURNING 1").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
                params = []
            conn.execute(query, params).fetchone()
            conn.commit()
        return jsonify({"message": "Đã thêm bản ghi."}), 201
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.put("/api/rows")
def update_row():
    body = request.get_json(silent=True) or {}
    schema, table, values, error = body_table_info(body)
    if error:
        return error
    key = body.get("key")
    if not isinstance(key, dict) or not key:
        return jsonify({"error": "Thiếu khóa chính của bản ghi."}), 400
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info or not info["primary_key"] or set(key) != set(info["primary_key"]):
                return jsonify({"error": "Bảng cần có khóa chính hợp lệ để cập nhật."}), 400
            allowed = {c["name"]: c for c in info["columns"] if c["updatable"]}
            unknown = set(values) - set(allowed)
            if unknown:
                return jsonify({"error": "Cột không thể cập nhật: " + ", ".join(sorted(unknown))}), 400
            if not values:
                return jsonify({"error": "Chưa có cột nào để cập nhật."}), 400
            set_clause = sql.SQL(", ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in values
            )
            where = sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in info["primary_key"]
            )
            query = sql.SQL("UPDATE {}.{} SET {} WHERE {} RETURNING 1").format(
                sql.Identifier(schema), sql.Identifier(table), set_clause, where
            )
            params = [adapt_input(values[n], allowed[n]["type"]) for n in values]
            params.extend(key[n] for n in info["primary_key"])
            changed = conn.execute(query, params).fetchone()
            conn.commit()
        if not changed:
            return jsonify({"error": "Không tìm thấy bản ghi để cập nhật."}), 404
        return jsonify({"message": "Đã cập nhật bản ghi."})
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.delete("/api/rows")
def delete_row():
    body = request.get_json(silent=True) or {}
    schema = str(body.get("schema", "")).strip()
    table = str(body.get("table", "")).strip()
    key = body.get("key")
    if not schema or not table or not isinstance(key, dict) or not key:
        return jsonify({"error": "Thiếu schema, table hoặc khóa chính."}), 400
    try:
        with connect_db() as conn:
            info = table_info(conn, schema, table)
            if not info or not info["primary_key"] or set(key) != set(info["primary_key"]):
                return jsonify({"error": "Bảng cần có khóa chính hợp lệ để xóa."}), 400
            where = sql.SQL(" AND ").join(
                sql.SQL("{} = %s").format(sql.Identifier(name)) for name in info["primary_key"]
            )
            query = sql.SQL("DELETE FROM {}.{} WHERE {} RETURNING 1").format(
                sql.Identifier(schema), sql.Identifier(table), where
            )
            deleted = conn.execute(query, [key[n] for n in info["primary_key"]]).fetchone()
            conn.commit()
        if not deleted:
            return jsonify({"error": "Không tìm thấy bản ghi để xóa."}), 404
        return jsonify({"message": "Đã xóa bản ghi."})
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 400


def fetch_rows(schema, table, limit, offset):
    with connect_db() as conn:
        if not table_exists(conn, schema, table):
            return None, None
        query = sql.SQL("SELECT * FROM {}.{} LIMIT %s OFFSET %s").format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        with conn.cursor() as cur:
            cur.execute(query, (limit, offset))
            headers = [desc.name for desc in cur.description]
            rows = cur.fetchall()
        return headers, rows


@app.get("/api/data")
def table_data():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        limit = min(max(int(request.args.get("limit", "100")), 1), 1000)
        offset = max(int(request.args.get("offset", "0")), 0)
        headers, rows = fetch_rows(schema, table, limit, offset)
        if headers is None:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        return jsonify({
            "schema": schema,
            "table": table,
            "limit": limit,
            "offset": offset,
            "columns": headers,
            "rows": [[json_value(v) for v in row] for row in rows],
        })
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/powerbi")
def powerbi_data():
    """Return records as a JSON array that Power BI's Web connector can import."""
    schema, table, error = requested_table()
    if error:
        return error
    try:
        limit = min(max(int(request.args.get("limit", "1000")), 1), 100000)
        offset = max(int(request.args.get("offset", "0")), 0)
        headers, rows = fetch_rows(schema, table, limit, offset)
        if headers is None:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        return jsonify([
            {name: json_value(value) for name, value in zip(headers, row)}
            for row in rows
        ])
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/powerbi/tuition-monthly")
def powerbi_tuition_monthly():
    """Return school-month tuition aggregates without student identifiers."""
    conditions = []
    params = []
    try:
        start_value = request.args.get("from", "").strip()
        end_value = request.args.get("to", "").strip()
        if start_value:
            conditions.append(sql.SQL("m.start_date::date >= %s"))
            params.append(date.fromisoformat(start_value))
        if end_value:
            conditions.append(sql.SQL("m.start_date::date <= %s"))
            params.append(date.fromisoformat(end_value))
        school_value = request.args.get("school_id", "").strip()
        if school_value:
            conditions.append(sql.SQL("d.school_id = %s"))
            params.append(int(school_value))

        query = sql.SQL(
            """SELECT d.school_id, s.name AS school_name,
                      m.id AS month_id, m.name AS month_name,
                      m.start_date::date AS month_start, m.end_date::date AS month_end,
                      m.school_term_id, t.name AS term_name,
                      t.school_year_id, y.name AS school_year,
                      count(DISTINCT d.user_code) AS student_count,
                                            count(DISTINCT d.user_code)
                                                FILTER (WHERE d.last_debt_balance > 0) AS students_with_closing_debt,
                      COALESCE(sum(d.last_period_debt_balance), 0) AS opening_debt_amount,
                      COALESCE(sum(d.total_net_payable), 0) AS net_payable_amount,
                      COALESCE(sum(d.total_payment), 0) AS payment_amount,
                      COALESCE(sum(d.total_refund), 0) AS refund_amount,
                      COALESCE(sum(d.total_adjustment_additional), 0) AS additional_adjustment_amount,
                      COALESCE(sum(d.total_adjustment_refund), 0) AS refund_adjustment_amount,
                      COALESCE(sum(d.last_debt_balance), 0) AS closing_debt_amount
               FROM public.tuition_fee_debt_record_by_month d
               JOIN public.school_month m
                 ON m.id = d.month_id AND m.school_id = d.school_id
               LEFT JOIN public.school s
                 ON s.id = d.school_id AND s.deleted_at IS NULL
               LEFT JOIN public.school_term t
                 ON t.id = m.school_term_id AND t.school_id = m.school_id
                    AND t.deleted_at IS NULL
               LEFT JOIN public.school_year y
                 ON y.id = t.school_year_id AND y.school_id = m.school_id
                    AND y.deleted_at IS NULL
               WHERE d.deleted_at IS NULL AND m.deleted_at IS NULL
                 AND {filters}
               GROUP BY d.school_id, s.name, m.id, m.name, m.start_date,
                        m.end_date, m.school_term_id, t.name,
                        t.school_year_id, y.name
               ORDER BY m.start_date, d.school_id"""
        ).format(filters=sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("TRUE"))

        with connect_db() as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                headers = [description.name for description in cur.description]
                rows = cur.fetchall()
        return jsonify([
            {
                name: float(value) if isinstance(value, Decimal) else json_value(value)
                for name, value in zip(headers, row)
            }
            for row in rows
        ])
    except ValueError as exc:
        return jsonify({"error": f"Tham số không hợp lệ: {exc}"}), 400
    except psycopg.Error as exc:
        return jsonify({"error": f"Không truy vấn được dữ liệu học phí: {exc}"}), 503


@app.get("/dashboard")
def dashboard_home():
    return render_template_string(DASHBOARD_HOME)


@app.get("/dashboard/ban-giam-hieu")
def principal_tuition_dashboard():
    return render_template_string(DASHBOARD)


@app.get("/api/accounting/dashboard")
def accounting_dashboard_data():
    dataset = request.args.get("dataset", "all").strip().lower()
    if dataset not in {"all", "transactions", "refunds"}:
        return jsonify({"error": "dataset phải là all, transactions hoặc refunds."}), 400
    try:
        with connect_db() as conn:
            years = conn.execute(
                """SELECT y.name, MAX(y.start_at) AS year_start
                   FROM tuition.tuition_fee_transaction tx
                   LEFT JOIN public.school_month m
                     ON m.id = tx.month_id AND m.school_id = tx.school_id
                   JOIN public.school_term t
                     ON t.id = COALESCE(tx.school_term_id, m.school_term_id)
                    AND t.school_id = tx.school_id AND t.deleted_at IS NULL
                   JOIN public.school_year y
                     ON y.id = t.school_year_id AND y.school_id = t.school_id
                    AND y.deleted_at IS NULL
                   WHERE tx.deleted_at IS NULL
                   GROUP BY y.name
                   ORDER BY MAX(y.start_at) DESC
                   LIMIT 3"""
            ).fetchall()
            year_names = [row[0] for row in years]
            if not year_names:
                return jsonify({"error": "Không tìm thấy giao dịch học phí thuộc năm học."}), 404

            transactions = conn.execute(
                """SELECT tx.accounting_date, y.name AS school_year,
                          t.name AS term_name, m.name AS month_name,
                          s.name AS school_name,
                          COALESCE(pm.name, 'Chưa xác định') AS payment_method,
                          tx.total_debit, tx.total_credit,
                          tx.is_valid_transaction, tx.is_scanned,
                          tx.status_confirm, tx.accounting_number,
                          tx.transaction_ref_no
                   FROM tuition.tuition_fee_transaction tx
                   LEFT JOIN public.school_month m
                     ON m.id = tx.month_id AND m.school_id = tx.school_id
                   JOIN public.school_term t
                     ON t.id = COALESCE(tx.school_term_id, m.school_term_id)
                    AND t.school_id = tx.school_id AND t.deleted_at IS NULL
                   JOIN public.school_year y
                     ON y.id = t.school_year_id AND y.school_id = t.school_id
                    AND y.deleted_at IS NULL
                   LEFT JOIN public.school s
                     ON s.id = tx.school_id AND s.deleted_at IS NULL
                   LEFT JOIN public.tuition_fee_payment_method pm
                     ON pm.id = tx.payment_method_id AND pm.deleted_at IS NULL
                   WHERE tx.deleted_at IS NULL AND y.name = ANY(%s)
                   ORDER BY tx.accounting_date DESC, tx.id DESC""",
                (year_names,),
            ).fetchall()
            transaction_fields = [
                "accounting_date", "school_year", "term_name", "month_name",
                "school_name", "payment_method", "total_debit", "total_credit",
                "is_valid_transaction", "is_scanned", "status_confirm",
                "accounting_number", "transaction_ref_no",
            ]

            refunds = conn.execute(
                """SELECT r.date AS refund_date, y.name AS school_year,
                          s.name AS school_name, c.name AS class_name,
                          u.full_name AS student_name,
                          COALESCE(pc.description, pc.code, 'Chưa xác định') AS project_name,
                          r.refund_amount, d.total_days_not_used,
                          d.total_service_fee_per_day
                   FROM tuition.tuition_report_service_detail_refund r
                   JOIN tuition.tuition_report_service_detail_by_month d
                     ON d.id = r.report_detail_by_month_id AND d.deleted_at IS NULL
                   JOIN tuition.tuition_report_service_config_by_month cm
                     ON cm.id = d.config_by_month_id AND cm.deleted_at IS NULL
                   JOIN tuition.tuition_report_service_config_by_year cy
                     ON cy.id = cm.config_by_year_id AND cy.deleted_at IS NULL
                   JOIN public.school_year y
                     ON y.id = cy.school_year_id AND y.school_id = cy.school_id
                    AND y.deleted_at IS NULL
                   LEFT JOIN public.school s
                     ON s.id = r.school_id AND s.deleted_at IS NULL
                   LEFT JOIN public.tuition_fee_project_code pc
                     ON pc.id = cy.project_id AND pc.deleted_at IS NULL
                   LEFT JOIN public.tuition_fee_debt_record dr
                     ON dr.id = d.debt_record_id AND dr.deleted_at IS NULL
                   LEFT JOIN public.profile_student ps
                     ON ps.user_code = dr.user_code AND ps.deleted_at IS NULL
                   LEFT JOIN public.users u
                     ON u.id = ps.student_id AND u.deleted_at IS NULL
                   LEFT JOIN LATERAL (
                       SELECT cl.name
                       FROM public.classroom_student cs
                       JOIN public.classroom cl
                         ON cl.id = cs.classroom_id AND cl.deleted_at IS NULL
                       WHERE cs.student_id = u.id AND cs.deleted_at IS NULL
                         AND cl.school_year_id = cy.school_year_id
                       ORDER BY cs.id DESC
                       LIMIT 1
                   ) c ON TRUE
                   WHERE r.deleted_at IS NULL AND y.name = ANY(%s)
                   ORDER BY r.date DESC, r.id DESC
                   LIMIT 10000""",
                (year_names,),
            ).fetchall()
            refund_fields = [
                "refund_date", "school_year", "school_name", "class_name",
                "student_name", "project_name", "refund_amount",
                "total_days_not_used", "total_service_fee_per_day",
            ]

        def records(fields, rows):
            return [
                {name: json_value(value) for name, value in zip(fields, row)}
                for row in rows
            ]

        transaction_data = records(transaction_fields, transactions)
        refund_data = records(refund_fields, refunds)
        if dataset == "transactions":
            return jsonify(transaction_data)
        if dataset == "refunds":
            return jsonify(refund_data)
        return jsonify({
            "years": year_names,
            "transactions": transaction_data,
            "refunds": refund_data,
        })
    except psycopg.Error as exc:
        return jsonify({"error": f"Không truy vấn được dữ liệu kế toán học phí: {exc}"}), 503


@app.get("/dashboard/truong-phong-ke-toan")
def accounting_dashboard():
    return render_template_string(ACCOUNTING_DASHBOARD)


@app.get("/api/accounting/clerk")
def clerk_dashboard_data():
    dataset = request.args.get("dataset", "meta").strip().lower()
    if dataset not in {"meta", "transactions", "refunds"}:
        return jsonify({"error": "dataset phải là meta, transactions hoặc refunds."}), 400

    try:
        start_value = request.args.get("from", "").strip()
        end_value = request.args.get("to", "").strip()
        start_date = date.fromisoformat(start_value) if start_value else None
        end_date = date.fromisoformat(end_value) if end_value else None
        if start_date and end_date and start_date > end_date:
            return jsonify({"error": "Ngày bắt đầu không được sau ngày kết thúc."}), 400
        search = request.args.get("q", "").strip()
        if len(search) > 100:
            return jsonify({"error": "Từ khóa tìm kiếm tối đa 100 ký tự."}), 400
        pattern = f"%{search}%"

        with connect_db() as conn:
            years = conn.execute(
                """SELECT y.id, y.name, MAX(y.start_at) AS year_start
                   FROM tuition.tuition_fee_transaction tx
                   LEFT JOIN public.school_month m
                     ON m.id = tx.month_id AND m.school_id = tx.school_id
                   JOIN public.school_term t
                     ON t.id = COALESCE(tx.school_term_id, m.school_term_id)
                    AND t.school_id = tx.school_id AND t.deleted_at IS NULL
                   JOIN public.school_year y
                     ON y.id = t.school_year_id AND y.school_id = t.school_id
                    AND y.deleted_at IS NULL
                   WHERE tx.deleted_at IS NULL
                   GROUP BY y.id, y.name
                   ORDER BY MAX(y.start_at) DESC
                   LIMIT 3"""
            ).fetchall()
            if not years:
                return jsonify({"error": "Không tìm thấy giao dịch học phí thuộc năm học."}), 404

            year_ids = [row[0] for row in years]
            year_names = [row[1] for row in years]
            date_bounds = conn.execute(
                """SELECT MIN(tx.accounting_date::date),
                          LEAST(CURRENT_DATE, MAX(tx.accounting_date::date))
                   FROM tuition.tuition_fee_transaction tx
                   JOIN public.school_month m
                     ON m.id = tx.month_id AND m.school_id = tx.school_id
                   JOIN public.school_term t
                     ON t.id = COALESCE(tx.school_term_id, m.school_term_id)
                    AND t.school_id = tx.school_id AND t.deleted_at IS NULL
                   WHERE tx.deleted_at IS NULL AND t.school_year_id = ANY(%s)
                     AND tx.accounting_date::date <= CURRENT_DATE""",
                (year_ids,),
            ).fetchone()
            default_from = date_bounds[0]
            default_to = date_bounds[1] or date.today()
            start_date = start_date or default_from
            end_date = end_date or default_to
            if start_date and start_date > end_date:
                return jsonify({"error": "Khoảng ngày chọn không có thứ tự hợp lệ."}), 400

            if dataset == "meta":
                return jsonify({
                    "years": year_names,
                    "from": start_date.isoformat() if start_date else "",
                    "to": end_date.isoformat() if end_date else "",
                })

            if dataset == "transactions":
                query = """SELECT tx.accounting_date, y.name AS school_year,
                                  t.name AS term_name, m.name AS month_name,
                                  s.name AS school_name,
                                  COALESCE(ps.user_code, '') AS student_code,
                                  COALESCE(u.full_name, '') AS student_name,
                                  tx.total_debit, tx.total_credit,
                                  COALESCE(pm.name, 'Chưa xác định') AS payment_method,
                                  tx.accounting_number, tx.transaction_ref_no,
                                  tx.transaction_content, tx.is_valid_transaction,
                                  tx.is_scanned
                           FROM tuition.tuition_fee_transaction tx
                           LEFT JOIN public.school_month m
                             ON m.id = tx.month_id AND m.school_id = tx.school_id
                           JOIN public.school_term t
                             ON t.id = COALESCE(tx.school_term_id, m.school_term_id)
                            AND t.school_id = tx.school_id AND t.deleted_at IS NULL
                           JOIN public.school_year y
                             ON y.id = t.school_year_id AND y.school_id = t.school_id
                            AND y.deleted_at IS NULL
                           LEFT JOIN public.school s
                             ON s.id = tx.school_id AND s.deleted_at IS NULL
                           LEFT JOIN public.users u
                             ON u.id = tx.student_id AND u.deleted_at IS NULL
                           LEFT JOIN LATERAL (
                               SELECT p.user_code
                               FROM public.profile_student p
                               WHERE p.student_id = tx.student_id
                                 AND p.deleted_at IS NULL
                                 AND (p.school_id = tx.school_id OR p.school_id IS NULL)
                               ORDER BY (p.school_id = tx.school_id) DESC,
                                        p.updated_at DESC NULLS LAST, p.id DESC
                               LIMIT 1
                           ) ps ON TRUE
                           LEFT JOIN public.tuition_fee_payment_method pm
                             ON pm.id = tx.payment_method_id AND pm.deleted_at IS NULL
                           WHERE tx.deleted_at IS NULL
                             AND y.id = ANY(%s)
                             AND tx.accounting_date::date >= %s
                             AND tx.accounting_date::date <= %s
                             AND (%s = ''
                                  OR COALESCE(ps.user_code, '') ILIKE %s
                                  OR COALESCE(u.full_name, '') ILIKE %s
                                  OR tx.accounting_number ILIKE %s
                                  OR COALESCE(tx.transaction_ref_no, '') ILIKE %s)
                           ORDER BY tx.accounting_date DESC, tx.id DESC"""
                fields = [
                    "accounting_date", "school_year", "term_name", "month_name",
                    "school_name", "student_code", "student_name", "total_debit",
                    "total_credit", "payment_method", "accounting_number",
                    "transaction_ref_no", "transaction_content",
                    "is_valid_transaction", "is_scanned",
                ]
                rows = conn.execute(
                    query,
                    (year_ids, start_date, end_date, search, pattern, pattern, pattern, pattern),
                ).fetchall()
            else:
                query = """SELECT r.date AS refund_date, y.name AS school_year,
                                  cm.month_id, sm.name AS month_name,
                                  s.name AS school_name, c.class_name,
                                  COALESCE(dr.user_code, '') AS student_code,
                                  COALESCE(u.full_name, '') AS student_name,
                                  COALESCE(pc.description, pc.code, 'Chưa xác định') AS project_name,
                                  r.refund_amount, d.total_days_not_used,
                                  d.total_service_fee_per_day
                           FROM tuition.tuition_report_service_detail_refund r
                           JOIN tuition.tuition_report_service_detail_by_month d
                             ON d.id = r.report_detail_by_month_id AND d.deleted_at IS NULL
                           JOIN tuition.tuition_report_service_config_by_month cm
                             ON cm.id = d.config_by_month_id AND cm.deleted_at IS NULL
                           JOIN tuition.tuition_report_service_config_by_year cy
                             ON cy.id = cm.config_by_year_id AND cy.deleted_at IS NULL
                           JOIN public.school_year y
                             ON y.id = cy.school_year_id AND y.school_id = cy.school_id
                            AND y.deleted_at IS NULL
                           LEFT JOIN public.school_month sm
                             ON sm.id = cm.month_id AND sm.school_id = cm.school_id
                           LEFT JOIN public.school s
                             ON s.id = r.school_id AND s.deleted_at IS NULL
                           LEFT JOIN public.tuition_fee_project_code pc
                             ON pc.id = cy.project_id AND pc.deleted_at IS NULL
                           LEFT JOIN public.tuition_fee_debt_record dr
                             ON dr.id = d.debt_record_id AND dr.deleted_at IS NULL
                           LEFT JOIN public.profile_student ps
                             ON ps.user_code = dr.user_code AND ps.deleted_at IS NULL
                           LEFT JOIN public.users u
                             ON u.id = ps.student_id AND u.deleted_at IS NULL
                           LEFT JOIN LATERAL (
                               SELECT cl.name AS class_name
                               FROM public.classroom_student cs
                               JOIN public.classroom cl
                                 ON cl.id = cs.classroom_id AND cl.deleted_at IS NULL
                               WHERE cs.student_id = u.id AND cs.deleted_at IS NULL
                                 AND cl.school_year_id = cy.school_year_id
                               ORDER BY cs.id DESC
                               LIMIT 1
                           ) c ON TRUE
                           WHERE r.deleted_at IS NULL AND y.id = ANY(%s)
                             AND r.date::date >= %s AND r.date::date <= %s
                             AND (%s = ''
                                  OR COALESCE(dr.user_code, '') ILIKE %s
                                  OR COALESCE(u.full_name, '') ILIKE %s
                                  OR COALESCE(c.class_name, '') ILIKE %s
                                  OR COALESCE(pc.description, pc.code, '') ILIKE %s)
                           ORDER BY r.date DESC, r.id DESC"""
                fields = [
                    "refund_date", "school_year", "month_id", "month_name",
                    "school_name", "class_name", "student_code", "student_name",
                    "project_name", "refund_amount", "total_days_not_used",
                    "total_service_fee_per_day",
                ]
                rows = conn.execute(
                    query,
                    (year_ids, start_date, end_date, search, pattern, pattern, pattern, pattern),
                ).fetchall()

        return jsonify([
            {name: json_value(value) for name, value in zip(fields, row)}
            for row in rows
        ])
    except ValueError as exc:
        return jsonify({"error": f"Tham số không hợp lệ: {exc}"}), 400
    except psycopg.Error as exc:
        return jsonify({"error": f"Không truy vấn được dữ liệu kế toán viên: {exc}"}), 503


@app.get("/dashboard/ke-toan-vien")
def clerk_dashboard():
    return render_template_string(CLERK_DASHBOARD)


@app.get("/export.csv")
def export_csv():
    schema, table, error = requested_table()
    if error:
        return error
    try:
        limit = min(max(int(request.args.get("limit", "100000")), 1), 100000)
        offset = max(int(request.args.get("offset", "0")), 0)
        headers, rows = fetch_rows(schema, table, limit, offset)
        if headers is None:
            return jsonify({"error": "Không tìm thấy bảng."}), 404
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([json_value(v) for v in row])
        filename = f"{schema}_{table}.csv"
        return Response(
            "\ufeff" + output.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except (ValueError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


CLERK_DASHBOARD = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tra cứu học phí | Kế toán viên</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}*{box-sizing:border-box}
body{margin:0}header{background:#172554;color:white;padding:24px max(24px,calc((100vw - 1440px)/2))}
header h1{margin:0 0 5px;font-size:25px}header p{margin:0;color:#dbeafe}
.home-link{display:inline-block;color:#dbeafe;font-size:13px;margin-bottom:10px;text-decoration:none}
main{max-width:1440px;margin:22px auto;padding:0 20px}
.toolbar{display:flex;align-items:end;gap:12px;flex-wrap:wrap;padding:14px;background:white;border:1px solid #e1e7f0;border-radius:10px;margin-bottom:16px}
.toolbar label{font-size:13px;font-weight:600;color:#475569}.toolbar input{display:block;margin-top:5px;padding:9px 10px;min-width:170px;border:1px solid #cbd5e1;border-radius:7px;font:inherit}
button{font:inherit}.toolbar button,.pager button{padding:9px 13px;border:0;border-radius:6px;background:#2563eb;color:#fff;cursor:pointer}.toolbar button:disabled,.pager button:disabled{opacity:.45;cursor:default}
.status{display:none;padding:14px;background:#fff7ed;color:#9a3412;border-radius:8px;margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}.card,.panel{background:white;border:1px solid #e1e7f0;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.card{padding:15px}.card span{display:block;color:#64748b;font-size:13px}.card strong{display:block;margin-top:7px;font-size:20px;overflow-wrap:anywhere}
.panel{padding:16px;margin-bottom:16px}.panel h2{font-size:17px;margin:0 0 4px}.panel>p{margin:0 0 10px;color:#64748b;font-size:12px}
.chart svg{display:block;width:100%;height:auto;min-height:230px}.chart svg [data-clickable="true"]{cursor:pointer}.chart svg [data-clickable="true"]:hover{filter:brightness(.88)}
.table-scroll{overflow:auto;max-height:480px;border:1px solid #e2e8f0;border-radius:6px}
table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{padding:8px 10px;border-bottom:1px solid #e8edf4;text-align:left}th{background:#eaf0f8;position:sticky;top:0;z-index:1}td.num{text-align:right;font-variant-numeric:tabular-nums}
.pager{display:flex;align-items:center;justify-content:flex-end;gap:10px;margin-top:10px;color:#64748b;font-size:13px}
.note{color:#64748b;font-size:12px;margin-top:9px}.empty{text-align:center;padding:32px;color:#64748b}
footer{padding:8px 0 28px;color:#64748b;font-size:12px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:520px){header{padding:20px}main{padding:0 12px}.cards{grid-template-columns:1fr 1fr;gap:8px}.card{padding:11px}.card strong{font-size:17px}.toolbar input{min-width:140px;width:100%}}
</style></head>
<body>
<header><a class="home-link" href="/dashboard">← Chọn đối tượng</a><h1>Tra cứu và đối soát học phí</h1><p>Kế toán viên · Giao dịch theo học sinh, ngày hạch toán và chi tiết hoàn phí</p></header>
<main>
 <form class="toolbar" id="filters">
  <label>Từ ngày<input id="fromDate" type="date"></label>
  <label>Đến ngày<input id="toDate" type="date"></label>
  <label>Mã hoặc tên học sinh<input id="studentSearch" type="search" maxlength="100" placeholder="Nhập mã hoặc tên học sinh"></label>
  <button id="searchButton" type="submit">Tra cứu</button>
 </form>
 <div id="status" class="status" role="alert"></div>
 <section class="cards" aria-label="Tổng quan kết quả tra cứu">
  <div class="card"><span>Số giao dịch</span><strong id="txCount">—</strong></div>
  <div class="card"><span>Số học sinh có giao dịch</span><strong id="studentCount">—</strong></div>
  <div class="card"><span>Tổng ghi nợ</span><strong id="debitTotal">—</strong></div>
  <div class="card"><span>Tổng ghi có</span><strong id="creditTotal">—</strong></div>
 </section>
 <section class="panel chart"><h2>Giao dịch theo ngày hạch toán</h2><p>Bấm cột để lọc danh sách giao dịch trong ngày đó. Ngày dùng theo ngày hạch toán trong dữ liệu.</p><div id="dailyChart"></div></section>
 <section class="panel"><h2>Chi tiết giao dịch theo học sinh</h2><p>Tra cứu bằng mã/tên học sinh; gồm ngày, kỳ/tháng, ghi nợ/ghi có, phương thức và mã đối chiếu.</p>
  <div class="table-scroll"><table><thead><tr><th>Ngày hạch toán</th><th>Năm học</th><th>Học kỳ</th><th>Tháng</th><th>Cơ sở</th><th>Mã học sinh</th><th>Tên học sinh</th><th>Ghi nợ</th><th>Ghi có</th><th>Phương thức</th><th>Số hạch toán</th><th>Mã tham chiếu</th><th>Hợp lệ</th><th>Đã quét</th></tr></thead><tbody id="transactions"></tbody></table></div>
  <div class="pager"><button id="txPrev" type="button">Trước</button><span id="txPage"></span><button id="txNext" type="button">Sau</button></div>
  <p class="note" id="txNote"></p>
 </section>
 <section class="panel"><h2>Chi tiết các khoản hoàn phí</h2><p>Tra cứu theo khoảng ngày và học sinh; gồm lớp, cơ sở, tháng, dự án dịch vụ và ngày chưa sử dụng.</p>
  <div class="table-scroll"><table><thead><tr><th>Ngày hoàn</th><th>Năm học</th><th>Tháng dịch vụ</th><th>Cơ sở</th><th>Mã học sinh</th><th>Tên học sinh</th><th>Lớp</th><th>Dự án dịch vụ</th><th>Ngày chưa sử dụng</th><th>Phí/ngày</th><th>Số tiền hoàn</th></tr></thead><tbody id="refunds"></tbody></table></div>
  <div class="pager"><button id="refundPrev" type="button">Trước</button><span id="refundPage"></span><button id="refundNext" type="button">Sau</button></div>
  <p class="note" id="refundNote"></p>
 </section>
 <footer>Dữ liệu giới hạn trong ba năm học gần nhất. Tên và mã học sinh là dữ liệu cá nhân, chỉ sử dụng cho nghiệp vụ nội bộ và không chia sẻ báo cáo ra ngoài phạm vi được phép.</footer>
</main>
<script>
const API="/api/accounting/clerk",pageSize=100,svgNS="http://www.w3.org/2000/svg";
const money=new Intl.NumberFormat("vi-VN",{maximumFractionDigits:0}),shortMoney=new Intl.NumberFormat("vi-VN",{notation:"compact",maximumFractionDigits:1});
let transactions=[],refunds=[],txPage=0,refundPage=0;
function total(rows,key){return rows.reduce((n,r)=>n+(Number(r[key])||0),0)}
function td(row,value,cls){const cell=document.createElement("td");cell.textContent=value===null||value===undefined||value===""?"—":String(value);if(cls)cell.className=cls;row.appendChild(cell)}
function addCell(row,value,format){let text=value;if(format==="money")text=money.format(Number(value)||0)+" ₫";else if(format==="date")text=value?String(value).slice(0,10):"";else if(format==="bool")text=value?"Có":"Không";td(row,text,format==="money"?"num":"")}
function renderTable(bodyId,rows,columns,page){const body=document.getElementById(bodyId);body.replaceChildren();rows.slice(page*pageSize,(page+1)*pageSize).forEach(record=>{const row=document.createElement("tr");columns.forEach(([key,format])=>addCell(row,record[key],format));body.appendChild(row)})}
function groupByDate(rows){const map=new Map();rows.forEach(r=>{const key=String(r.accounting_date||"").slice(0,10);if(!key)return;if(!map.has(key))map.set(key,{date:key,debit:0,credit:0,count:0});const g=map.get(key);g.debit+=Number(r.total_debit)||0;g.credit+=Number(r.total_credit)||0;g.count++});return [...map.values()].sort((a,b)=>a.date.localeCompare(b.date))}
function drawDailyChart(days){
 const target=document.getElementById("dailyChart");target.replaceChildren();if(!days.length){const empty=document.createElement("div");empty.className="empty";empty.textContent="Không có giao dịch trong khoảng lọc.";target.appendChild(empty);return}
 const svg=document.createElementNS(svgNS,"svg");svg.setAttribute("viewBox","0 0 960 300");svg.setAttribute("role","img");target.appendChild(svg);
 const W=960,H=300,L=80,R=20,T=24,B=56,pw=W-L-R,ph=H-T-B,max=Math.max(1,...days.flatMap(d=>[d.debit,d.credit]));
 for(let n=0;n<=4;n++){const y=T+ph*n/4;const line=document.createElementNS(svgNS,"line");line.setAttribute("x1",L);line.setAttribute("x2",W-R);line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("stroke","#e8edf4");svg.appendChild(line);const label=document.createElementNS(svgNS,"text");label.setAttribute("x",L-8);label.setAttribute("y",y+4);label.setAttribute("text-anchor","end");label.setAttribute("fill","#64748b");label.setAttribute("font-size","12");label.textContent=shortMoney.format(max*(4-n)/4);svg.appendChild(label)}
 const slot=pw/days.length,groupWidth=Math.min(slot*.72,76),barWidth=groupWidth/2;
 days.forEach((d,i)=>{[[d.debit,"#2563eb","Ghi nợ"],[d.credit,"#14b8a6","Ghi có"]].forEach((item,j)=>{const h=Math.max(1,item[0]/max*ph),x=L+i*slot+(slot-groupWidth)/2+j*barWidth,y=T+ph-h;const rect=document.createElementNS(svgNS,"rect");rect.setAttribute("x",x);rect.setAttribute("y",y);rect.setAttribute("width",Math.max(2,barWidth-4));rect.setAttribute("height",h);rect.setAttribute("rx",3);rect.setAttribute("fill",item[1]);rect.setAttribute("data-clickable","true");rect.setAttribute("tabindex","0");rect.setAttribute("role","button");const title=document.createElementNS(svgNS,"title");title.textContent=`${d.date} · ${item[2]}: ${money.format(item[0])} ₫ · ${d.count} giao dịch · Bấm để xem`;rect.appendChild(title);const selectDate=()=>{document.getElementById("fromDate").value=d.date;document.getElementById("toDate").value=d.date;document.getElementById("filters").requestSubmit()};rect.addEventListener("click",selectDate);rect.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();selectDate()}});svg.appendChild(rect)});if(i%Math.max(1,Math.ceil(days.length/12))===0||i===days.length-1){const label=document.createElementNS(svgNS,"text");label.setAttribute("x",L+i*slot+slot/2);label.setAttribute("y",H-B+20);label.setAttribute("text-anchor","middle");label.setAttribute("fill","#475569");label.setAttribute("font-size","11");label.textContent=d.date;svg.appendChild(label)}});
 [["#2563eb","Ghi nợ"],["#14b8a6","Ghi có"]].forEach((item,i)=>{const x=L+i*110,box=document.createElementNS(svgNS,"rect");box.setAttribute("x",x);box.setAttribute("y",H-18);box.setAttribute("width",11);box.setAttribute("height",11);box.setAttribute("fill",item[0]);svg.appendChild(box);const text=document.createElementNS(svgNS,"text");text.setAttribute("x",x+17);text.setAttribute("y",H-8);text.setAttribute("fill","#475569");text.setAttribute("font-size","12");text.textContent=item[1];svg.appendChild(text)})
}
function render(){
 const students=new Set(transactions.map(r=>r.student_code).filter(Boolean));
 document.getElementById("txCount").textContent=money.format(transactions.length);
 document.getElementById("studentCount").textContent=money.format(students.size);
 document.getElementById("debitTotal").textContent=money.format(total(transactions,"total_debit"))+" ₫";
 document.getElementById("creditTotal").textContent=money.format(total(transactions,"total_credit"))+" ₫";
 drawDailyChart(groupByDate(transactions));
 const txPages=Math.max(1,Math.ceil(transactions.length/pageSize));txPage=Math.min(txPage,txPages-1);
 renderTable("transactions",transactions,[["accounting_date","date"],["school_year"],["term_name"],["month_name"],["school_name"],["student_code"],["student_name"],["total_debit","money"],["total_credit","money"],["payment_method"],["accounting_number"],["transaction_ref_no"],["is_valid_transaction","bool"],["is_scanned","bool"]],txPage);
 document.getElementById("txPage").textContent=`Trang ${txPage+1} / ${txPages}`;
 document.getElementById("txPrev").disabled=txPage===0;document.getElementById("txNext").disabled=txPage>=txPages-1;
 document.getElementById("txNote").textContent=`${transactions.length.toLocaleString("vi-VN")} giao dịch phù hợp với bộ lọc.`;
 const refundPages=Math.max(1,Math.ceil(refunds.length/pageSize));refundPage=Math.min(refundPage,refundPages-1);
 renderTable("refunds",refunds,[["refund_date","date"],["school_year"],["month_name"],["school_name"],["student_code"],["student_name"],["class_name"],["project_name"],["total_days_not_used"],["total_service_fee_per_day","money"],["refund_amount","money"]],refundPage);
 document.getElementById("refundPage").textContent=`Trang ${refundPage+1} / ${refundPages}`;
 document.getElementById("refundPrev").disabled=refundPage===0;document.getElementById("refundNext").disabled=refundPage>=refundPages-1;
 document.getElementById("refundNote").textContent=`${refunds.length.toLocaleString("vi-VN")} khoản hoàn phù hợp với bộ lọc.`;
}
async function loadData(){
 const status=document.getElementById("status"),button=document.getElementById("searchButton");status.style.display="none";button.disabled=true;button.textContent="Đang tải...";
 try{
  const from=document.getElementById("fromDate").value,to=document.getElementById("toDate").value,q=document.getElementById("studentSearch").value.trim();
  if(from&&to&&from>to)throw new Error("Ngày bắt đầu không được sau ngày kết thúc.");
  const params=new URLSearchParams({from,to,q});
  const [txResponse,refundResponse]=await Promise.all([fetch(`${API}?dataset=transactions&${params}`),fetch(`${API}?dataset=refunds&${params}`)]);
  const txData=await txResponse.json(),refundData=await refundResponse.json();
  if(!txResponse.ok)throw new Error(txData.error||"Không tải được giao dịch.");
  if(!refundResponse.ok)throw new Error(refundData.error||"Không tải được hoàn phí.");
  if(!Array.isArray(txData)||!Array.isArray(refundData))throw new Error("Dữ liệu trả về không đúng định dạng.");
  transactions=txData;refunds=refundData;txPage=0;refundPage=0;render();
 }catch(error){status.textContent=`Không tải được dữ liệu: ${error.message}`;status.style.display="block"}
 finally{button.disabled=false;button.textContent="Tra cứu"}
}
async function start(){
 const status=document.getElementById("status");
 try{
  const response=await fetch(`${API}?dataset=meta`),meta=await response.json();if(!response.ok)throw new Error(meta.error||"Không tải được kỳ dữ liệu.");
  document.getElementById("fromDate").value=meta.from;document.getElementById("toDate").value=meta.to;
  document.getElementById("filters").addEventListener("submit",event=>{event.preventDefault();loadData()});
  document.getElementById("txPrev").addEventListener("click",()=>{txPage=Math.max(0,txPage-1);render()});
  document.getElementById("txNext").addEventListener("click",()=>{txPage++;render()});
  document.getElementById("refundPrev").addEventListener("click",()=>{refundPage=Math.max(0,refundPage-1);render()});
  document.getElementById("refundNext").addEventListener("click",()=>{refundPage++;render()});
  await loadData();
 }catch(error){status.textContent=`Không tải được dashboard: ${error.message}`;status.style.display="block"}
}
start();
</script></body></html>"""


DASHBOARD_HOME = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard học phí | Chọn đối tượng</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}*{box-sizing:border-box}
body{margin:0}header{background:#172554;color:white;padding:26px max(24px,calc((100vw - 1120px)/2))}
h1{margin:0 0 6px;font-size:27px}header p{margin:0;color:#dbeafe}
main{max-width:1120px;margin:42px auto;padding:0 20px}.intro{color:#64748b;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
a.card{display:block;padding:24px;background:white;border:1px solid #dce4ef;border-radius:12px;text-decoration:none;color:inherit;box-shadow:0 4px 18px #12223b0b;transition:transform .15s,border-color .15s}
a.card:hover,a.card:focus{transform:translateY(-2px);border-color:#2563eb;outline:none}
.tag{color:#0f766e;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
h2{margin:10px 0;font-size:21px}p{line-height:1.6}.link{display:inline-block;margin-top:8px;color:#1d4ed8;font-weight:600}
footer{max-width:1120px;margin:0 auto;padding:20px;color:#64748b;font-size:12px}
@media(max-width:650px){.cards{grid-template-columns:1fr}main{margin:26px auto}}
</style></head>
<body><header><h1>Dashboard học phí</h1><p>Chọn đối tượng để xem bộ chỉ số và biểu đồ phù hợp.</p></header>
<main><p class="intro">Chọn một dashboard. Có thể bấm vào cột hoặc điểm dữ liệu để xem số liệu chi tiết tương ứng.</p>
<section class="cards" aria-label="Chọn đối tượng dashboard">
 <a class="card" href="/dashboard/ban-giam-hieu"><span class="tag">Điều hành nhà trường</span><h2>Ban giám hiệu</h2><p>Tổng quan phải thu, thanh toán ghi nhận, dư nợ cuối kỳ, so sánh cơ sở và hoàn phí.</p><span class="link">Mở dashboard Ban giám hiệu →</span></a>
 <a class="card" href="/dashboard/truong-phong-ke-toan"><span class="tag">Đối soát và kiểm soát</span><h2>Trưởng phòng Kế toán</h2><p>Đối chiếu ghi nợ/ghi có, phương thức thanh toán, trạng thái giao dịch và chi tiết hoàn phí.</p><span class="link">Mở dashboard Kế toán →</span></a>
 <a class="card" href="/dashboard/ke-toan-vien"><span class="tag">Tra cứu nghiệp vụ</span><h2>Kế toán viên</h2><p>Tra cứu giao dịch theo học sinh, đối soát theo ngày hạch toán và kiểm tra chi tiết hoàn phí.</p><span class="link">Mở dashboard Kế toán viên →</span></a>
</section></main><footer>Dashboard sử dụng dữ liệu học phí PostgreSQL trên máy cục bộ.</footer></body></html>"""


ACCOUNTING_DASHBOARD = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Đối soát học phí | Phòng Kế toán</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}
*{box-sizing:border-box}body{margin:0}header{background:#172554;color:#fff;padding:24px max(24px,calc((100vw - 1440px)/2))}
header h1{margin:0 0 5px;font-size:25px}header p{margin:0;color:#dbeafe}
.home-link{display:inline-block;color:#dbeafe;font-size:13px;margin-bottom:10px;text-decoration:none}.home-link:hover{text-decoration:underline}
main{max-width:1440px;margin:22px auto;padding:0 20px}
.toolbar{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:16px;padding:14px;background:#fff;border:1px solid #e1e7f0;border-radius:10px}
.toolbar label{font-size:13px;font-weight:600;color:#475569}.toolbar select,.toolbar input{display:block;margin-top:5px;padding:9px 10px;min-width:170px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;font:inherit;color:#172033}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:16px}
.card,.chart,.table-card{background:#fff;border:1px solid #e1e7f0;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.card{padding:16px}.card span{display:block;color:#64748b;font-size:13px}.card strong{display:block;margin-top:8px;font-size:20px;overflow-wrap:anywhere}
.charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.chart{padding:16px;min-width:0}.chart h2,.table-card h2{font-size:16px;margin:0 0 4px}.chart p,.table-card p{font-size:12px;color:#64748b;margin:0 0 8px}
.chart svg{display:block;width:100%;height:auto;min-height:220px}.empty{height:250px;display:grid;place-items:center;color:#64748b}
.chart svg [data-clickable="true"]{cursor:pointer}.chart svg [data-clickable="true"]:hover{filter:brightness(.88)}
.detail-panel{margin-top:16px;padding:16px;background:#fff;border:1px solid #e1e7f0;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.detail-heading{display:flex;justify-content:space-between;align-items:start;gap:12px;margin-bottom:12px}.detail-heading h2{margin:0 0 4px;font-size:17px}.detail-heading p{margin:0;color:#64748b;font-size:13px}.detail-heading button{padding:8px 12px;border:1px solid #cbd5e1;border-radius:6px;background:white;cursor:pointer}
.detail-scroll{overflow:auto;max-height:420px;border:1px solid #e2e8f0;border-radius:6px}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{padding:8px 10px;border-bottom:1px solid #e8edf4;text-align:left}th{background:#eaf0f8;position:sticky;top:0;z-index:1}td.num{text-align:right;font-variant-numeric:tabular-nums}
.detail-note{color:#64748b;font-size:12px;margin:10px 0 0}
.table-card{margin-top:16px;padding:16px;min-width:0}.table-scroll{overflow:auto;max-height:420px;border:1px solid #e2e8f0;border-radius:6px}
table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{padding:8px 10px;border-bottom:1px solid #e8edf4;text-align:left}th{background:#eaf0f8;position:sticky;top:0;z-index:1}td.num{text-align:right;font-variant-numeric:tabular-nums}
.pagination{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:10px;color:#64748b;font-size:13px}.pagination button{padding:7px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#172033;cursor:pointer}.pagination button:disabled{opacity:.45;cursor:default}
.status{padding:18px;background:#fff7ed;color:#9a3412;border-radius:8px;margin:12px 0;display:none}
.note{color:#64748b;font-size:12px;margin:12px 0 0}footer{color:#64748b;font-size:12px;padding:18px 2px 28px}
.detail-heading{display:flex;justify-content:space-between;align-items:start;gap:12px;margin-bottom:12px}.detail-heading h2{margin:0 0 4px}.detail-heading p{margin:0;color:#64748b;font-size:13px}.detail-heading button{padding:8px 12px;border:1px solid #cbd5e1;border-radius:6px;background:white;cursor:pointer}
.chart svg [data-clickable="true"]{cursor:pointer}.chart svg [data-clickable="true"]:hover{filter:brightness(.88)}
@media(max-width:850px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.charts{grid-template-columns:1fr}}
@media(max-width:480px){header{padding:20px}main{padding:0 12px}.cards{gap:8px}.card{padding:12px}.card strong{font-size:17px}.toolbar select,.toolbar input{min-width:130px}}
</style>
</head>
<body>
<header><a class="home-link" href="/dashboard">← Chọn đối tượng</a><h1>Đối soát học phí</h1><p>Trưởng phòng Kế toán · Theo dõi giao dịch, phương thức thanh toán và hoàn phí</p></header>
<main>
 <section class="toolbar" aria-label="Bộ lọc báo cáo">
  <label>Năm học<select id="yearFilter"></select></label>
  <label>Học kỳ<select id="termFilter"></select></label>
  <label>Cơ sở<select id="schoolFilter"></select></label>
  <label>Nhóm hoàn phí<select id="refundGroup"><option value="project_name">Dự án dịch vụ</option><option value="class_name">Lớp</option></select></label>
 </section>
 <div id="status" class="status" role="alert"></div>
 <section class="cards" aria-label="Chỉ số đối soát">
  <div class="card"><span>Tổng ghi nợ</span><strong id="kpiDebit">—</strong></div>
  <div class="card"><span>Tổng ghi có</span><strong id="kpiCredit">—</strong></div>
  <div class="card"><span>Giao dịch chưa hợp lệ / chưa quét</span><strong id="kpiReview">—</strong></div>
  <div class="card"><span>Tổng tiền hoàn phí</span><strong id="kpiRefund">—</strong></div>
 </section>
 <section class="charts" aria-label="Năm biểu đồ kế toán">
  <article class="chart"><h2>1. Ghi nợ và ghi có theo tháng</h2><p>Phân tích theo năm học, học kỳ và cơ sở đã chọn · bấm cột/điểm để xem chi tiết</p><div id="monthlyChart"></div></article>
  <article class="chart"><h2>2. Chênh lệch ghi nợ và ghi có theo tháng</h2><p>Giá trị thể hiện là ghi nợ trừ ghi có trong từng tháng · bấm điểm để xem giao dịch</p><div id="differenceChart"></div></article>
  <article class="chart"><h2>3. Giao dịch theo phương thức thanh toán</h2><p>Cột thể hiện tổng ghi nợ và ghi có; bấm cột để xem giao dịch</p><div id="methodChart"></div></article>
  <article class="chart"><h2>4. Kiểm soát trạng thái giao dịch</h2><p>Số giao dịch hợp lệ/chưa hợp lệ và đã quét/chưa quét · bấm cột để xem giao dịch</p><div id="statusChart"></div></article>
  <article class="chart"><h2>5. Hoàn phí theo nhóm</h2><p>Chọn nhóm theo dự án dịch vụ hoặc lớp; bấm cột để xem khoản hoàn</p><div id="refundChart"></div></article>
 </section>
 <p class="note" id="periodNote"></p>
 <section class="table-card"><h2>Giao dịch cần kiểm tra</h2><p>Giao dịch chưa hợp lệ hoặc chưa được quét; đối chiếu theo số hạch toán và mã tham chiếu.</p>
  <div class="table-scroll"><table><thead><tr><th>Ngày hạch toán</th><th>Cơ sở</th><th>Số hạch toán</th><th>Mã tham chiếu</th><th>Hợp lệ</th><th>Đã quét</th><th>Ghi nợ</th><th>Ghi có</th></tr></thead><tbody id="transactionRows"></tbody></table></div>
  <p class="note" id="transactionNote"></p>
  <div class="pagination"><button id="txPrev" type="button">Trước</button><span id="txPageLabel"></span><button id="txNext" type="button">Sau</button></div>
 </section>
 <section class="table-card"><h2>Chi tiết hoàn phí dịch vụ</h2><p>Thông tin phục vụ đối chiếu theo ngày hoàn, lớp, học sinh và dự án dịch vụ.</p>
  <div class="table-scroll"><table><thead><tr><th>Ngày hoàn</th><th>Cơ sở</th><th>Lớp</th><th>Học sinh</th><th>Dự án dịch vụ</th><th>Ngày chưa sử dụng</th><th>Tiền hoàn</th></tr></thead><tbody id="refundRows"></tbody></table></div>
  <p class="note" id="refundNote"></p>
  <div class="pagination"><button id="refundPrev" type="button">Trước</button><span id="refundPageLabel"></span><button id="refundNext" type="button">Sau</button></div>
 </section>
 <section class="table-card" id="chartDetails" hidden><div class="detail-heading"><div><h2 id="detailTitle">Chi tiết biểu đồ</h2><p id="detailSummary"></p></div><button id="closeDetails" type="button">Đóng</button></div><div class="table-scroll"><table><thead id="detailHead"></thead><tbody id="detailBody"></tbody></table></div><p class="note" id="detailNote"></p></section>
 <div class="pagination" id="detailPagination" hidden><button id="detailPrev" type="button">Trước</button><span id="detailPageLabel"></span><button id="detailNext" type="button">Sau</button></div>
 <footer>Dữ liệu lấy từ PostgreSQL. Danh sách hoàn phí có thông tin học sinh và chỉ dùng cho đối soát nội bộ; dashboard chỉ lắng nghe trên máy cục bộ. Trạng thái hợp lệ/quét lấy theo cờ trong dữ liệu nguồn.</footer>
</main>
<script>
const API="/api/accounting/dashboard",svgNS="http://www.w3.org/2000/svg";
const money=new Intl.NumberFormat("vi-VN",{maximumFractionDigits:0});
const shortMoney=new Intl.NumberFormat("vi-VN",{notation:"compact",maximumFractionDigits:1});
const colors=["#2563eb","#14b8a6","#f97316","#8b5cf6"];
let transactions=[],refunds=[];
let txPage=0,refundPage=0;
let detailRows=[],detailColumns=[],detailPage=0,detailTitle="";
const pageSize=100;
function val(v){return Number(v)||0}
function sum(rows,key){return rows.reduce((a,r)=>a+val(r[key]),0)}
function el(tag,attrs,text){const n=document.createElementNS(svgNS,tag);for(const[k,v]of Object.entries(attrs||{}))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;return n}
function empty(id,message){const target=document.getElementById(id);target.replaceChildren();const n=document.createElement("div");n.className="empty";n.textContent=message;target.appendChild(n)}
function axisSvg(id,categories,series,kind,onSelect){
 if(!categories.length){empty(id,"Không có dữ liệu cho bộ lọc đã chọn.");return}
 const target=document.getElementById(id);target.replaceChildren();const svg=el("svg",{viewBox:"0 0 860 310",role:"img"});target.appendChild(svg);
 const W=860,H=310,L=76,R=20,T=30,B=66,pw=W-L-R,ph=H-T-B;
 const values=categories.flatMap(c=>series.map(s=>val(c[s.key]))),min=Math.min(0,...values),max=Math.max(0,...values),range=max-min||1;
 const yFor=v=>T+(max-v)/range*ph,zeroY=yFor(0);
 for(let i=0;i<=4;i++){const y=T+ph*i/4,v=max-range*i/4;svg.appendChild(el("line",{x1:L,y1:y,x2:W-R,y2:y,stroke:"#e8edf4"}));svg.appendChild(el("text",{x:L-10,y:y+4,"text-anchor":"end",fill:"#64748b","font-size":12},shortMoney.format(v)))}
 const slot=pw/categories.length;
 if(kind==="line"){
  const step=categories.length>1?pw/(categories.length-1):0;
  svg.appendChild(el("line",{x1:L,y1:zeroY,x2:W-R,y2:zeroY,stroke:"#94a3b8","stroke-dasharray":"4 4"}));
  series.forEach(s=>{const pts=categories.map((c,i)=>[L+(categories.length>1?i*step:pw/2),yFor(val(c[s.key]))]);svg.appendChild(el("polyline",{points:pts.map(p=>p.join(",")).join(" "),fill:"none",stroke:s.color,"stroke-width":3,"stroke-linejoin":"round","stroke-linecap":"round"}));pts.forEach((p,i)=>{const dot=el("circle",{cx:p[0],cy:p[1],r:5,fill:s.color,"data-clickable":"true",tabindex:0,role:"button"});dot.appendChild(el("title",{},`${categories[i].label} · ${s.label}: ${money.format(val(categories[i][s.key]))} ₫ · Bấm để xem chi tiết`));dot.addEventListener("click",()=>onSelect?.(categories[i],s));dot.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();onSelect?.(categories[i],s)}});svg.appendChild(dot)})});
 }else{
  const groupW=Math.min(slot*.72,110),barW=groupW/series.length;
  svg.appendChild(el("line",{x1:L,y1:zeroY,x2:W-R,y2:zeroY,stroke:"#94a3b8"}));
  categories.forEach((c,i)=>series.forEach((s,j)=>{const v=val(c[s.key]),y=yFor(v),x=L+i*slot+(slot-groupW)/2+j*barW,extra=c.count!==undefined?` · ${c.count} giao dịch`:"";const rect=el("rect",{x,y:Math.min(y,zeroY),width:Math.max(2,barW-4),height:Math.max(1,Math.abs(zeroY-y)),rx:3,fill:s.color,"data-clickable":"true",tabindex:0,role:"button"});rect.appendChild(el("title",{},`${c.label} · ${s.label}: ${money.format(v)}${s.unit||" ₫"}${extra} · Bấm để xem chi tiết`));rect.addEventListener("click",()=>onSelect?.(c,s));rect.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();onSelect?.(c,s)}});svg.appendChild(rect)}))
 }
 const stride=Math.max(1,Math.ceil(categories.length/10));categories.forEach((c,i)=>{if(i%stride===0||i===categories.length-1){const x=kind==="line"?(L+(categories.length>1?i*pw/(categories.length-1):pw/2)):(L+i*slot+slot/2);const label=String(c.label||"").length>15?String(c.label).slice(0,14)+"…":c.label;svg.appendChild(el("text",{x,y:H-B+22,"text-anchor":"middle",fill:"#475569","font-size":11},label))}});
 let lx=L;series.forEach(s=>{svg.appendChild(el("rect",{x:lx,y:H-23,width:11,height:11,rx:2,fill:s.color}));svg.appendChild(el("text",{x:lx+16,y:H-13,fill:"#475569","font-size":12},s.label));lx+=s.label.length*7+40})
}
function groupRows(rows,key,label){const map=new Map();rows.forEach(r=>{const k=key(r)||"Chưa xác định";if(!map.has(k))map.set(k,{label:label(r)||"Chưa xác định",rows:[]});map.get(k).rows.push(r)});return [...map.values()]}
function currentTransactions(){
 const year=document.getElementById("yearFilter").value,term=document.getElementById("termFilter").value,school=document.getElementById("schoolFilter").value;
 return transactions.filter(r=>(!year||r.school_year===year)&&(!term||r.term_name===term)&&(!school||r.school_name===school))
}
function currentRefunds(){const y=document.getElementById("yearFilter").value,s=document.getElementById("schoolFilter").value;return refunds.filter(r=>(!y||r.school_year===y)&&(!s||r.school_name===s))}
function td(row,value,cls){const cell=document.createElement("td");cell.textContent=value===null||value===undefined||value===""?"—":String(value);if(cls)cell.className=cls;row.appendChild(cell)}
function showDetails(title,rows,columns){
 detailTitle=title;detailRows=rows;detailColumns=columns;detailPage=0;
 renderDetails();
 const panel=document.getElementById("chartDetails");panel.hidden=false;panel.scrollIntoView({behavior:"smooth",block:"start"});
}
function renderDetails(){
 const head=document.getElementById("detailHead"),body=document.getElementById("detailBody");
 head.replaceChildren();body.replaceChildren();
 const headerRow=document.createElement("tr");detailColumns.forEach(column=>td(headerRow,column.label));head.appendChild(headerRow);
 const pages=Math.max(1,Math.ceil(detailRows.length/pageSize));detailPage=Math.min(detailPage,pages-1);
 detailRows.slice(detailPage*pageSize,(detailPage+1)*pageSize).forEach(record=>{
  const row=document.createElement("tr");
  detailColumns.forEach(column=>{
   let value=record[column.key];
   if(column.format==="money")value=money.format(val(value))+" ₫";
   else if(column.format==="boolean")value=value?"Có":"Không";
   else if(column.format==="date")value=value?String(value).slice(0,10):"";
   td(row,value,column.format==="money"?"num":"");
  });
  body.appendChild(row);
 });
 document.getElementById("detailTitle").textContent=detailTitle;
 document.getElementById("detailSummary").textContent=`${detailRows.length.toLocaleString("vi-VN")} dòng dữ liệu phù hợp.`;
 document.getElementById("detailNote").textContent=detailRows.length?"Dùng nút phân trang để xem toàn bộ dữ liệu.":"Không có bản ghi chi tiết.";
 document.getElementById("detailPagination").hidden=detailRows.length<=pageSize;
 document.getElementById("detailPageLabel").textContent=`Trang ${detailPage+1} / ${pages}`;
 document.getElementById("detailPrev").disabled=detailPage===0;
 document.getElementById("detailNext").disabled=detailPage>=pages-1;
}
function render(){
 const rows=currentTransactions(),refundRows=currentRefunds();
 document.getElementById("kpiDebit").textContent=money.format(sum(rows,"total_debit"))+" ₫";
 document.getElementById("kpiCredit").textContent=money.format(sum(rows,"total_credit"))+" ₫";
 const needsReview=rows.filter(r=>!r.is_valid_transaction||!r.is_scanned);
 document.getElementById("kpiReview").textContent=money.format(needsReview.length);
 document.getElementById("kpiRefund").textContent=money.format(sum(refundRows,"refund_amount"))+" ₫";
 const months=groupRows(rows,r=>r.accounting_date?.slice(0,7),r=>{const d=r.accounting_date?.slice(0,7);return d?`Tháng ${Number(d.slice(5,7))}/${d.slice(0,4)}`:"Không rõ tháng"}).sort((a,b)=>a.rows[0].accounting_date.localeCompare(b.rows[0].accounting_date));
 const monthData=months.map(m=>({label:m.label,key:m.rows[0].accounting_date?.slice(0,7),rows:m.rows,debit:sum(m.rows,"total_debit"),credit:sum(m.rows,"total_credit"),difference:sum(m.rows,"total_debit")-sum(m.rows,"total_credit")}));
 const transactionColumns=[{key:"accounting_date",label:"Ngày hạch toán",format:"date"},{key:"school_name",label:"Cơ sở"},{key:"term_name",label:"Học kỳ"},{key:"payment_method",label:"Phương thức"},{key:"accounting_number",label:"Số hạch toán"},{key:"transaction_ref_no",label:"Mã tham chiếu"},{key:"total_debit",label:"Ghi nợ",format:"money"},{key:"total_credit",label:"Ghi có",format:"money"},{key:"is_valid_transaction",label:"Hợp lệ",format:"boolean"},{key:"is_scanned",label:"Đã quét",format:"boolean"}];
 const transactionRowsFor=category=>rows.filter(r=>r.accounting_date?.slice(0,7)===category.key);
 axisSvg("monthlyChart",monthData,[{key:"debit",label:"Ghi nợ",color:colors[0]},{key:"credit",label:"Ghi có",color:colors[1]}],"bar",(c,s)=>showDetails(`${c.label} · ${s.label}`,transactionRowsFor(c),transactionColumns));
 axisSvg("differenceChart",monthData,[{key:"difference",label:"Ghi nợ − ghi có",color:colors[2]}],"line",(c)=>showDetails(`${c.label} · Đối chiếu giao dịch`,transactionRowsFor(c),transactionColumns));
 const methods=groupRows(rows,r=>r.payment_method,r=>r.payment_method).map(m=>({label:m.label,rows:m.rows,debit:sum(m.rows,"total_debit"),credit:sum(m.rows,"total_credit"),count:m.rows.length}));
 const methodSeries=[{key:"debit",label:"Ghi nợ",color:colors[0]},{key:"credit",label:"Ghi có",color:colors[1]}];
 axisSvg("methodChart",methods,methodSeries,"bar",(c,s)=>showDetails(`${c.label} · ${s.label}`,c.rows,transactionColumns));
 const txStatus=[
  {label:"Hợp lệ",value:rows.filter(r=>r.is_valid_transaction).length},
  {label:"Chưa hợp lệ",value:rows.filter(r=>!r.is_valid_transaction).length},
  {label:"Đã quét",value:rows.filter(r=>r.is_scanned).length},
  {label:"Chưa quét",value:rows.filter(r=>!r.is_scanned).length}
 ];
 const statusCategories=txStatus.map(x=>({label:x.label,count:x.value,rows:rows.filter(r=>x.label==="Hợp lệ"?r.is_valid_transaction:x.label==="Chưa hợp lệ"?!r.is_valid_transaction:x.label==="Đã quét"?r.is_scanned:!r.is_scanned)}));
 axisSvg("statusChart",statusCategories,[{key:"count",label:"Số giao dịch",color:colors[3],unit:" giao dịch"}],"bar",(c)=>showDetails(`${c.label} · Giao dịch`,c.rows,transactionColumns));
 const groupKey=document.getElementById("refundGroup").value;
 const groups=groupRows(refundRows,r=>r[groupKey],r=>r[groupKey]).map(g=>({label:g.label,rows:g.rows,amount:sum(g.rows,"refund_amount"),count:g.rows.length,days:sum(g.rows,"total_days_not_used")}));
 groups.sort((a,b)=>b.amount-a.amount);
 axisSvg("refundChart",groups.slice(0,12),[{key:"amount",label:"Tiền hoàn",color:colors[2]}],"bar",(c)=>showDetails(`${c.label} · Chi tiết hoàn phí`,c.rows,[{key:"refund_date",label:"Ngày hoàn",format:"date"},{key:"school_name",label:"Cơ sở"},{key:"class_name",label:"Lớp"},{key:"student_name",label:"Học sinh"},{key:"project_name",label:"Dự án dịch vụ"},{key:"total_days_not_used",label:"Ngày chưa sử dụng"},{key:"refund_amount",label:"Tiền hoàn",format:"money"}]));

 const reviewSorted=needsReview.slice().sort((a,b)=>b.accounting_date.localeCompare(a.accounting_date));
 const txBody=document.getElementById("transactionRows");txBody.replaceChildren();
 const txPages=Math.max(1,Math.ceil(reviewSorted.length/pageSize));txPage=Math.min(txPage,txPages-1);
 reviewSorted.slice(txPage*pageSize,(txPage+1)*pageSize).forEach(r=>{const tr=document.createElement("tr");td(tr,r.accounting_date?.slice(0,10));td(tr,r.school_name);td(tr,r.accounting_number);td(tr,r.transaction_ref_no);td(tr,r.is_valid_transaction?"Có":"Không");td(tr,r.is_scanned?"Có":"Không");td(tr,money.format(val(r.total_debit)),"num");td(tr,money.format(val(r.total_credit)),"num");txBody.appendChild(tr)});
 document.getElementById("transactionNote").textContent=needsReview.length?`${needsReview.length} giao dịch cần rà soát.`:"Không có giao dịch chưa hợp lệ hoặc chưa quét trong bộ lọc hiện tại.";
 document.getElementById("txPageLabel").textContent=`Trang ${txPage+1} / ${txPages}`;
 document.getElementById("txPrev").disabled=txPage===0;document.getElementById("txNext").disabled=txPage>=txPages-1;
 const refundSorted=refundRows.slice().sort((a,b)=>b.refund_date.localeCompare(a.refund_date));
 const refundBody=document.getElementById("refundRows");refundBody.replaceChildren();
 const refundPages=Math.max(1,Math.ceil(refundSorted.length/pageSize));refundPage=Math.min(refundPage,refundPages-1);
 refundSorted.slice(refundPage*pageSize,(refundPage+1)*pageSize).forEach(r=>{const tr=document.createElement("tr");td(tr,r.refund_date?.slice(0,10));td(tr,r.school_name);td(tr,r.class_name);td(tr,r.student_name);td(tr,r.project_name);td(tr,money.format(val(r.total_days_not_used)),"num");td(tr,money.format(val(r.refund_amount))+" ₫","num");refundBody.appendChild(tr)});
 document.getElementById("refundNote").textContent=refundRows.length?`${refundRows.length} khoản hoàn phí.`:"Không có khoản hoàn phí trong bộ lọc hiện tại.";
 document.getElementById("refundPageLabel").textContent=`Trang ${refundPage+1} / ${refundPages}`;
 document.getElementById("refundPrev").disabled=refundPage===0;document.getElementById("refundNext").disabled=refundPage>=refundPages-1;
 document.getElementById("periodNote").textContent=`${rows.length.toLocaleString("vi-VN")} giao dịch · ${refundRows.length.toLocaleString("vi-VN")} khoản hoàn · tiền hoàn phân nhóm theo ${groupKey==="class_name"?"lớp":"dự án dịch vụ"}.`;
}
function setOptions(id,values,firstLabel){
 const select=document.getElementById(id),previous=select.value;
 select.replaceChildren();const all=document.createElement("option");all.value="";all.textContent=firstLabel;select.appendChild(all);
 values.forEach(value=>{const o=document.createElement("option");o.value=value;o.textContent=value;select.appendChild(o)});
 if(values.includes(previous))select.value=previous;
}
async function start(){
 const status=document.getElementById("status");
 try{
  const response=await fetch(API);if(!response.ok)throw new Error(`API trả về HTTP ${response.status}`);
  const data=await response.json();if(!Array.isArray(data.transactions)||!Array.isArray(data.refunds))throw new Error(data.error||"Dữ liệu kế toán không đúng định dạng.");
  transactions=data.transactions;refunds=data.refunds;
  const years=[...new Set(data.years||[])];
  if(!years.length)throw new Error("Không tìm thấy giao dịch trong ba năm học gần nhất.");
  setOptions("yearFilter",years,"Tất cả 3 năm học");document.getElementById("yearFilter").value=years[0];
  setOptions("schoolFilter",[...new Set(transactions.map(r=>r.school_name).filter(Boolean))].sort(),"Tất cả cơ sở");
  setOptions("termFilter",[...new Set(transactions.map(r=>r.term_name).filter(Boolean))].sort(),"Tất cả học kỳ");
  ["yearFilter","schoolFilter","termFilter","refundGroup"].forEach(id=>document.getElementById(id).addEventListener("change",()=>{
   txPage=0;refundPage=0;
   if(id==="yearFilter"){const subset=transactions.filter(r=>!document.getElementById(id).value||r.school_year===document.getElementById(id).value);setOptions("termFilter",[...new Set(subset.map(r=>r.term_name).filter(Boolean))].sort(),"Tất cả học kỳ")}
   render();
  }));
  document.getElementById("txPrev").addEventListener("click",()=>{txPage=Math.max(0,txPage-1);render()});
  document.getElementById("txNext").addEventListener("click",()=>{txPage++;render()});
  document.getElementById("refundPrev").addEventListener("click",()=>{refundPage=Math.max(0,refundPage-1);render()});
  document.getElementById("refundNext").addEventListener("click",()=>{refundPage++;render()});
  document.getElementById("closeDetails").addEventListener("click",()=>{document.getElementById("chartDetails").hidden=true});
  document.getElementById("detailPrev").addEventListener("click",()=>{detailPage=Math.max(0,detailPage-1);renderDetails()});
  document.getElementById("detailNext").addEventListener("click",()=>{detailPage++;renderDetails()});
  render();
 }catch(error){status.style.display="block";status.textContent=`Không tải được dashboard: ${error.message}`}
}
start();
</script>
</body></html>"""


DASHBOARD = r"""<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard học phí | Ban giám hiệu</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}
*{box-sizing:border-box}body{margin:0}header{background:#172554;color:#fff;padding:24px max(24px,calc((100vw - 1440px)/2))}
header h1{margin:0 0 5px;font-size:25px}header p{margin:0;color:#dbeafe}
main{max-width:1440px;margin:22px auto;padding:0 20px}
.toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap}
.toolbar label{font-weight:600}.toolbar select{margin-left:8px;padding:9px 30px 9px 10px;border:1px solid #cbd5e1;border-radius:7px;background:white;font:inherit}
.note{color:#64748b;font-size:13px}.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;margin-bottom:16px}
.card,.chart{background:white;border:1px solid #e1e7f0;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.card{padding:16px}.card span{display:block;color:#64748b;font-size:13px}.card strong{display:block;margin-top:8px;font-size:21px;overflow-wrap:anywhere}
.charts{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}.chart{padding:16px;min-width:0}.chart h2{font-size:16px;margin:0 0 4px}.chart p{font-size:12px;color:#64748b;margin:0 0 8px}
.chart svg{display:block;width:100%;height:auto;min-height:220px}.empty{height:250px;display:grid;place-items:center;color:#64748b}
.status{padding:18px;background:#fff7ed;color:#9a3412;border-radius:8px;margin:12px 0;display:none}
footer{color:#64748b;font-size:12px;padding:18px 2px 28px}
@media(max-width:850px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.charts{grid-template-columns:1fr}}
@media(max-width:480px){header{padding:20px}main{padding:0 12px}.cards{gap:8px}.card{padding:12px}.card strong{font-size:17px}}
</style>
</head>
<body>
<header><a class="home-link" href="/dashboard">← Chọn đối tượng</a><h1>Dashboard học phí</h1><p>Ban giám hiệu · Theo dõi số liệu thu học phí và hoàn phí</p></header>
<main>
 <div class="toolbar">
  <label for="schoolYear">Năm học
   <select id="schoolYear" aria-label="Lọc năm học"></select>
  </label>
  <span class="note" id="periodNote">Đang tải dữ liệu...</span>
 </div>
 <div id="status" class="status" role="alert"></div>
 <section class="cards" aria-label="Chỉ số tổng quan">
  <div class="card"><span>Phải thu trong năm học</span><strong id="kpiPayable">—</strong></div>
  <div class="card"><span>Thanh toán ghi nhận trong năm học</span><strong id="kpiPaid">—</strong></div>
  <div class="card"><span>Dư nợ cuối kỳ gần nhất</span><strong id="kpiDebt">—</strong></div>
  <div class="card"><span>Hoàn phí trong năm học</span><strong id="kpiRefund">—</strong></div>
 </section>
 <section class="charts" aria-label="Năm biểu đồ học phí">
  <article class="chart"><h2>1. Phải thu và thanh toán ghi nhận theo năm học</h2><p>Tổng các kỳ/tháng trong 3 năm học gần nhất · bấm cột để xem số liệu tháng</p><div id="yearChart"></div></article>
  <article class="chart"><h2>2. Xu hướng phải thu và thanh toán ghi nhận theo tháng</h2><p>Năm học đang chọn · bấm điểm để xem chi tiết theo cơ sở</p><div id="monthChart"></div></article>
  <article class="chart"><h2>3. Dư nợ cuối kỳ theo tháng</h2><p>Số dư là ảnh chụp cuối kỳ, không cộng dồn qua các tháng · bấm điểm để xem chi tiết</p><div id="debtChart"></div></article>
  <article class="chart"><h2>4. So sánh phải thu và thanh toán ghi nhận theo cơ sở</h2><p>Năm học đang chọn · bấm cột để xem theo tháng</p><div id="schoolChart"></div></article>
  <article class="chart"><h2>5. Hoàn phí và điều chỉnh giảm theo tháng</h2><p>Năm học đang chọn · bấm điểm để xem theo cơ sở</p><div id="refundChart"></div></article>
 </section>
 <section class="detail-panel" id="chartDetails" hidden><div class="detail-heading"><div><h2 id="detailTitle">Chi tiết biểu đồ</h2><p id="detailSummary"></p></div><button id="closeDetails" type="button">Đóng</button></div><div class="detail-scroll"><table><thead id="detailHead"></thead><tbody id="detailBody"></tbody></table></div><p class="detail-note" id="detailNote"></p></section>
 <footer>Dữ liệu tổng hợp từ PostgreSQL; mã học sinh không được hiển thị. Các tháng tương lai và tháng hiện tại chưa chốt được loại khỏi biểu đồ. Thanh toán ghi nhận có thể âm khi phát sinh giao dịch điều chỉnh/đảo ngược. Dư nợ thể hiện snapshot của tháng, không phải dòng tiền trong kỳ.</footer>
</main>
<script>
const API="/api/powerbi/tuition-monthly";
const money=new Intl.NumberFormat("vi-VN",{maximumFractionDigits:0});
const shortMoney=new Intl.NumberFormat("vi-VN",{notation:"compact",maximumFractionDigits:1});
const colors=["#2563eb","#14b8a6","#f97316","#8b5cf6"];
const svgNS="http://www.w3.org/2000/svg";
const fields={payable:"net_payable_amount",paid:"payment_amount",debt:"closing_debt_amount",refund:"refund_amount"};
const detailColumns=[["school_year","Năm học"],["month_name","Tháng"],["school_name","Cơ sở"],["term_name","Học kỳ"],["net_payable_amount","Phải thu"],["payment_amount","Thanh toán ghi nhận"],["closing_debt_amount","Dư nợ cuối kỳ"],["refund_amount","Hoàn phí"],["refund_adjustment_amount","Điều chỉnh hoàn"]];
function sum(rows,key){return rows.reduce((a,r)=>a+(Number(r[key])||0),0)}
function svgNode(tag,attrs,text){const el=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs||{}))el.setAttribute(k,v);if(text!==undefined)el.textContent=text;return el}
function chartBase(target){target.replaceChildren();const svg=svgNode("svg",{viewBox:"0 0 860 310",role:"img"});target.appendChild(svg);return svg}
function empty(target,message){target.innerHTML="";const box=document.createElement("div");box.className="empty";box.textContent=message;target.appendChild(box)}
function showPrincipalDetails(title,rows,metric){
 const panel=document.getElementById("chartDetails"),head=document.getElementById("detailHead"),body=document.getElementById("detailBody");
 head.replaceChildren();body.replaceChildren();
 const heading=document.createElement("tr");[...detailColumns.map(column=>column[1]),"Chỉ số đã chọn"].forEach(label=>{const th=document.createElement("th");th.textContent=label;heading.appendChild(th)});head.appendChild(heading);
 rows.forEach(record=>{const tr=document.createElement("tr");detailColumns.forEach(([key])=>{const td=document.createElement("td");const value=record[key];td.textContent=value===null||value===undefined?"—":key.endsWith("_amount")?money.format(Number(value)||0)+" ₫":String(value);if(key.endsWith("_amount"))td.className="num";tr.appendChild(td)});const selected=document.createElement("td");selected.textContent=`${metric.label}: ${money.format(Number(record[metric.key])||0)} ₫`;selected.className="num";tr.appendChild(selected);body.appendChild(tr)});
 document.getElementById("detailTitle").textContent=title;
 document.getElementById("detailSummary").textContent=`${rows.length.toLocaleString("vi-VN")} dòng trường-tháng trong vùng dữ liệu được chọn.`;
 document.getElementById("detailNote").textContent="Các khoản dư nợ là số dư cuối kỳ tại tháng tương ứng; không cộng snapshot qua nhiều tháng.";
 panel.hidden=false;panel.scrollIntoView({behavior:"smooth",block:"start"});
}
function drawGrouped(target,categories,series,onSelect){
 if(!categories.length){empty(target,"Không có dữ liệu trong năm học đã chọn.");return}
 const svg=chartBase(target),W=860,H=310,L=76,R=20,T=30,B=65,plotW=W-L-R,plotH=H-T-B;
 const max=Math.max(1,...categories.flatMap(c=>series.map(s=>Number(c[s.key])||0)));
 for(let i=0;i<=4;i++){const y=T+plotH*i/4;svg.appendChild(svgNode("line",{x1:L,y1:y,x2:W-R,y2:y,stroke:"#e8edf4"}));svg.appendChild(svgNode("text",{x:L-10,y:y+4,"text-anchor":"end",fill:"#64748b","font-size":12},shortMoney.format(max*(4-i)/4)))}
 const slot=plotW/categories.length,groupW=Math.min(slot*.68,110),barW=groupW/series.length;
 categories.forEach((c,i)=>{series.forEach((s,j)=>{const v=Number(c[s.key])||0,h=v/max*plotH,x=L+i*slot+(slot-groupW)/2+j*barW,y=T+plotH-h;const rect=svgNode("rect",{x,y,width:Math.max(2,barW-4),height:Math.max(1,h),rx:3,fill:s.color,"data-clickable":"true",tabindex:0,role:"button"});rect.appendChild(svgNode("title",{},`${c.label} · ${s.label}: ${money.format(v)} ₫ · Bấm để xem chi tiết`));rect.addEventListener("click",()=>onSelect?.(c,s));rect.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();onSelect?.(c,s)}});svg.appendChild(rect)});svg.appendChild(svgNode("text",{x:L+i*slot+slot/2,y:H-B+22,"text-anchor":"middle",fill:"#475569","font-size":12},c.label.length>15?c.label.slice(0,14)+"…":c.label))});
 let lx=L;series.forEach(s=>{svg.appendChild(svgNode("rect",{x:lx,y:H-23,width:11,height:11,rx:2,fill:s.color}));svg.appendChild(svgNode("text",{x:lx+16,y:H-13,fill:"#475569","font-size":12},s.label));lx+=s.label.length*7+38})
}
function drawLines(target,categories,series,onSelect){
 if(!categories.length){empty(target,"Không có dữ liệu trong năm học đã chọn.");return}
 const svg=chartBase(target),W=860,H=310,L=76,R=20,T=30,B=65,plotW=W-L-R,plotH=H-T-B;
 const max=Math.max(1,...categories.flatMap(c=>series.map(s=>Number(c[s.key])||0)));
 for(let i=0;i<=4;i++){const y=T+plotH*i/4;svg.appendChild(svgNode("line",{x1:L,y1:y,x2:W-R,y2:y,stroke:"#e8edf4"}));svg.appendChild(svgNode("text",{x:L-10,y:y+4,"text-anchor":"end",fill:"#64748b","font-size":12},shortMoney.format(max*(4-i)/4)))}
 const step=categories.length>1?plotW/(categories.length-1):0;
 series.forEach((s,si)=>{const points=categories.map((c,i)=>[L+(categories.length>1?i*step:plotW/2),T+plotH-(Number(c[s.key])||0)/max*plotH]);svg.appendChild(svgNode("polyline",{points:points.map(p=>p.join(",")).join(" "),fill:"none",stroke:s.color,"stroke-width":3,"stroke-linejoin":"round","stroke-linecap":"round"}));points.forEach((p,i)=>{const circle=svgNode("circle",{cx:p[0],cy:p[1],r:6,fill:s.color,"data-clickable":"true",tabindex:0,role:"button"});circle.appendChild(svgNode("title",{},`${categories[i].label} · ${s.label}: ${money.format(Number(categories[i][s.key])||0)} ₫ · Bấm để xem chi tiết`));circle.addEventListener("click",()=>onSelect?.(categories[i],s));circle.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();onSelect?.(categories[i],s)}});svg.appendChild(circle)})});
 const stride=Math.max(1,Math.ceil(categories.length/10));categories.forEach((c,i)=>{if(i%stride===0||i===categories.length-1)svg.appendChild(svgNode("text",{x:L+(categories.length>1?i*step:plotW/2),y:H-B+22,"text-anchor":"middle",fill:"#475569","font-size":11},c.label))});
 let lx=L;series.forEach(s=>{svg.appendChild(svgNode("line",{x1:lx,y1:H-18,x2:lx+14,y2:H-18,stroke:s.color,"stroke-width":3}));svg.appendChild(svgNode("text",{x:lx+20,y:H-13,fill:"#475569","font-size":12},s.label));lx+=s.label.length*7+48})
}
function aggregate(rows,key,label){const map=new Map();rows.forEach(r=>{const k=key(r);if(!map.has(k))map.set(k,{label:label(r),rows:[]});map.get(k).rows.push(r)});return [...map.values()]}
function render(data,years,selected){
 const yearRows=data.filter(r=>r.school_year===selected),months=aggregate(yearRows,r=>r.month_start,r=>r.month_name).sort((a,b)=>a.rows[0].month_start.localeCompare(b.rows[0].month_start));
 const latest=months.length?months[months.length-1].rows:[];
 document.getElementById("kpiPayable").textContent=money.format(sum(yearRows,fields.payable))+" ₫";
 document.getElementById("kpiPaid").textContent=money.format(sum(yearRows,fields.paid))+" ₫";
 document.getElementById("kpiDebt").textContent=money.format(sum(latest,fields.debt))+" ₫";
 document.getElementById("kpiRefund").textContent=money.format(sum(yearRows,fields.refund))+" ₫";
 document.getElementById("periodNote").textContent=`${selected} · ${months.length} tháng đã chốt · dư nợ cập nhật đến ${latest.length?latest[0].month_name:"—"}`;
 const yearData=years.map(y=>{const rs=data.filter(r=>r.school_year===y);return {label:y,rows:rs,payable:sum(rs,fields.payable),paid:sum(rs,fields.paid)}});
 drawGrouped(document.getElementById("yearChart"),yearData,[{key:"payable",label:"Phải thu",color:colors[0]},{key:"paid",label:"Thanh toán ghi nhận",color:colors[1]}],(c,s)=>showPrincipalDetails(`${c.label} · ${s.label}`,c.rows,s));
 const monthly=months.map(m=>({label:m.label,rows:m.rows,payable:sum(m.rows,fields.payable),paid:sum(m.rows,fields.paid),debt:sum(m.rows,fields.debt),refund:sum(m.rows,fields.refund)+sum(m.rows,"refund_adjustment_amount")}));
 drawLines(document.getElementById("monthChart"),monthly,[{key:"payable",label:"Phải thu",color:colors[0]},{key:"paid",label:"Thanh toán ghi nhận",color:colors[1]}],(c,s)=>showPrincipalDetails(`${c.label} · ${s.label}`,c.rows,s));
 drawLines(document.getElementById("debtChart"),monthly,[{key:"debt",label:"Dư nợ cuối kỳ",color:colors[2]}],(c,s)=>showPrincipalDetails(`${c.label} · ${s.label}`,c.rows,s));
 const schools=aggregate(yearRows,r=>r.school_id,r=>r.school_name).map(s=>({label:s.label,rows:s.rows,payable:sum(s.rows,fields.payable),paid:sum(s.rows,fields.paid)}));
 drawGrouped(document.getElementById("schoolChart"),schools,[{key:"payable",label:"Phải thu",color:colors[0]},{key:"paid",label:"Thanh toán ghi nhận",color:colors[1]}],(c,s)=>showPrincipalDetails(`${c.label} · ${s.label}`,c.rows,s));
 drawLines(document.getElementById("refundChart"),monthly,[{key:"refund",label:"Hoàn phí + điều chỉnh hoàn",color:colors[3]}],(c,s)=>showPrincipalDetails(`${c.label} · ${s.label}`,c.rows,s));
}
async function start(){
 const status=document.getElementById("status");
 try{
  const response=await fetch(API);if(!response.ok)throw new Error(`API trả về HTTP ${response.status}`);
  const all=await response.json();if(!Array.isArray(all))throw new Error(all.error||"Dữ liệu học phí không đúng định dạng.");
  const currentMonth=new Date();currentMonth.setDate(1);const cutoff=`${currentMonth.getFullYear()}-${String(currentMonth.getMonth()+1).padStart(2,"0")}-01`;
  const data=all.filter(r=>r.month_start<cutoff&&r.school_year&&r.month_start);
  const years=[...new Set(data.map(r=>r.school_year))].sort().slice(-3);
  if(!years.length)throw new Error("Không có dữ liệu học phí đã chốt trong các kỳ gần đây.");
  const select=document.getElementById("schoolYear");select.replaceChildren(...years.slice().reverse().map(y=>{const o=document.createElement("option");o.value=y;o.textContent=y;return o}));
  render(data,years,select.value);select.addEventListener("change",()=>render(data,years,select.value));
  document.getElementById("closeDetails").addEventListener("click",()=>{document.getElementById("chartDetails").hidden=true});
 }catch(error){status.style.display="block";status.textContent=`Không tải được dashboard: ${error.message}`}
}
start();
</script>
</body></html>"""


PAGE = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>K12 Database Browser</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f6fb;color:#172033;font:14px Segoe UI,Arial,sans-serif}
header{background:#172554;color:white;padding:20px 28px}header h1{margin:0 0 5px;font-size:22px}header p{margin:0;color:#cbd5e1}
main{max-width:1500px;margin:22px auto;padding:0 18px;display:grid;grid-template-columns:320px 1fr;gap:18px}
section{background:white;border:1px solid #e1e7f0;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.sidebar{padding:16px;max-height:calc(100vh - 145px);display:flex;flex-direction:column}.sidebar h2{font-size:16px;margin:0 0 12px}
input{padding:10px;border:1px solid #cbd5e1;border-radius:6px;font:inherit;width:100%;margin-bottom:10px}
#tables{overflow:auto}button.table{display:block;text-align:left;width:100%;border:0;background:white;padding:9px 8px;border-bottom:1px solid #f0f2f6;cursor:pointer;color:#172033}
button.table:hover,button.table.active{background:#eff6ff;color:#1d4ed8}small{color:#64748b;display:block;margin-top:3px}
.content{padding:18px;min-width:0}.bar{display:flex;justify-content:space-between;gap:12px;align-items:start;flex-wrap:wrap}.bar h2{margin:0 0 5px;font-size:19px}.muted{color:#64748b}.actions a,.actions button,.row-action{display:inline-block;text-decoration:none;background:#2563eb;color:white;padding:8px 11px;border:0;border-radius:6px;margin:0 0 10px 6px;cursor:pointer}.row-action{font-size:12px;padding:5px 7px;margin:0 3px 0 0}.row-action.delete{background:#dc2626}
.columns{margin:15px 0;padding:12px;background:#f8fafc;border-radius:6px;color:#475569}.scroll{overflow:auto;max-height:68vh;border:1px solid #e2e8f0;border-radius:6px}table{border-collapse:collapse;width:100%;white-space:nowrap}th,td{padding:8px 10px;border-bottom:1px solid #e8edf4;text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}th{background:#eaf0f8;position:sticky;top:0;z-index:1}#status{padding:18px;color:#64748b}
dialog{width:min(760px,94vw);max-height:90vh;border:0;border-radius:10px;padding:0;box-shadow:0 16px 60px #0f172a55}dialog::backdrop{background:#0f172a88}.dialog-head,.dialog-foot{padding:16px 20px;background:#f8fafc;display:flex;justify-content:space-between;align-items:center}.dialog-head h3{margin:0}.dialog-body{padding:18px 20px;max-height:65vh;overflow:auto}.field{display:grid;grid-template-columns:minmax(150px,1fr) 2fr;gap:12px;align-items:center;margin-bottom:10px}.field label{font-weight:600}.field input,.field textarea{margin:0}.field textarea{min-height:70px;padding:9px;border:1px solid #cbd5e1;border-radius:6px;font:inherit}.dialog-foot button{padding:9px 14px;border:0;border-radius:6px;cursor:pointer}.dialog-foot .save{background:#2563eb;color:white}.notice{margin:12px 0;color:#b91c1c}
@media(max-width:800px){main{grid-template-columns:1fr}.sidebar{max-height:40vh}}
</style></head><body>
<header><h1>K12 Staging · Table Browser</h1><p>Danh mục và xem trước dữ liệu PostgreSQL · Power BI có thể lấy CSV qua liên kết xuất dữ liệu</p></header>
<main><section class="sidebar"><h2>Danh sách bảng</h2><input id="search" placeholder="Tìm schema hoặc tên bảng..."><div id="tables">Đang tải...</div></section>
<section class="content"><div class="bar"><div><h2 id="heading">Chọn một bảng</h2><div class="muted" id="subheading">Bản xem trước tối đa 100 dòng</div></div><div class="actions"><button id="addRow" hidden>+ Thêm dòng</button><a id="csv" href="#" hidden>Xuất CSV / Power BI</a></div></div><div class="columns" id="columns" hidden></div><div class="scroll" id="preview"><div id="status">Chọn bảng ở danh sách bên trái.</div></div></section></main>
<dialog id="editor"><form id="editorForm"><div class="dialog-head"><h3 id="dialogTitle">Bản ghi</h3><button type="button" id="closeDialog">Đóng</button></div><div class="dialog-body"><div id="formFields"></div><div class="notice" id="formError"></div></div><div class="dialog-foot"><span class="muted">Để trống trường tùy chọn để lưu NULL.</span><button class="save" type="submit">Lưu</button></div></form></dialog>
<script>
const list=document.querySelector('#tables'), search=document.querySelector('#search'), dialog=document.querySelector('#editor'); let selected=null, timer, currentData=null, tableInfo=null, editingKey=null;
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function loadTables(){list.textContent='Đang tải...';try{let r=await fetch('/api/tables?q='+encodeURIComponent(search.value));if(!r.ok)throw new Error((await r.json()).error);let a=await r.json();list.innerHTML=a.map(x=>`<button class="table ${selected&&selected.schema===x.schema&&selected.table===x.table?'active':''}" data-s="${esc(x.schema)}" data-t="${esc(x.table)}">${esc(x.table)}<small>${esc(x.schema)} · ~${Number(x.estimated_rows).toLocaleString()} dòng · ${esc(x.kind)}</small></button>`).join('')||'<div id="status">Không tìm thấy bảng.</div>';list.querySelectorAll('button').forEach(b=>b.onclick=()=>openTable(b.dataset.s,b.dataset.t))}catch(e){list.textContent=e.message}}
async function openTable(schema,table){selected={schema,table};loadTables();document.querySelector('#heading').textContent=schema+'.'+table;document.querySelector('#subheading').textContent='Đang tải bản xem trước...';document.querySelector('#preview').innerHTML='<div id="status">Đang tải dữ liệu...</div>';let params=new URLSearchParams({schema,table});document.querySelector('#csv').href='/export.csv?'+params;document.querySelector('#csv').hidden=false;try{let [dr,ir]=await Promise.all([fetch('/api/data?'+new URLSearchParams({schema,table,limit:100})),fetch('/api/table-info?'+params)]);let d=await dr.json();tableInfo=ir.ok?await ir.json():null;if(!dr.ok)throw new Error(d.error);currentData=d;document.querySelector('#subheading').textContent=`Hiển thị ${d.rows.length} dòng đầu tiên`;document.querySelector('#addRow').hidden=!tableInfo;let ci=document.querySelector('#columns');ci.hidden=false;ci.innerHTML='<b>Cột:</b> '+d.columns.map(x=>{let c=tableInfo?.columns.find(y=>y.name===x);return `${esc(x)} <span class="muted">(${esc(c?.type||'')})</span>`}).join(' · ');let actions=tableInfo?.primary_key.length?'<th>Thao tác</th>':'';document.querySelector('#preview').innerHTML='<table><thead><tr>'+d.columns.map(x=>'<th>'+esc(x)+'</th>').join('')+actions+'</tr></thead><tbody>'+d.rows.map((row,i)=>'<tr>'+row.map(v=>'<td title="'+esc(v)+'">'+esc(v)+'</td>').join('')+(actions?`<td><button class="row-action" data-edit="${i}">Sửa</button><button class="row-action delete" data-delete="${i}">Xóa</button></td>`:'')+'</tr>').join('')+'</tbody></table>';document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>editRow(Number(b.dataset.edit)));document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>deleteRow(Number(b.dataset.delete)))}catch(e){document.querySelector('#preview').innerHTML='<div id="status">'+esc(e.message)+'</div>'}}
function openEditor(row=null,key=null){if(!tableInfo)return;editingKey=key;document.querySelector('#dialogTitle').textContent=row?'Sửa bản ghi':'Thêm bản ghi';document.querySelector('#formError').textContent='';let fields=tableInfo.columns.filter(c=>row?c.updatable:c.insertable);document.querySelector('#formFields').innerHTML=fields.map(c=>{let val=row?.[c.name];let text=val==null?'':(typeof val==='object'?JSON.stringify(val):String(val));let area=/json|\[\]/i.test(c.type)||c.type==='ARRAY';let required=!row&&!c.nullable&&!c.has_default;return `<div class="field"><label for="f_${esc(c.name)}">${esc(c.name)}${required?' *':''}<small>${esc(c.type)}${c.primary_key?' · khóa chính':''}</small></label>${area?`<textarea id="f_${esc(c.name)}" name="${esc(c.name)}" ${required?'required':''}>${esc(text)}</textarea>`:`<input id="f_${esc(c.name)}" name="${esc(c.name)}" value="${esc(text)}" ${required?'required':''} placeholder="${c.nullable?'Để trống = NULL':''}">`}</div>`}).join('')||'<p>Bảng không có cột nào có thể nhập hoặc sửa.</p>';dialog.showModal()}
async function editRow(i){let row=currentData.rows[i],key={};tableInfo.primary_key.forEach(k=>key[k]=row[currentData.columns.indexOf(k)]);let res=await fetch('/api/row?'+new URLSearchParams({schema:selected.schema,table:selected.table,key:JSON.stringify(key)}));let full=await res.json();if(!res.ok){alert(full.error);return}openEditor(full,key)}
async function deleteRow(i){if(!confirm('Xóa bản ghi này? Thao tác sẽ ghi trực tiếp vào database.'))return;let row=currentData.rows[i],key={};tableInfo.primary_key.forEach(k=>key[k]=row[currentData.columns.indexOf(k)]);let r=await fetch('/api/rows',{method:'DELETE',headers:{'Content-Type':'application/json'},body:JSON.stringify({...selected,key})}),d=await r.json();if(!r.ok){alert(d.error);return}await openTable(selected.schema,selected.table)}
document.querySelector('#addRow').onclick=()=>openEditor();document.querySelector('#closeDialog').onclick=()=>dialog.close();document.querySelector('#editorForm').onsubmit=async e=>{e.preventDefault();let values={};new FormData(e.currentTarget).forEach((v,k)=>{let c=tableInfo.columns.find(x=>x.name===k);if(!(v===''&&!editingKey&&c?.has_default))values[k]=v});let r=await fetch('/api/rows',{method:editingKey?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...selected,key:editingKey,values})}),d=await r.json();if(!r.ok){document.querySelector('#formError').textContent=d.error;return}dialog.close();await openTable(selected.schema,selected.table)};
search.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(loadTables,200)});loadTables();
</script></body></html>"""


@app.get("/")
def home():
    return render_template_string(PAGE)


if __name__ == "__main__":
    app.run(host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "5000")), debug=False)
