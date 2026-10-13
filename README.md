# K12 School Health Dashboards

Ứng dụng Flask gồm 11 dashboard y tế theo đối tượng sử dụng và trình duyệt bảng PostgreSQL dùng chung.

## Chạy ứng dụng

Mở PowerShell tại thư mục dự án:

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

Mở trang chọn đối tượng: <http://127.0.0.1:5000/dashboard>.

## 11 dashboard theo đối tượng

| Đối tượng | Đường dẫn |
|---|---|
| Ban giám hiệu | `/dashboard/ban-giam-hieu` |
| Trưởng bộ phận y tế học đường | `/dashboard/truong-bo-phan-y-te` |
| Nhân viên y tế / y tá trường học | `/dashboard/nhan-vien-y-te` |
| Bác sĩ / đơn vị khám sức khỏe | `/dashboard/bac-si-don-vi-kham` |
| Phụ trách BHYT học sinh | `/dashboard/phu-trach-bhyt` |
| Điều phối tiêm chủng | `/dashboard/dieu-phoi-tiem-chung` |
| Phụ trách quản lý thuốc tại trường | `/dashboard/quan-ly-thuoc` |
| Giáo viên chủ nhiệm | `/dashboard/giao-vien-chu-nhiem` |
| Phụ huynh / người giám hộ | `/dashboard/phu-huynh` |
| Học sinh | `/dashboard/hoc-sinh` |
| Phòng công tác học sinh | `/dashboard/cong-tac-hoc-sinh` |

Mỗi trang có mục tiêu nghiệp vụ, bộ chỉ số và biểu đồ phù hợp với vai trò. Trang bác sĩ/đơn vị khám, giáo viên chủ nhiệm, phụ huynh và học sinh đang khóa dữ liệu cho đến khi ứng dụng có đăng nhập, phân quyền theo lớp/học sinh và kiểm soát truy cập phù hợp.

## Dữ liệu và bảo mật

Dashboard tổng hợp hồ sơ sức khỏe, khám nhanh, khám định kỳ, BHYT, sự kiện tiêm chủng và hồ sơ thuốc từ các bảng `medical_*`. API dashboard chỉ trả số liệu tổng hợp, không trả tên học sinh, triệu chứng, chẩn đoán hoặc thông tin liên hệ. Một số trường hợp chưa có dữ liệu nguồn (ví dụ bản ghi tiêm chủng) sẽ hiển thị 0; không suy ra tỷ lệ bao phủ nếu chưa có mẫu số học sinh đủ điều kiện.

Dữ liệu sức khỏe là dữ liệu nhạy cảm. Ứng dụng hiện chưa có xác thực người dùng và phân quyền, vì vậy chỉ chạy trên máy cục bộ và không dùng để cung cấp hồ sơ cá nhân.

## Trình duyệt PostgreSQL và API dùng chung

Trang gốc <http://127.0.0.1:5000/> cho phép tìm bảng, xem dữ liệu, sửa/xóa theo khóa chính và xuất CSV. Các API dùng chung:

- `/api/tables?q=medical` — tìm bảng theo schema hoặc tên bảng.
- `/api/columns?schema=public&table=medical_quick_examinations` — xem cấu trúc bảng.
- `/api/data?schema=public&table=medical_quick_examinations&limit=100&offset=0` — xem dữ liệu JSON.
- `/api/powerbi?schema=public&table=medical_quick_examinations&limit=100000&offset=0` — dữ liệu JSON cho Power BI Web connector.
- `/api/medical/dashboard?from=2024-01-01&to=2026-12-31&school_id=3` — số liệu y tế tổng hợp theo ngày và cơ sở.
- `/export.csv?schema=public&table=medical_quick_examinations` — xuất CSV.
- `POST /api/rows`, `PUT /api/rows`, `DELETE /api/rows` — thao tác trên bảng PostgreSQL thường có thể ghi.
