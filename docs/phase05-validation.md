# Phase 0.5 — Facebook collector validation

Phase này kiểm chứng dữ liệu của collectors, không triển khai ứng dụng Phase 1. Các số liệu 22 post Go / 8 post Python và 6 matched trong yêu cầu là **kết quả người dùng cung cấp**, chưa có raw tương ứng trong cloud checkout để tái kiểm chứng. Không đưa summary đó vào stability như một lượt chạy đã kiểm tra.

## Chạy trên WSL

Trong Ubuntu, tại `/mnt/c/projects/Guitar-Radar`, chạy mỗi lệnh một dòng:

```bash
git pull origin main
bash scripts/setup.sh
.venv/bin/python -m validation demo
```

Demo là synthetic, kết quả riêng ở `reports/validation-demo/`, luôn NOT READY FOR PHASE 1. Không dùng nó làm dữ liệu live.

Giữ `.env` đã có cookie; đừng copy `.env.example` đè lên. Nếu shell đang export cookie cũ, `unset FACEBOOK_COOKIE FACEBOOK_COOKIE_FILE FACEBOOK_USER_ID` để dùng file `.env`. Không gửi cookie trong chat/commit.

Khung yêu cầu đã xác nhận: **06/11/2026 00:00 Việt Nam → lúc bắt đầu suite**. Chạy một cặp:

```bash
.venv/bin/python -m validation window --group 'https://www.facebook.com/groups/vegmpremium' --from '2026-11-06T00:00:00+07:00' --to now
```

Chạy 10 cặp tuần tự trên cùng window (chốt `now` trước lượt đầu):

```bash
.venv/bin/python -m validation repeat --group 'https://www.facebook.com/groups/vegmpremium' --from '2026-11-06T00:00:00+07:00' --to now --runs 10 --interval 30
```

Hoặc `make benchmark-window` / `make benchmark-repeat RUNS=10`, dùng group trong `.env`. Override `GROUP_ID=... FROM=... TO=... MAX_PAGES=100 WINDOW_TIMEOUT=600` khi cần. `MAX_PAGES` là safety cap, không phải đơn vị so sánh coverage. Khi chạm cap mà chưa tới biên, report phải incomplete; không tự coi đó là feed đã hết.

Mỗi collector mặc định tối đa 50 page, 20 post/page, 300 giây; có thể điều chỉnh trong giới hạn 200 page/3600 giây. Chỉ theo dõi group được chọn. Suite dừng ngay khi auth/session/checkpoint/restriction/rate limit, không cố đổi IP, account, retry hay vượt bảo vệ. Nếu một collector gặp safety stop, collector còn lại được ghi skipped/failed, không gửi request tiếp. Exit code 1 báo collection/window chưa hoàn tất; 2 báo lỗi cấu hình/input/report. NOT READY vì chưa đủ bằng chứng không tự có nghĩa là lỗi chạy command.

## Known-image ground truth

`fixtures/known-image-posts.json` đã chứa 5 link bạn xác nhận:

| Link share/p | Có ảnh |
|---|---|
| `1FAh8qGkKx` | Có |
| `1C3tg8rCQm` | Có |
| `1HctR4htGo` | Có |
| `1JMZMYrcML` | Không |
| `1DpPjtnm6T` | Không |

Link share không chứa post ID. Runner resolve bằng cùng session, chỉ nhận canonical/og:url hoặc redirect về permalink rõ ràng. Nhiều ID/không có ID => unresolved, không đoán từ caption hay ID profile. Xem `reports/known-posts-resolved.json`; có thể thay URL bằng numeric `post_id` sau khi xác nhận. Các link cần thuộc khoảng benchmark và xuất hiện trong feed; nếu ngoài khoảng, chúng sẽ là not_observed, không phải extraction_failure.

```json
[
  {"post_id": "CONFIRMED_POST_ID", "expected_has_images": true},
  {"url": "https://www.facebook.com/groups/GROUP_ID/posts/POST_ID/", "expected_has_images": false}
]
```

Đây là placeholder format, không dùng nguyên các ID chữ đó làm fixture thật. `--known-posts path.json` chọn file khác; `KNOWN_IMAGE_POSTS` trong env/.env nhận CSV ID hoặc URL có ảnh. Ground truth không được tự suy từ extractor đang được đánh giá.

Report tách rõ known image không xuất hiện, known image đã thấy nhưng images rỗng (extraction_failure), known text-only không ảnh (expected), và text-only bị gắn ảnh (possible false positive). `image_detection_recall` là detected/all known image posts; conditional recall là detected/observed known image posts; `observation_recall` là observed/all known image posts. Không có ground truth => recall null. “Có ít nhất một ảnh” chưa xác minh lấy đủ cả album.

## Media investigation

Hai giới hạn tìm được bằng đọc source upstream đã pin:

- Go chọn `attachment.url` trước `media.image.uri`, dễ giữ permalink thay cho URL ảnh; model chỉ giữ vài attachment fields.
- fbgql Post dataclass không có image/author. Wrapper Phase 0 tìm mọi Story đệ quy, có thể biến shared original thành post riêng, và bỏ duplicate trước raw.

