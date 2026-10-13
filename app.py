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


@app.get("/dashboard")
def dashboard_home():
    return render_template_string(DASHBOARD_HOME, roles=MEDICAL_ROLES)


@app.get("/api/medical/dashboard")
def medical_dashboard_data():
    start_value = request.args.get("from", "").strip()
    end_value = request.args.get("to", "").strip()
    school_value = request.args.get("school_id", "").strip()
    try:
        start_date = date.fromisoformat(start_value) if start_value else None
        end_date = date.fromisoformat(end_value) if end_value else None
        school_id = int(school_value) if school_value else None
        if school_id is not None and school_id < 1:
            raise ValueError("school_id phải là số nguyên dương.")
        if start_date and end_date and start_date > end_date:
            raise ValueError("Ngày bắt đầu không được sau ngày kết thúc.")
    except ValueError as exc:
        return jsonify({"error": f"Tham số không hợp lệ: {exc}"}), 400

    school_filter = school_id
    start_filter = start_date
    end_filter = end_date
    try:
        with connect_db() as conn:
            schools = conn.execute(
                """SELECT id, name FROM public.school
                   WHERE deleted_at IS NULL
                   ORDER BY name, id"""
            ).fetchall()

            health = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          COUNT(*) FILTER (WHERE NULLIF(BTRIM(di_ung), '') IS NOT NULL),
                          COUNT(*) FILTER (WHERE NULLIF(BTRIM(current_disease), '') IS NOT NULL)
                   FROM public.medical_student_health_records h
                   WHERE h.deleted_at IS NULL
                     AND (%s::integer IS NULL OR h.school_id = %s)
                     AND (%s::date IS NULL OR h.created_at::date >= %s)
                     AND (%s::date IS NULL OR h.created_at::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            quick = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          COUNT(*) FILTER (WHERE status_send_to_parent::text = 'DA_GUI_CHO_PH'),
                          COUNT(*) FILTER (WHERE status_send_to_parent::text = 'CHO_GVCN_XAC_NHAN')
                   FROM public.medical_quick_examinations q
                   WHERE q.deleted_at IS NULL
                     AND (%s::integer IS NULL OR q.school_id = %s)
                     AND (%s::date IS NULL OR q.day::date >= %s)
                     AND (%s::date IS NULL OR q.day::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            exams = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          AVG(bmi) FILTER (WHERE bmi > 0),
                          COUNT(*) FILTER (WHERE NULLIF(BTRIM(z_score_bmi_conclude), '') IS NOT NULL)
                   FROM public.medical_examination_generals e
                   WHERE e.deleted_at IS NULL
                     AND (%s::integer IS NULL OR e.school_id = %s)
                     AND (%s::date IS NULL OR e.examination_date::date >= %s)
                     AND (%s::date IS NULL OR e.examination_date::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            bhyt = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT user_id),
                          COUNT(DISTINCT user_id) FILTER (WHERE ngay_het_han::date >= CURRENT_DATE),
                          COUNT(*) FILTER (WHERE ngay_het_han::date >= CURRENT_DATE
                                             AND ngay_het_han::date < CURRENT_DATE + 30),
                          COUNT(*) FILTER (WHERE ngay_het_han::date < CURRENT_DATE)
                   FROM public.medical_manager_bhyts b
                   WHERE b.deleted_at IS NULL
                     AND (%s::integer IS NULL OR b.school_id = %s)
                     AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date >= %s)
                     AND (%s::date IS NULL OR COALESCE(b.ngay_hieu_luc, b.created_at)::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            vaccination = conn.execute(
                """SELECT COUNT(*),
                          (SELECT COUNT(*)
                           FROM public.medical_vaccination_records r
                           JOIN public.medical_vaccination_events re
                             ON re.id = r.medical_vaccination_event_id
                            AND re.deleted_at IS NULL
                           WHERE r.deleted_at IS NULL
                             AND (%s::integer IS NULL OR r.school_id = %s)
                             AND (%s::date IS NULL OR re.date::date >= %s)
                             AND (%s::date IS NULL OR re.date::date <= %s)),
                          (SELECT COUNT(DISTINCT r.user_id)
                           FROM public.medical_vaccination_records r
                           JOIN public.medical_vaccination_events re
                             ON re.id = r.medical_vaccination_event_id
                            AND re.deleted_at IS NULL
                           WHERE r.deleted_at IS NULL
                             AND (%s::integer IS NULL OR r.school_id = %s)
                             AND (%s::date IS NULL OR re.date::date >= %s)
                             AND (%s::date IS NULL OR re.date::date <= %s))
                   FROM public.medical_vaccination_events v
                   WHERE v.deleted_at IS NULL
                     AND (%s::integer IS NULL OR v.school_id = %s)
                     AND (%s::date IS NULL OR v.date::date >= %s)
                     AND (%s::date IS NULL OR v.date::date <= %s)""",
                (
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                ),
            ).fetchone()

            medicine = conn.execute(
                """SELECT COUNT(DISTINCT m.id), COUNT(d.id),
                          COUNT(DISTINCT m.id) FILTER (
                              WHERE d.start_date::date <= CURRENT_DATE
                                AND (d.end_date IS NULL OR d.end_date::date >= CURRENT_DATE))
                   FROM public.medical_coach_medicines m
                   LEFT JOIN public.medical_coach_medicine_details d
                     ON d.medical_coach_medicine_id = m.id AND d.deleted_at IS NULL
                   WHERE m.deleted_at IS NULL
                     AND (%s::integer IS NULL OR m.school_id = %s)
                     AND (%s::date IS NULL OR m.created_at::date >= %s)
                     AND (%s::date IS NULL OR m.created_at::date <= %s)""",
                (school_filter, school_filter, start_filter, start_filter, end_filter, end_filter),
            ).fetchone()

            series = conn.execute(
                """SELECT 'Khám nhanh' AS category, date_trunc('month', q.day)::date AS period,
                          COUNT(*) AS total
                   FROM public.medical_quick_examinations q
                   WHERE q.deleted_at IS NULL
                     AND q.day IS NOT NULL
                     AND (%s::integer IS NULL OR q.school_id = %s)
                     AND (%s::date IS NULL OR q.day::date >= %s)
                     AND (%s::date IS NULL OR q.day::date <= %s)
                   GROUP BY 1, 2
                   UNION ALL
                   SELECT 'Khám định kỳ', date_trunc('month', e.examination_date)::date, COUNT(*)
                   FROM public.medical_examination_generals e
                   WHERE e.deleted_at IS NULL
                     AND e.examination_date IS NOT NULL
                     AND (%s::integer IS NULL OR e.school_id = %s)
                     AND (%s::date IS NULL OR e.examination_date::date >= %s)
                     AND (%s::date IS NULL OR e.examination_date::date <= %s)
                   GROUP BY 1, 2
                   UNION ALL
                   SELECT 'Sự kiện tiêm chủng', date_trunc('month', v.date)::date, COUNT(*)
                   FROM public.medical_vaccination_events v
                   WHERE v.deleted_at IS NULL
                     AND v.date IS NOT NULL
                     AND (%s::integer IS NULL OR v.school_id = %s)
                     AND (%s::date IS NULL OR v.date::date >= %s)
                     AND (%s::date IS NULL OR v.date::date <= %s)
                   GROUP BY 1, 2
                   ORDER BY period, category""",
                (
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                    school_filter, school_filter, start_filter, start_filter, end_filter, end_filter,
                ),
            ).fetchall()

        def records(fields, rows):
            return [
                {name: json_value(value) for name, value in zip(fields, row)}
                for row in rows
            ]

        return jsonify({
            "filters": {
                "from": start_date.isoformat() if start_date else None,
                "to": end_date.isoformat() if end_date else None,
                "school_id": school_id,
            },
            "schools": [{"id": row[0], "name": row[1]} for row in schools],
            "metrics": {
                "health_profiles": health[0],
                "students_with_health_profiles": health[1],
                "allergy_profiles": health[2],
                "current_condition_profiles": health[3],
                "quick_examinations": quick[0],
                "students_with_quick_examinations": quick[1],
                "parent_notifications_sent": quick[2],
                "teacher_confirmations_pending": quick[3],
                "periodic_examinations": exams[0],
                "students_with_periodic_examinations": exams[1],
                "average_bmi": float(exams[2]) if exams[2] is not None else None,
                "examinations_with_bmi_conclusion": exams[3],
                "bhyt_records": bhyt[0],
                "students_with_bhyt": bhyt[1],
                "bhyt_valid": bhyt[2],
                "bhyt_expiring_soon": bhyt[3],
                "bhyt_expired": bhyt[4],
                "vaccination_events": vaccination[0],
                "vaccination_records": vaccination[1],
                "students_vaccinated": vaccination[2],
                "medicine_records": medicine[0],
                "medicine_details": medicine[1],
                "active_medicine_records": medicine[2],
            },
            "series": records(["category", "period", "total"], series),
        })
    except psycopg.Error as exc:
        return jsonify({"error": f"Không truy vấn được dữ liệu y tế: {exc}"}), 503


@app.get("/dashboard/<role_key>")
def medical_role_dashboard(role_key):
    role = MEDICAL_ROLES_BY_KEY.get(role_key)
    if role is None:
        return jsonify({"error": "Không tìm thấy dashboard cho đối tượng này."}), 404
    return render_template_string(MEDICAL_DASHBOARD, role=role, palette=MEDICAL_PALETTE)


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


MEDICAL_ROLES = [
    {
        "key": "ban-giam-hieu",
        "title": "Ban giám hiệu",
        "subtitle": "Điều hành và phân bổ nguồn lực chăm sóc sức khỏe học sinh",
        "story": "Là Ban giám hiệu, tôi muốn theo dõi quy mô hồ sơ sức khỏe, hoạt động khám, BHYT và tiêm chủng theo cơ sở và thời gian để ưu tiên nguồn lực, xây dựng kế hoạch chăm sóc sức khỏe và phối hợp với phụ huynh.",
        "metrics": [
            ("healthProfiles", "Hồ sơ sức khỏe", "health_profiles"),
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("bhytValid", "Học sinh có BHYT chưa qua ngày hết hạn", "bhyt_valid"),
            ("bhytExpiring", "Hồ sơ BHYT hết hạn trong 30 ngày", "bhyt_expiring_soon"),
            ("vaccinationEvents", "Sự kiện tiêm chủng", "vaccination_events"),
            ("vaccinationRecords", "Lượt tiêm đã ghi nhận", "vaccination_records"),
            ("medicineRecords", "Hồ sơ thuốc", "medicine_records"),
        ],
        "charts": ["Khám nhanh", "Khám định kỳ", "Sự kiện tiêm chủng"],
        "enabled": True,
    },
    {
        "key": "truong-bo-phan-y-te",
        "title": "Trưởng bộ phận y tế học đường",
        "subtitle": "Điều phối nghiệp vụ y tế và theo dõi khối lượng xử lý",
        "story": "Là Trưởng bộ phận y tế học đường, tôi muốn theo dõi khối lượng khám, tình trạng thông báo, kết quả khám định kỳ và hồ sơ cần lưu ý để phân công nhân sự, lập kế hoạch theo dõi và báo cáo nhà trường.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
            ("teacherConfirmations", "Chờ giáo viên xác nhận", "teacher_confirmations_pending"),
            ("bmiConclusions", "Hồ sơ khám có kết luận BMI", "examinations_with_bmi_conclusion"),
            ("bhytExpiring", "Hồ sơ BHYT hết hạn trong 30 ngày", "bhyt_expiring_soon"),
            ("activeMedicineRecords", "Hồ sơ thuốc đang theo dõi", "active_medicine_records"),
        ],
        "charts": ["Khám nhanh", "Khám định kỳ", "Sự kiện tiêm chủng"],
        "enabled": True,
    },
    {
        "key": "nhan-vien-y-te",
        "title": "Nhân viên y tế / y tá trường học",
        "subtitle": "Theo dõi lượt khám nhanh và trạng thái phối hợp",
        "story": "Là Nhân viên y tế, tôi muốn theo dõi lượt khám nhanh, số học sinh đến khám và tình trạng gửi thông báo hoặc chờ xác nhận để sắp xếp công việc và tiếp tục xử lý các trường hợp cần phối hợp.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("quickStudents", "Học sinh có lượt khám", "students_with_quick_examinations"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
            ("teacherConfirmations", "Chờ giáo viên xác nhận", "teacher_confirmations_pending"),
        ],
        "charts": ["Khám nhanh"],
        "enabled": True,
    },
    {
        "key": "bac-si-don-vi-kham",
        "title": "Bác sĩ / đơn vị khám sức khỏe",
        "subtitle": "Theo dõi tổng hợp hoạt động khám định kỳ",
        "story": "Là Bác sĩ hoặc đơn vị khám, tôi muốn theo dõi số lượt khám định kỳ và mức độ hoàn thành dữ liệu khám ở dạng tổng hợp để phối hợp tổ chức khám và lập báo cáo mà không truy cập thông tin cá nhân ngoài phạm vi được phép.",
        "metrics": [
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("examStudents", "Học sinh có lượt khám định kỳ", "students_with_periodic_examinations"),
            ("bmiConclusions", "Hồ sơ có kết luận BMI", "examinations_with_bmi_conclusion"),
        ],
        "charts": ["Khám định kỳ"],
        "enabled": False,
        "access_note": "Dashboard dành cho đơn vị khám bên ngoài chưa được mở vì hệ thống chưa có tài khoản, phân quyền hoặc cơ chế chia sẻ dữ liệu an toàn.",
    },
    {
        "key": "phu-trach-bhyt",
        "title": "Phụ trách BHYT học sinh",
        "subtitle": "Rà soát hồ sơ bảo hiểm theo cơ sở và thời gian",
        "story": "Là Người phụ trách BHYT, tôi muốn theo dõi số hồ sơ có ngày hết hạn đã qua hoặc sắp đến theo cơ sở để rà soát và chủ động phối hợp gia hạn.",
        "metrics": [
            ("bhytRecords", "Hồ sơ BHYT", "bhyt_records"),
            ("bhytStudents", "Học sinh có hồ sơ BHYT", "students_with_bhyt"),
            ("bhytValid", "Học sinh có ngày hết hạn chưa qua", "bhyt_valid"),
            ("bhytExpiring", "Hết hạn trong 30 ngày", "bhyt_expiring_soon"),
            ("bhytExpired", "Đã qua ngày hết hạn", "bhyt_expired"),
        ],
        "charts": [],
        "enabled": True,
    },
    {
        "key": "dieu-phoi-tiem-chung",
        "title": "Điều phối tiêm chủng",
        "subtitle": "Theo dõi sự kiện tiêm và tình trạng cập nhật bản ghi",
        "story": "Là Người điều phối tiêm chủng, tôi muốn theo dõi sự kiện và số lượt tiêm đã được ghi nhận theo thời gian để lập kế hoạch, rà soát dữ liệu và phối hợp với gia đình.",
        "metrics": [
            ("vaccinationEvents", "Sự kiện tiêm chủng", "vaccination_events"),
            ("vaccinationRecords", "Bản ghi tiêm", "vaccination_records"),
            ("vaccinatedStudents", "Học sinh có bản ghi tiêm", "students_vaccinated"),
        ],
        "charts": ["Sự kiện tiêm chủng"],
        "enabled": True,
    },
    {
        "key": "quan-ly-thuoc",
        "title": "Phụ trách quản lý thuốc tại trường",
        "subtitle": "Theo dõi hồ sơ quản lý thuốc ở mức tổng hợp",
        "story": "Là Người phụ trách quản lý thuốc, tôi muốn theo dõi số hồ sơ thuốc, chi tiết thuốc và hồ sơ có thời gian sử dụng hiện hành để rà soát thông tin phục vụ quy trình bảo quản và sử dụng thuốc tại trường.",
        "metrics": [
            ("medicineRecords", "Hồ sơ thuốc", "medicine_records"),
            ("medicineDetails", "Chi tiết thuốc ghi nhận", "medicine_details"),
            ("activeMedicineRecords", "Hồ sơ thuốc có thời gian hiện hành", "active_medicine_records"),
        ],
        "charts": [],
        "enabled": True,
    },
    {
        "key": "giao-vien-chu-nhiem",
        "title": "Giáo viên chủ nhiệm",
        "subtitle": "Phối hợp xác nhận thông báo y tế của lớp được phân công",
        "story": "Là Giáo viên chủ nhiệm, tôi muốn xem các thông báo y tế đang chờ xác nhận trong lớp được phân công để phối hợp phản hồi với nhân viên y tế và gia đình kịp thời.",
        "metrics": [
            ("teacherConfirmations", "Thông báo chờ xác nhận", "teacher_confirmations_pending"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
        ],
        "charts": [],
        "enabled": False,
        "access_note": "Chưa thể giới hạn dữ liệu theo lớp hoặc giáo viên đăng nhập. Cần bổ sung xác thực và phân quyền lớp trước khi bật dashboard này.",
    },
    {
        "key": "phu-huynh",
        "title": "Phụ huynh / người giám hộ",
        "subtitle": "Cổng theo dõi thông tin của học sinh được giám hộ",
        "story": "Là Phụ huynh, tôi muốn xem hồ sơ, thông báo sức khỏe và lịch khám của chính con mình để phối hợp chăm sóc và cập nhật thông tin cần thiết cho nhà trường.",
        "metrics": [],
        "charts": [],
        "enabled": False,
        "access_note": "Chưa thể mở dữ liệu học sinh cho phụ huynh. Cần đăng nhập, xác minh quan hệ giám hộ, giới hạn đúng học sinh và ghi nhật ký truy cập.",
    },
    {
        "key": "hoc-sinh",
        "title": "Học sinh",
        "subtitle": "Thông tin chăm sóc sức khỏe phù hợp với học sinh",
        "story": "Là Học sinh, tôi muốn xem lịch khám và thông báo sức khỏe được phép chia sẻ để chủ động tham gia các hoạt động chăm sóc sức khỏe tại trường.",
        "metrics": [],
        "charts": [],
        "enabled": False,
        "access_note": "Chưa mở dashboard cho học sinh. Cần xây dựng tài khoản phù hợp độ tuổi, quy tắc đồng ý của người giám hộ và chỉ hiển thị thông tin được phép.",
    },
    {
        "key": "cong-tac-hoc-sinh",
        "title": "Phòng công tác học sinh",
        "subtitle": "Phối hợp hỗ trợ sức khỏe học sinh ở mức tổng hợp",
        "story": "Là Người phụ trách công tác học sinh, tôi muốn theo dõi khối lượng hoạt động y tế ở mức tổng hợp để phối hợp giữa nhà trường, bộ phận y tế và gia đình mà không truy cập dữ liệu sức khỏe vượt quá nhiệm vụ.",
        "metrics": [
            ("quickExams", "Lượt khám nhanh", "quick_examinations"),
            ("periodicExams", "Lượt khám định kỳ", "periodic_examinations"),
            ("parentNotifications", "Thông báo đã gửi phụ huynh", "parent_notifications_sent"),
        ],
        "charts": ["Khám nhanh", "Khám định kỳ"],
        "enabled": True,
    },
]
MEDICAL_ROLES_BY_KEY = {role["key"]: role for role in MEDICAL_ROLES}
MEDICAL_PALETTE = {"Khám nhanh": "#2563eb", "Khám định kỳ": "#0f766e", "Sự kiện tiêm chủng": "#d97706"}


DASHBOARD_HOME = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard y tế học đường | Chọn đối tượng</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}*{box-sizing:border-box}
body{margin:0}header{background:#14532d;color:#fff;padding:26px max(24px,calc((100vw - 1240px)/2))}
h1{margin:0 0 6px;font-size:27px}header p{margin:0;color:#dcfce7}
main{max-width:1240px;margin:34px auto;padding:0 20px}.intro{color:#64748b;margin-bottom:20px;line-height:1.55}
.cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
a.card{display:flex;flex-direction:column;min-height:190px;padding:20px;background:white;border:1px solid #dce8df;border-radius:12px;text-decoration:none;color:inherit;box-shadow:0 4px 18px #12223b0b;transition:transform .15s,border-color .15s}
a.card:hover,a.card:focus{transform:translateY(-2px);border-color:#15803d;outline:none}.tag{color:#15803d;font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase}
h2{margin:9px 0;font-size:18px}p{line-height:1.55}.desc{color:#475569;font-size:13px;margin:0 0 12px}.link{margin-top:auto;color:#166534;font-weight:600;font-size:13px}
.pending{display:block;margin-top:8px;color:#b45309;font-size:12px;font-weight:600}
footer{max-width:1240px;margin:auto;padding:20px;color:#64748b;font-size:12px;line-height:1.5}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:600px){.cards{grid-template-columns:1fr}main{margin:24px auto;padding:0 12px}a.card{min-height:0}}
</style></head><body>
<header><h1>Dashboard Y tế học đường</h1><p>Chọn đối tượng để mở dashboard nghiệp vụ riêng.</p></header>
<main><p class="intro">Có 11 dashboard theo vai trò. Mỗi trang có chỉ số và user story phù hợp. Một số trang cần hoàn thiện đăng nhập và phân quyền trước khi bật dữ liệu cá nhân.</p>
<section class="cards" aria-label="Chọn đối tượng dashboard y tế">
{% for role in roles %}<a class="card" href="/dashboard/{{ role.key }}"><span class="tag">Đối tượng {{ loop.index }}</span><h2>{{ role.title }}</h2><p class="desc">{{ role.subtitle }}</p>{% if not role.enabled %}<span class="pending">Cần bổ sung phân quyền trước khi mở dữ liệu</span>{% endif %}<span class="link">Mở dashboard →</span></a>{% endfor %}
</section></main><footer>Dữ liệu sức khỏe là dữ liệu nhạy cảm. Dashboard chỉ hiển thị tổng hợp; hồ sơ cá nhân chỉ được mở khi đã triển khai xác thực, phân quyền và kiểm soát truy cập phù hợp.</footer></body></html>"""


MEDICAL_DASHBOARD = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ role.title }} | Dashboard y tế</title>
<style>
:root{font-family:"Segoe UI",Arial,sans-serif;color:#172033;background:#f3f6fb}*{box-sizing:border-box}
body{margin:0}header{background:#14532d;color:#fff;padding:24px max(24px,calc((100vw - 1320px)/2))}
header h1{margin:0 0 5px;font-size:25px}header p{margin:0;color:#dcfce7}.home-link{display:inline-block;color:#dcfce7;font-size:13px;margin-bottom:10px;text-decoration:none}
main{max-width:1320px;margin:22px auto;padding:0 20px}.toolbar{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin-bottom:14px;padding:14px;background:white;border:1px solid #dce8df;border-radius:10px}
.toolbar label{font-size:13px;font-weight:600;color:#475569}.toolbar input,.toolbar select{display:block;margin-top:5px;padding:9px 10px;min-width:165px;border:1px solid #cbd5e1;border-radius:7px;font:inherit;background:white}
button{font:inherit}.toolbar button{padding:10px 15px;border:0;border-radius:6px;background:#15803d;color:white;cursor:pointer}.toolbar button:disabled{opacity:.55;cursor:wait}
.status{display:none;padding:13px;background:#fff7ed;color:#9a3412;border-radius:8px;margin-bottom:14px}
.cards{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}.card,.panel,.story,.locked{background:white;border:1px solid #dce8df;border-radius:10px;box-shadow:0 3px 10px #12223b0a}
.card{padding:15px}.card span{display:block;color:#64748b;font-size:13px}.card strong{display:block;margin-top:7px;font-size:21px;overflow-wrap:anywhere}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}.panel,.story,.locked{padding:16px;margin-bottom:14px;min-width:0}.panel h2,.story h2{font-size:17px;margin:0 0 7px}.panel>p,.story p{margin:0;color:#475569;line-height:1.6}
.chart svg{display:block;width:100%;height:auto;min-height:190px}.chart svg [data-clickable="true"]{cursor:pointer}.chart svg [data-clickable="true"]:hover{filter:brightness(.88)}
.notice{padding:13px 15px;margin:0 0 16px;border-left:4px solid #d97706;background:#fffbeb;color:#713f12;border-radius:5px;font-size:13px;line-height:1.55}
.locked{border-color:#f4d5a6;background:#fffbeb}.locked h2{margin:0 0 8px;color:#92400e;font-size:17px}.locked p{margin:0;line-height:1.55}
.muted{color:#64748b;font-size:12px}footer{padding:8px 0 28px;color:#64748b;font-size:12px;line-height:1.5}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:620px){header{padding:20px}main{padding:0 12px}.grid{grid-template-columns:1fr}.cards{gap:8px}.card{padding:11px}.card strong{font-size:18px}.toolbar label,.toolbar input,.toolbar select{width:100%}}
</style></head><body>
<header><a class="home-link" href="/dashboard">← Chọn đối tượng</a><h1>{{ role.title }}</h1><p>{{ role.subtitle }}</p></header>
<main>
<article class="story"><h2>Mục tiêu nghiệp vụ</h2><p>{{ role.story }}</p></article>
{% if role.enabled %}
 <form class="toolbar" id="filters">
  <label>Từ ngày<input id="fromDate" type="date"></label><label>Đến ngày<input id="toDate" type="date"></label>
  <label>Cơ sở<select id="school"><option value="">Tất cả cơ sở</option></select></label><button id="loadButton" type="submit">Lọc dữ liệu</button>
 </form>
 <div id="status" class="status" role="alert"></div>
 <p class="notice"><b>Lưu ý bảo mật:</b> Trang này chỉ hiển thị số liệu tổng hợp, không trả tên học sinh, triệu chứng, chẩn đoán hoặc thông tin liên hệ. Dữ liệu sức khỏe là dữ liệu nhạy cảm; không dùng số liệu này thay cho tư vấn y tế chuyên môn.</p>
 <section class="cards" aria-label="Chỉ số theo vai trò">{% for metric in role.metrics %}<div class="card"><span>{{ metric[1] }}</span><strong id="{{ metric[0] }}">—</strong></div>{% endfor %}</section>
 <section class="grid" aria-label="Biểu đồ theo vai trò">{% for chart in role.charts %}<div class="panel chart"><h2>{{ chart }} theo tháng</h2><p>Bấm cột để lọc về tháng tương ứng.</p><div id="chart-{{ loop.index0 }}"></div></div>{% endfor %}
 {% if role.key in ["ban-giam-hieu","truong-bo-phan-y-te","phu-trach-bhyt"] %}<div class="panel"><h2>Tổng hợp BHYT</h2><p id="bhytSummary">Đang tải...</p></div>{% endif %}
 {% if role.key in ["ban-giam-hieu","truong-bo-phan-y-te","cong-tac-hoc-sinh"] %}<div class="panel"><h2>Hồ sơ sức khỏe (tổng hợp)</h2><p id="healthSummary">Đang tải...</p></div>{% endif %}</section>
 <footer>Bộ lọc thời gian áp dụng theo ngày khám/sự kiện hoặc ngày tạo hồ sơ tương ứng. Phân loại BHYT chỉ dựa trên ngày hết hạn, không thay thế trạng thái nghiệp vụ. Số lượt tiêm được tính từ bản ghi tiêm thực tế, không suy ra từ số sự kiện.</footer>
{% else %}
 <section class="locked" role="status"><h2>Dashboard chưa mở quyền truy cập</h2><p>{{ role.access_note }}</p></section>
 <p class="muted">Không gọi API dữ liệu và không hiển thị số liệu học sinh trên trang này.</p>
{% endif %}
</main>
{% if role.enabled %}
<script>
const API="/api/medical/dashboard",numberFormat=new Intl.NumberFormat("vi-VN"),svgNS="http://www.w3.org/2000/svg",metrics={{ role.metrics|tojson }},charts={{ role.charts|tojson }},palette={{ palette|tojson }};
let currentData=null;
function setMetric(id,value){const element=document.getElementById(id);if(element)element.textContent=value===null||value===undefined?"—":numberFormat.format(value)}
function drawChart(targetId,category,color){
 const target=document.getElementById(targetId);target.replaceChildren();const rows=(currentData.series||[]).filter(row=>row.category===category);
 if(!rows.length){const empty=document.createElement("p");empty.className="muted";empty.textContent="Không có dữ liệu trong khoảng lọc.";target.appendChild(empty);return}
 const W=680,H=225,L=48,R=12,T=18,B=44,pw=W-L-R,ph=H-T-B,slot=pw/rows.length,max=Math.max(1,...rows.map(row=>Number(row.total)||0)),svg=document.createElementNS(svgNS,"svg");
 svg.setAttribute("viewBox",`0 0 ${W} ${H}`);svg.setAttribute("role","img");svg.setAttribute("aria-label",`${category} theo tháng`);target.appendChild(svg);
 for(let n=0;n<=3;n++){const y=T+ph*n/3,line=document.createElementNS(svgNS,"line");line.setAttribute("x1",L);line.setAttribute("x2",W-R);line.setAttribute("y1",y);line.setAttribute("y2",y);line.setAttribute("stroke","#e8edf4");svg.appendChild(line);const label=document.createElementNS(svgNS,"text");label.setAttribute("x",L-6);label.setAttribute("y",y+4);label.setAttribute("text-anchor","end");label.setAttribute("fill","#64748b");label.setAttribute("font-size","10");label.textContent=numberFormat.format(Math.round(max*(3-n)/3));svg.appendChild(label)}
 rows.forEach((row,index)=>{const value=Number(row.total)||0,h=Math.max(1,value/max*ph),width=Math.max(3,Math.min(28,slot*.64)),x=L+index*slot+(slot-width)/2,y=T+ph-h,rect=document.createElementNS(svgNS,"rect");rect.setAttribute("x",x);rect.setAttribute("y",y);rect.setAttribute("width",width);rect.setAttribute("height",h);rect.setAttribute("rx",3);rect.setAttribute("fill",color);rect.setAttribute("data-clickable","true");rect.setAttribute("tabindex","0");rect.setAttribute("role","button");const month=String(row.period).slice(0,7),title=document.createElementNS(svgNS,"title");title.textContent=`${month}: ${numberFormat.format(value)} · Bấm để lọc tháng`;rect.appendChild(title);const selectMonth=()=>{document.getElementById("fromDate").value=`${month}-01`;const lastDay=new Date(Number(month.slice(0,4)),Number(month.slice(5,7)),0).getDate();document.getElementById("toDate").value=`${month}-${String(lastDay).padStart(2,"0")}`;document.getElementById("filters").requestSubmit()};rect.addEventListener("click",selectMonth);rect.addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();selectMonth()}});svg.appendChild(rect);if(index%Math.max(1,Math.ceil(rows.length/8))===0||index===rows.length-1){const text=document.createElementNS(svgNS,"text");text.setAttribute("x",L+index*slot+slot/2);text.setAttribute("y",H-15);text.setAttribute("text-anchor","middle");text.setAttribute("fill","#475569");text.setAttribute("font-size","10");text.textContent=month;svg.appendChild(text)}});
}
function renderData(data){
 currentData=data;const values=data.metrics;
 metrics.forEach(([id,,key])=>setMetric(id,values[key]));
 const bhyt=document.getElementById("bhytSummary");if(bhyt)bhyt.textContent=`${numberFormat.format(values.bhyt_records)} hồ sơ · ${numberFormat.format(values.bhyt_valid)} học sinh có ngày hết hạn chưa qua · ${numberFormat.format(values.bhyt_expiring_soon)} hồ sơ hết hạn trong 30 ngày tới · ${numberFormat.format(values.bhyt_expired)} hồ sơ đã qua ngày hết hạn.`;
 const health=document.getElementById("healthSummary");if(health)health.textContent=`${numberFormat.format(values.students_with_health_profiles)} học sinh có hồ sơ sức khỏe · ${numberFormat.format(values.allergy_profiles)} hồ sơ có ghi nhận dị ứng · ${numberFormat.format(values.current_condition_profiles)} hồ sơ có ghi nhận bệnh hiện tại.`;
 charts.forEach((category,index)=>drawChart(`chart-${index}`,category,palette[category]));
}
async function loadData(event){
 if(event)event.preventDefault();const button=document.getElementById("loadButton"),status=document.getElementById("status");button.disabled=true;button.textContent="Đang tải...";status.style.display="none";
 try{const params=new URLSearchParams({from:document.getElementById("fromDate").value,to:document.getElementById("toDate").value,school_id:document.getElementById("school").value});if(params.get("from")&&params.get("to")&&params.get("from")>params.get("to"))throw new Error("Ngày bắt đầu không được sau ngày kết thúc.");const response=await fetch(`${API}?${params}`),data=await response.json();if(!response.ok)throw new Error(data.error||"Không tải được số liệu y tế.");renderData(data)}
 catch(error){status.textContent=`Không tải được dữ liệu y tế: ${error.message}`;status.style.display="block"}
 finally{button.disabled=false;button.textContent="Lọc dữ liệu"}
}
async function start(){
 const status=document.getElementById("status");try{
  const formatDate=value=>`${value.getFullYear()}-${String(value.getMonth()+1).padStart(2,"0")}-${String(value.getDate()).padStart(2,"0")}`,today=new Date(),end=formatDate(today),startDate=new Date(today);startDate.setFullYear(startDate.getFullYear()-3);const start=formatDate(startDate);
  document.getElementById("fromDate").value=start;document.getElementById("toDate").value=end;
  const response=await fetch(`${API}?${new URLSearchParams({from:start,to:end})}`),data=await response.json();if(!response.ok)throw new Error(data.error||"Không tải được danh mục cơ sở.");
  const select=document.getElementById("school");data.schools.forEach(item=>{const option=document.createElement("option");option.value=item.id;option.textContent=item.name||`Cơ sở ${item.id}`;select.appendChild(option)});
  document.getElementById("filters").addEventListener("submit",loadData);renderData(data);
 }catch(error){status.textContent=`Không tải được dashboard: ${error.message}`;status.style.display="block"}
}
start();
</script>
{% endif %}
</body></html>"""


if __name__ == "__main__":
    app.run(host=os.getenv("APP_HOST", "127.0.0.1"), port=int(os.getenv("APP_PORT", "5000")), debug=False)
