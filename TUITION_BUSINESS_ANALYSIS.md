# Phân tích nghiệp vụ học phí và Business Goals

## Hiện trạng

- Trong README và ứng dụng hiện có mục tiêu kỹ thuật là cho phép Power BI đọc dữ liệu PostgreSQL qua JSON/CSV.
- Chưa tìm thấy business goal học phí đã được phê duyệt, KPI mục tiêu hay target số trong tài liệu/repository. Các mục tiêu bên dưới là đề xuất để Finance/ban điều hành xác nhận, không phải cam kết đã thống nhất.
- Database `K12_Staging_Local_Restore` đang kết nối được. Bảng `public.tuition_fee_debt_record_by_month` có 62.457 dòng chưa xóa; endpoint tổng hợp thành 74 dòng trường-tháng từ 08/2024 đến 08/2028.
- Dữ liệu có các tháng tương lai so với ngày kiểm tra 01/10/2026. Đây có thể là kỳ học phí đã lập kế hoạch, không được diễn giải thành thực thu.

## Business Goals đề xuất

| Goal | Chỉ số theo dõi | Trạng thái |
|---|---|---|
| Giảm số dư công nợ học phí cuối kỳ và số học sinh còn dư nợ | `closing_debt_amount`, `students_with_closing_debt` theo trường-tháng | Đề xuất; Finance xác nhận kỳ chốt và target |
| Cải thiện tiến độ thu học phí | `payment_amount` so với `net_payable_amount` theo trường-tháng | Đề xuất; xác nhận cách phân bổ thanh toán và mẫu số trước khi gọi là tỷ lệ thu |
| Tăng khả năng kiểm soát điều chỉnh và hoàn tiền | `refund_amount`, `additional_adjustment_amount`, `refund_adjustment_amount` | Đề xuất; cần thống nhất ngưỡng và quy trình xử lý ngoại lệ |
| Chuẩn hóa báo cáo học phí đa trường | Cùng một grain, định nghĩa và ngày chốt trên báo cáo Power BI | Đang triển khai kỹ thuật; chưa có SLA/target nghiệp vụ |

Không đặt target phần trăm trước khi Finance duyệt baseline, kỳ chốt (tháng hay năm học), phạm vi trường và cách xử lý khoản thu bù công nợ kỳ trước.

## Baseline dữ liệu

Tại ngày kiểm tra 01/10/2026, kỳ đã đóng gần nhất có `month_start = 2026-09-01`; số liệu tổng hợp 2 trường:

- Phải thu ròng (`net_payable_amount`): 2.068.806.400
- Thanh toán ghi nhận (`payment_amount`): 126.650.000
- Dư nợ cuối kỳ theo trường nguồn (`closing_debt_amount`): 5.614.671.985,2
- Học sinh có dư nợ (`students_with_closing_debt`): 331

Đây là baseline quan sát từ staging, chưa phải số liệu tài chính đã đối soát. `closing_debt_amount` là tổng trường `last_debt_balance`; không tính lại từ các thành phần. Không cộng số dư cuối kỳ qua nhiều tháng vì mỗi tháng là một snapshot. `payment_amount / net_payable_amount` hiện chỉ có thể là phép so sánh thô, không khẳng định tỷ lệ thu nếu tiền thu có thể trả công nợ kỳ trước hoặc vượt số phải thu kỳ này.

## Dataset cho Power BI

Kết nối **Get Data → Web** tới:

```text
http://127.0.0.1:5000/api/powerbi/tuition-monthly
```

Có thể lọc ngay ở URL, ví dụ `?from=2025-08-01&to=2026-09-01&school_id=1`. Mỗi dòng là một trường-tháng; join chiều tháng theo `school_id + month_id`. Endpoint loại bản ghi đã xóa và không xuất `user_code`/mã học sinh.

Trường dữ liệu:

- Chiều: `school_id`, `school_name`, `month_id`, `month_name`, `month_start`, `month_end`, `school_term_id`, `term_name`, `school_year_id`, `school_year`.
- Quy mô: `student_count`, `students_with_closing_debt`.
- Số tiền: `opening_debt_amount`, `net_payable_amount`, `payment_amount`, `refund_amount`, `additional_adjustment_amount`, `refund_adjustment_amount`, `closing_debt_amount`.

Visual gợi ý cho phiên bản đầu: KPI dư nợ và số học sinh còn dư nợ của kỳ đã chọn; biểu đồ xu hướng phải thu/thanh toán theo tháng; dư nợ cuối kỳ theo tháng; bảng so sánh trường; biểu đồ điều chỉnh/hoàn tiền. Đặt bộ lọc ngày để loại tháng tương lai khỏi trang thực tế; nếu cần thì tạo trang riêng cho kế hoạch.

Khi làm measure tổng dư nợ, chỉ lấy snapshot của tháng được chọn (hoặc tháng đã đóng gần nhất), không `SUM` dư nợ qua toàn bộ lịch sử. Trước khi công bố tỷ lệ thu, đối soát `payment_amount` với sổ giao dịch và thống nhất chính sách phân bổ tiền thu.

Power BI Desktop phải truy cập được máy chạy Flask. URL loopback `127.0.0.1` chỉ dùng cục bộ; muốn làm mới từ Power BI Service cần triển khai API có xác thực trong mạng được phép hoặc cấu hình on-premises data gateway, không mở app staging công khai.