Phase 0.5 bọc transport Go để đọc raw GraphQL **trước** model; Python scope story vào group feed edge, giữ duplicate trong raw và dùng parser upstream cho field post. Cả hai thêm `image_candidates` có URL, width/height nếu có, source/path và `possible_type`/`type_evidence`. Normalized `images` vẫn là URL array để giữ compatibility; chi tiết ở `post.validation.image_candidates`.

Extractor dựa vào kiểu Photo/Image và vị trí attachment/media, hỗ trợ nested/carousel/comet sections, không gom mọi URL trong response. Loại avatar/profile, link preview, thumbnail video/reel, recommendation và ảnh của shared original; chưa có bằng chứng loại post thì để unknown. Phần ảnh đã được kiểm thử trên response giả lập có cấu trúc GraphQL, **chưa khẳng định hoạt động/đủ ảnh trên các 5 link live**.

## Time window và output

Raw giữ tất cả bài scan, kể cả ngoài window và thiếu timestamp. Normalizer chỉ đưa `FROM <= posted_at <= TO` vào sample. Bài thiếu timestamp vẫn giữ raw và được đếm riêng; không tự coi ngoài window. Không dừng vì một bài cũ được ghim: cần cả page có timestamp cũ hơn FROM, hoặc feed hết. Unknown timestamp, chronology violation, lỗi, missing/repeated cursor và page cap đều ngăn kết luận complete. Facebook có thể reorder/ẩn bài nên complete là bằng chứng traversal của query, không chứng minh full recall.

```text
data/runs/<run-id>/metadata.json
data/runs/<run-id>/facebook-go.raw.json
data/runs/<run-id>/facebook-go.normalized.json
data/runs/<run-id>/fbgql.raw.json
data/runs/<run-id>/fbgql.normalized.json
data/debug/<run-id>/<collector>/<post-id>.json
reports/runs/<run-id>.json
reports/stability.json + stability.md
reports/consistency.json + consistency.md
reports/image-investigation.json + image-investigation.md
reports/collector-health.md
reports/final-report.md
reports/validation-status.json
```

Run ID mới mỗi lần, raw không ghi đè run trước. Debug chứa sanitized story GraphQL, không chứa bootstrap HTML/request/authorization headers/cookies/session tokens. Duplicate debug được giữ riêng/chain tùy adapter. Sanitization bỏ sensitive fields và token query/secret echoes; nội dung/author/ảnh vẫn là dữ liệu của group, chỉ chia sẻ có kiểm soát. Không tải byte ảnh. File private0600; Git ignore data/reports/cookie/session.

Metadata ghi run/group, UTC from/to/started_at, source, upstream commit và Git commit hiện tại. Duration bao gồm bootstrap/fetch/parse/debug của adapter, không gồm build, resolver group/share hoặc report. So latency với debug cùng bật trên cả hai; không so với Phase 0 như điều kiện đo giống nhau.

Report hiện hành được cất vào `reports/history/validation-.../` trước suite mới. `validation-status.json` cho biết running/completed/failed; không dùng health cũ làm kết quả lần chạy mới. Muốn phân tích lại raw đã lưu (không gọi Facebook):

```bash
.venv/bin/python -m validation analyze --run-ids 'RUN_ID_1,RUN_ID_2,RUN_ID_3' --known-posts fixtures/known-image-posts.json
```

Phải chọn cùng group và cùng frozen window; analyze từ chối trộn window/group/run-ID trùng. Mặc định analyze dùng known-post resolutions đã lưu trong metadata của run, không resolve mạng. Nếu muốn override rõ ràng, truyền `--known-posts reports/known-posts-resolved.json` hoặc file đã xác nhận khác; share link mới chưa resolve vẫn phải báo chưa quan sát.

## Quyết định

Mỗi run có status/duration/pages/raw rows/sample rows/unique/identifiable/duplicates/errors/field presence, matched/one-sided/ambiguous/unidentifiable. Collector-only records có type/raw evidence, nguyên nhân mặc định unresolved. Repeated consistency liệt kê added/removed và text/author/canonical URL/timestamp/image-count thay đổi; không tự gọi thay đổi Facebook là bug.

Ranking theo success rate → observed union coverage → URL → author → timestamp → known image recall → latency. Coverage là union hai collector quan sát, không phải tổng post thật của group; tie không chọn bừa. READY yêu cầu ít nhất 3 complete paired live runs, mọi required field/identity, confirmed images đã thấy và detect, consistency không còn vấn đề và không còn collector-only omission chưa giải thích. Đây là ngưỡng tối thiểu thận trọng, không phải cam kết production. `primary_candidate` có thể có trong khi `primary` vẫn null/NOT READY.

`final-report.md` trả lời A–F với evidence/blockers. Không chuyển sang Phase 1 tự động. Cloud hiện chưa có cookie/raw benchmark người dùng nên phần live phải chạy trên WSL của bạn trước khi kết luận.
