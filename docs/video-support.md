# Metadata video trong bài Facebook

Ứng dụng dùng `facebook-go` qua `facebook_crawler` để nhận diện video từ metadata attachment của bài. `data_filter` chuẩn hóa thành mảng `videos` tách khỏi mảng `images`; PostgreSQL lưu metadata và giao diện hiển thị trong chi tiết bài. Không tải video về máy, chuyển mã hoặc tự phát.

## Dữ liệu và cách hiển thị

Mỗi video được nhận diện có thể có `video_id`, `kind` (`direct`, `reel`, `shared` hoặc `live`), `thumbnail_url`, `duration_seconds`, `width`, `height` và `playback_url`. Trường thiếu trong dữ liệu nguồn được giữ là `null`; không dựng URL phát từ ID, thumbnail hoặc permalink. `playback_url_expires_at` chỉ có giá trị nếu nguồn cho biết thời điểm hết hạn. URL phát có thể là URL tạm, kể cả khi không biết thời điểm hết hạn; trạng thái này được ghi riêng. URL của **bài Facebook gốc** vẫn là permalink để người dùng mở bài.

Thumbnail video không tính là ảnh của bài. Gallery và bộ lọc “có ảnh” vẫn chỉ dùng ảnh được xác định là ảnh. Bài cũ không có `videos` được đọc như danh sách rỗng. Khi không có URL phát hợp lệ hoặc URL đã hết hạn, giao diện giữ thumbnail và metadata đã có, báo không phát được và cho mở bài Facebook gốc. Khi có URL hợp lệ, trình phát có điều khiển, tắt autoplay và dùng `preload="metadata"`.

URL phát và thumbnail được dùng trực tiếp từ nguồn trong trình duyệt, không qua proxy hay CDN của ứng dụng. URL media có thể hết hạn hoặc bị Facebook từ chối theo phiên, quyền xem hoặc thay đổi của nền tảng. Việc nhận diện video không chứng minh rằng mọi video trong group đều được lấy, và URL phát không phải permalink lâu dài.

Trình phát dùng các URL HTTPS trên tên miền con của `fbcdn.net` và giữ nguyên tham số chữ ký. Collector đọc URL phát trực tiếp ở metadata video, kể cả danh sách `progressive_urls` lồng trong dữ liệu của feed nhóm. Các nguồn chỉ có manifest DASH hoặc cần gửi cookie hay header riêng sẽ dùng link bài gốc. Khi lượt thu thập mới không có ứng viên video, ứng dụng giữ metadata đã quan sát ở lượt trước.

## Phạm vi đã kiểm chứng

| Tình huống | Mức kiểm chứng |
| --- | --- |
| Video gắn trực tiếp, reel, video chia sẻ và livestream có marker attachment rõ ràng | Fixture tổng hợp và kiểm tra offline về nhận diện và metadata. |
| Thumbnail video, preview link ngoài, bài chỉ có ảnh hoặc chữ | Fixture tổng hợp và kiểm tra offline để không đếm thumbnail/preview thành ảnh hay suy ra URL phát. |
| Phát video và mở bài gốc khi thiếu URL hoặc lỗi phát | Chromium phát một video WebM tổng hợp qua URL CDN được chặn và mô phỏng cục bộ. Kiểm tra xác nhận chữ ký giữ nguyên qua PostgreSQL/API, trình phát có điều khiển và không tự phát, cùng link bài gốc khi lỗi. |
| Phát video từ Facebook thật | Hai bài trong một nhóm đã được thu thập lại, lưu URL phát từ `progressive_urls` và phát thành công trong Chrome trên Windows. |
| Các dạng attachment khác, video trong bình luận hoặc nguồn ngoài | Chưa xác minh hoặc chưa hỗ trợ. |

`fbgql` tiếp tục chuẩn hóa bài vào cùng định dạng với `videos: []`. Collector này có thể phân loại bài kiểu `video` hoặc `reel`, nhưng hiện chưa xuất ID video, thumbnail video, thời lượng, kích thước, URL phát hoặc thời điểm hết hạn của URL.

## Kiểm tra offline và trên máy của bạn

Sau khi pull, chạy trong WSL Ubuntu. Nếu thiếu `make` hoặc trình biên dịch C cho `go test -race`, cài gói `build-essential` trước.

```bash
cd /mnt/c/projects/Guitar-Radar
bash scripts/phase1.sh setup
make test
```

Để chạy kiểm thử PostgreSQL và trình duyệt, đặt `RADAR_TEST_DATABASE_URL` trỏ tới database thử riêng có quyền tạo schema. Sau đó chạy:

```bash
.venv/bin/python -m pip install -r tests/requirements-browser.txt
.venv/bin/python -m playwright install --with-deps chromium
make test
.venv/bin/python tests/phase1_browser_smoke.py
```

Nếu muốn đối chiếu một bài thật, dùng session đã lưu cục bộ trong `.env` hoặc `FACEBOOK_COOKIE_FILE`, rồi tự chạy collection với cửa sổ thời gian chứa bài. Ví dụ:

```bash
.venv/bin/python -m facebook_crawler \
  --group 'https://www.facebook.com/groups/<group-id>' \
  --from '2026-11-01T00:00:00+07:00' \
  --to '2026-11-02T00:00:00+07:00' \
  --output data/video-check.json
.venv/bin/python -m data_filter --input data/video-check.json --output data/video-check-normalized.json
```

Đổi group và cửa sổ theo bài cần kiểm tra, rồi mở output private hoặc chạy một lượt thu thập trong giao diện để xem chi tiết bài. Các file trong `data/` bị Git bỏ qua và chứa nội dung group: chỉ lưu cục bộ, không đưa session, URL phát có chữ ký hoặc output này vào issue/chat/report công khai. Không cần gửi cookie hay token cho người phát triển.
