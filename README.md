# K12 Database Browser

Ứng dụng Flask chạy cục bộ để duyệt bảng PostgreSQL, xem trước dữ liệu, thêm/sửa/xóa bản ghi và cung cấp JSON cho Power BI.

## Chạy ứng dụng

Mở PowerShell tại thư mục này:

```powershell
python -m pip install -r requirements.txt
$env:PGHOST = "localhost"
$env:PGPORT = "5432"
$env:PGUSER = "postgres"
$env:PGDATABASE = "K12_Staging_Local_Restore"
# Nếu PostgreSQL yêu cầu mật khẩu, đặt biến môi trường trước khi chạy:
# $env:PGPASSWORD = "<mật khẩu PostgreSQL>"
python app.py
```

Mở trang `http://127.0.0.1:5000`. Mặc định ứng dụng chỉ lắng nghe trên máy hiện tại.

Chọn một bảng để xem dữ liệu. Nút **Thêm dòng** mở biểu mẫu tạo bản ghi; các nút **Sửa/Xóa** xuất hiện khi bảng có khóa chính. Cột tự tăng/được sinh tự động được để PostgreSQL quản lý. Ràng buộc khóa ngoại, NOT NULL và unique của PostgreSQL vẫn được áp dụng; lỗi sẽ hiện ngay trong biểu mẫu.

Trang dashboard chung, chọn đối tượng sử dụng: `http://127.0.0.1:5000/dashboard`.

Dashboard Ban giám hiệu: `http://127.0.0.1:5000/dashboard/ban-giam-hieu`. Dashboard có 5 biểu đồ, mặc định chọn 3 năm học gần nhất có dữ liệu đã chốt; bấm cột hoặc điểm để xem số liệu chi tiết.

Dashboard Trưởng phòng Kế toán: `http://127.0.0.1:5000/dashboard/truong-phong-ke-toan`. Dashboard có bộ lọc năm học, học kỳ, cơ sở; bấm cột hoặc điểm biểu đồ để xem giao dịch/chi tiết hoàn phí tương ứng.

## Lấy dữ liệu trong Power BI Desktop

Chọn **Get Data → Web**, rồi nhập URL JSON của bảng muốn dùng. Ví dụ:

```text
http://127.0.0.1:5000/api/powerbi?schema=bus&table=bus_route&limit=100000&offset=0
```

API trả mảng JSON gồm các bản ghi có tên trường phù hợp với cột. Tối đa 100.000 dòng mỗi lần gọi; dùng `limit` và `offset` để lấy các phần tiếp theo.

Để dùng dữ liệu học phí đã tổng hợp theo trường-tháng, kết nối URL sau:

```text
http://127.0.0.1:5000/api/powerbi/tuition-monthly
```

Endpoint này không trả mã học sinh. Phân tích nghiệp vụ, định nghĩa chỉ số và lưu ý về kỳ kế hoạch được ghi tại [TUITION_BUSINESS_ANALYSIS.md](TUITION_BUSINESS_ANALYSIS.md).

Để tự dựng biểu đồ kế toán trong Power BI, chọn **Get Data → Web** và nạp riêng từng mảng JSON sau:

```text
http://127.0.0.1:5000/api/accounting/dashboard?dataset=transactions
http://127.0.0.1:5000/api/accounting/dashboard?dataset=refunds
```

`transactions` trả giao dịch với năm học, học kỳ/tháng, cơ sở, phương thức, ghi nợ/ghi có và trạng thái; `refunds` trả chi tiết hoàn phí theo lớp, dự án dịch vụ và ngày. Cả hai nguồn được giới hạn ở ba năm học gần nhất. Dữ liệu hoàn phí có tên học sinh, chỉ dùng trong báo cáo kế toán được phân quyền phù hợp.

## API

- `/api/tables?q=bus` — tìm bảng theo tên schema hoặc bảng.
- `/api/columns?schema=bus&table=bus_route` — xem các cột và kiểu dữ liệu.
- `/api/data?schema=bus&table=bus_route&limit=100&offset=0` — xem dữ liệu dạng JSON; `limit` tối đa 1.000.
- `/api/powerbi?schema=bus&table=bus_route&limit=100000&offset=0` — JSON record list cho Power BI Web connector.
- `/api/powerbi/tuition-monthly?from=2026-01-01&to=2026-09-01&school_id=1` — dữ liệu học phí tổng hợp theo trường-tháng cho Power BI; mọi bộ lọc đều tùy chọn.
- `/export.csv?schema=bus&table=bus_route` — tùy chọn xuất CSV.
- `POST /api/rows` — thêm dòng; body JSON gồm `schema`, `table`, `values`.
- `PUT /api/rows` — sửa dòng; body JSON gồm `schema`, `table`, `key` (các cột khóa chính) và `values`.
- `DELETE /api/rows` — xóa dòng theo `schema`, `table` và `key`.

Các thao tác thêm/sửa/xóa chỉ áp dụng cho bảng thường có thể ghi; sửa và xóa yêu cầu khóa chính. Tên schema/bảng được trích dẫn an toàn trước khi truy vấn.
