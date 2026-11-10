# Đề xuất cải thiện giao diện kiểm chứng

Trạng thái: người dùng đồng ý dùng thư viện ngoài sau khi xem các hướng; triển khai hướng B theo đề nghị.

Mục tiêu là đọc kết quả collector nhanh hơn, xem đủ ảnh và nội dung từng bài, tìm lượt lỗi/partial và đối chiếu hai lượt cùng group/cửa sổ. Frontend hiện là HTML/CSS/JavaScript tĩnh do FastAPI phục vụ, không có bước Node build.

| Hướng | Công cụ dự kiến | Phạm vi và đánh đổi |
| --- | --- | --- |
| A — Nâng giao diện hiện tại | PhotoSwipe + Lucide | Tổ chức lại bố cục/CSS, icon thống nhất, gallery phóng to và mobile. Phạm vi nhỏ; bảng phức tạp vẫn cần tự xây. |
| B — Dashboard kiểm chứng (đề nghị) | Bootstrap 5 + Tabulator + PhotoSwipe + Lucide | Bảng có lọc/sắp xếp/phân trang, panel chi tiết, gallery, so sánh hai lượt. Giữ cách chạy FastAPI; cần thống nhất theme của các thư viện. |
| C — Frontend mới | React + TypeScript + Vite + Tailwind/shadcn + TanStack Query/Table + PhotoSwipe | Component và trạng thái dữ liệu phù hợp mở rộng nhiều màn hình. Phải viết lại frontend, thêm Node build và artifact triển khai. |

## Phạm vi đề nghị cho hướng B

- Tổng quan ưu tiên lượt lỗi/partial, bài đối chiếu thiếu và chênh lệch nội dung/số ảnh.
- Bảng lượt chạy gồm group, FROM/TO theo giờ Việt Nam, trạng thái, số bài, số lỗi, thời lượng; có tìm/lọc/sắp xếp và mở chi tiết.
- Chi tiết bài có toàn bộ caption, thông tin tác giả/thời gian/link nguồn và gallery đánh số; phóng to/đổi ảnh bằng bàn phím hoặc điện thoại.
- So sánh hai lượt cùng group, nguồn và FROM/TO; hiển thị bài thêm/mất và trường thay đổi, kèm tình trạng hoàn tất cửa sổ.
- JSON đầu vào và normalized đặt trong mục nâng cao, giữ nguyên nội dung bằng chứng.
- Loading, lỗi và trạng thái trống có hướng xử lý; kiểm tra desktop/mobile, bàn phím và màn hình nhỏ.

## Cách tích hợp thư viện

Phiên bản được dùng: Bootstrap 5.3.8, Tabulator 6.3.1, PhotoSwipe 5.4.4 và Lucide 1.70.0. Asset được lưu trong dự án để FastAPI phục vụ cùng nguồn, kèm giấy phép và manifest checksum; xem [danh sách thư viện](../radar/static/THIRD_PARTY.md). Không cần Node để chạy ứng dụng và không phụ thuộc CDN tại runtime. Giữ API, đăng nhập và dữ liệu/bản chụp hiện có; UI trình bày đúng trạng thái bằng chứng, số trường có dữ liệu và độ đầy đủ đã được đối chiếu độc lập.
