# Guitar Radar

Ưu tiên hiện tại là [kiểm chứng bộ cào qua giao diện](docs/collector-verification.md): xem từng lượt, bài/thông tin thu được, so sánh cùng cửa sổ và đối chiếu bài đã biết. Mã chia thành `facebook_crawler` (thu thập) và `data_filter` (xử lý offline); `radar` điều phối, lưu trữ và hiển thị.

Ứng dụng vẫn có feed, quản lý group, PostgreSQL và lịch tùy chọn. Hướng dẫn cài đặt: [Chạy Phase 1](docs/phase1-running.md).

Trong WSL Ubuntu, với Docker Desktop/Compose đã chạy:

```bash
cd /mnt/c/projects/Guitar-Radar
bash scripts/phase1.sh setup
bash scripts/phase1.sh start
```

Mở http://localhost:8000. Tài khoản riêng của ứng dụng nằm trong `.env.phase1` được tạo khi setup. Session Facebook vẫn cấu hình trong `.env` như POC. Mặc định thu thập thủ công ở tab Nhóm theo dõi; không có Facebook request trong setup.

## Công cụ Phase 0 và Phase 0.5

POC so sánh **teslashibe/facebook-go** và **bsho5/fbgql** trên **cùng một Facebook Group**, cùng session, giới hạn trang và thứ tự `CHRONOLOGICAL`. Chỉ thu thập post, normalize và tạo report; không frontend, database, AI hay dedup cross-post.

## Trạng thái kiểm chứng

Hai adapter dùng API/parser thực của upstream đã pin. Benchmark Phase 0.5 do người dùng chạy trên `vegmpremium` đã báo `READY FOR PHASE 1` trong mẫu quan sát; facebook-go ổn định, fbgql có 1 lượt lỗi trên 10 lượt. Cloud không có session Facebook để chạy lại collection live. Lưu ý: kết quả benchmark không bảo đảm coverage hay độ tin cậy production.

`make demo` tạo dữ liệu **synthetic**, không gọi Facebook và không phải benchmark. Không dùng các số liệu này để chọn collector. Report live khi thiếu session ghi `failed`, `authentication_failed`, không phải 0 bài thành công.

## Requirements

- Linux x86_64 cho script tự cài Go; Go **1.25.5**, Python **3.11+**, `venv`, Git, curl, make. Trên Windows, chạy trong WSL Ubuntu.
- Facebook account/session hợp lệ có quyền đọc group. Không tự login, join group hay vượt checkpoint.
- HTTPS: GitHub, domain package-manager mặc định, `go.dev`, `dl.google.com`, `www.facebook.com`, `facebook.com`. Không tải ảnh nên không cần CDN ảnh.

| Collector | Commit đã pin |
|---|---|
| facebook-go | `0c762f3eea9082ed7a4694d6c474c3622546b3b6` |
| fbgql | `28e03ae84012be44d37a67014fcfcfcc420c9ee9` |

## Setup

Phase 0.5 (ảnh, cùng time window, repeated runs, stability/health): xem [hướng dẫn validation](docs/phase05-validation.md). Mặc định mới cho benchmark là **06/11/2026 00:00 giờ Việt Nam → now**; `now` được chốt một lần cho toàn bộ run. Phase 0 cũ vẫn dùng `make phase0`.

```bash
cd /workspace/Guitar-Radar
make setup
make test
make demo
```

`scripts/setup.sh` cài Go vào `.tools/` bằng archive chính thức và SHA-256 đã xác minh, tạo `.venv`, cài requirements đã pin và build `.bin/facebook-go`. Cache nằm ở `.cache/`; đặt `GUITAR_RADAR_TOOLS` hoặc `GUITAR_RADAR_CACHE` nếu muốn dùng nơi khác. Script có thể chạy lại. Không tạo Git worktree: mỗi cloud task đã có checkout cô lập.

`make test` chạy Python unittest và Go race tests, không cần cookie hay Internet. Tests bao gồm pagination hai trang, checkpoint/rate limit, lỗi parse từng post, partial result, identity fallback, file stale và bảo vệ credential. Fixture không thay thế kiểm thử Facebook live.

### Windows với WSL Ubuntu

Mở Ubuntu từ Start hoặc PowerShell bằng `wsl -d Ubuntu`, vào checkout trên ổ C:

```bash
cd /mnt/c/projects/Guitar-Radar
git config core.autocrlf false
git pull origin main
bash scripts/setup.sh
```

Chạy mỗi lệnh trên một dòng, không gõ dấu `\` ở cuối. Repository đặt `.gitattributes` để shell/Python/Makefile luôn checkout với Linux line endings. Nếu `git pull` báo file cục bộ đã đổi, giữ các thay đổi bằng stash rồi thử lại:

```bash
git stash push -m "local changes before Guitar Radar update"
git pull origin main
```

Stash chỉ cất file Git theo dõi; `.env` bị ignore vẫn nằm nguyên. Đừng chạy `git stash pop` nếu chưa cần lấy các thay đổi cũ lại. Nếu Ubuntu chưa có Python venv/curl/certificate, cài một lần bằng `sudo apt update` và `sudo apt install -y python3 python3-venv curl ca-certificates`; mật khẩu là mật khẩu Linux Ubuntu, không phải PIN Windows.

Sau setup, tạo `.env` rồi chạy:

```bash
test -f .env || cp .env.example .env
chmod 600 .env
.venv/bin/python scripts/phase0.py --group "https://www.facebook.com/groups/vegmpremium"
```

### Session an toàn

```bash
test -f .env || cp .env.example .env
chmod 600 .env
# Sửa .env bằng editor cục bộ; không dán cookie vào chat, issue hay commit.
```

Điền `FACEBOOK_COOKIE` bằng Cookie header từ session của bạn:

```env
FACEBOOK_GROUP_ID=https://www.facebook.com/groups/vegmpremium
FACEBOOK_COOKIE='c_user=<your-user-id>; xs=<your-session>; sb=<...>; datr=<...>'
FACEBOOK_USER_ID=
```

Đây chỉ là placeholder. Giữ nguyên các cookie cần thiết của session. Hai adapter dùng **cùng** header; `c_user` và `xs` bắt buộc. `FACEBOOK_USER_ID` tùy chọn, nếu điền phải khớp `c_user`. Có thể dùng `FACEBOOK_COOKIE_FILE=/private/path/cookie-header.txt`; file chứa duy nhất Cookie header, quyền `600`, nằm ngoài Git. Biến đã export ưu tiên hơn `.env`; parser không chạy shell/nội suy biến. Cookie file và `.env` được đọc bởi orchestration; gọi adapter trực tiếp cần export `FACEBOOK_COOKIE` từ nơi lưu bí mật.

**Không commit Facebook cookie, `.env`, token hoặc session data.** Không đưa chúng vào command-line arguments. Output mặc định, cookie/session/token và môi trường local được `.gitignore` bỏ qua. Output có quyền `600` nhưng chứa nội dung/author của group: vẫn cần lưu và chia sẻ có kiểm soát.

Trong cloud settings, áp dụng allowlist `facebook.com` và `www.facebook.com` đã lưu draft. Session cần cookie thật ở tiến trình để thư viện parse `c_user`/`xs`; không dùng proxy-placeholder thay Cookie header. Nếu Facebook yêu cầu checkpoint, dừng và xử lý thủ công trên Facebook.

## Chạy facebook-go và fbgql

Chạy benchmark cặp (khuyến nghị):

```bash
make phase0
make phase0 GROUP='https://www.facebook.com/groups/vegmpremium' PAGES=2 PAGE_SIZE=20 TIMEOUT=120
```

CLI nhận numeric ID hoặc Facebook URL `/groups/<id-or-alias>/`. Alias như `vegmpremium` được resolve **một lần** từ HTML bằng marker group rõ ràng; không đoán ID từ profile hay chọn bừa khi có nhiều ID. Sau đó hai collector nhận cùng numeric ID. Resolver dừng khi login/checkpoint/chặn, không bypass. Tracking query bị bỏ qua; post URL không được chấp nhận thay group URL.

Chạy riêng để debug:

```bash
make collect-facebook-go GROUP=123456789
make collect-fbgql GROUP=123456789
```

Hai lệnh riêng có run ID khác nhau, không dùng để tạo paired benchmark. Dùng `make phase0` để có cùng run ID. Adapter trực tiếp `.bin/facebook-go --help` và `.venv/bin/python collectors/fbgql/main.py --help` nhận **numeric** group ID/URL.

Lưu riêng từng lần chạy để kiểm tra độ ổn định:

```bash
.venv/bin/python scripts/phase0.py --group 'https://www.facebook.com/groups/vegmpremium' \
  --pages 2 --page-size 20 --timeout 120 \
  --data-dir data/runs/vegmpremium-1 --report-dir reports/vegmpremium-1
# Lặp lại với vegmpremium-2; dùng thư mục khác cho public group khác.
```

Hai collector chạy **tuần tự**, không crawl hàng loạt, không comments/replies, không retry khi bị chặn. Timeout là budget riêng mỗi collector; resolver có budget riêng. `duration_ms` đo adapter (bootstrap + request + parsing), không gồm build, normalization/comparison hay resolution. Với lỗi orchestration, duration là thời gian tới lỗi, không dùng như tốc độ collector.

Collector lỗi trả exit code khác 0; pipeline vẫn thử collector còn lại và tạo report khi input có thể kiểm chứng. Giữ partial data. Process bị kill/timeout ngoài adapter sẽ ghi lỗi thay vì dùng output cũ. `make` có thể trả 2 khi command bên trong thất bại.

Orchestration chuyển report lần trước vào `reports/history/<id>/` trước khi normalize/compare, và kiểm tra run ID của report mới. Nếu dùng `python -m compare` riêng và input lỗi, command trả nonzero, report cũ không bị ghi đè; luôn kiểm tra exit code và run ID.

## Comparing results

```bash
make normalize
make compare
# Với run đã lưu riêng:
.venv/bin/python -m normalizer --raw-dir data/runs/vegmpremium-1/raw --output-dir data/runs/vegmpremium-1/normalized
.venv/bin/python -m compare --input-dir data/runs/vegmpremium-1/normalized --output-dir reports/vegmpremium-1
```

Normalizer/comparator dùng Python stdlib. Không tự gọi Facebook hoặc bịa field thiếu. Cả raw và normalized là **envelope** có `schema_version`, `collector`, `source`, `run_id`, `group_id`, thời gian, `status`, `errors`, `pages_fetched`, cấu hình và mảng `posts`. Mỗi phần tử normalized có schema:

```json
{
  "collector": "facebook-go",
  "post_id": "123456789",
  "group_id": "987654321",
  "group_name": null,
  "author_id": null,
  "author_name": null,
  "text": null,
  "url": null,
  "images": [],
  "posted_at": null,
  "collected_at": "2026-11-07T12:00:00+00:00"
}
```

Timestamp chuẩn ISO 8601 UTC; zero time Go và timestamp không hợp lệ thành `null`. Raw giữ model post upstream, không lưu nguyên HTML/HTTP headers/session bootstrap có thể chứa token.

Matching ưu tiên `post_id`; nếu ít nhất một bên thiếu ID, dùng canonical Facebook permalink. Không match explicit ID khác nhau chỉ vì URL giống nhau. Report ghi post match/chỉ một bên, post không có identity và duplicate nội bộ; không semantic matching. Group/run/source lệch bị từ chối. Hai lượt thất bại trước resolution có thể cùng `group_input`, chỉ để báo blocker.

Field completeness đo **tỷ lệ có dữ liệu**, không chứng minh caption đầy đủ hay mọi ảnh đã được lấy. Report thêm khác biệt caption/độ dài, author, timestamp, URL và image sets trên post matched. Post không có ảnh có thể hợp lệ. Một lần chạy không đủ kết luận ổn định; nhiều post hơn chưa chứng minh recall tốt hơn khi chưa có ground truth.

## Output files

```text
collectors/facebook-go/    # Go adapter + tests + module lock
collectors/fbgql/          # Python adapter + tests + pinned requirements
normalizer/               # Schema mapping
compare/                  # Identity, completeness, JSON/Markdown
scripts/                  # Setup + orchestration
tests/fixtures/           # Synthetic, được commit, không phải dữ liệu thật
data/raw/{facebook-go,fbgql}.json
data/normalized/{facebook-go,fbgql}.json
reports/comparison.json
reports/comparison.md
data/demo/                # Fixture demo riêng, không đè live output
reports/demo/             # Report synthetic
```

Report có counts/duration/errors, matched/one-sided references, completeness, warnings và kết luận thận trọng. Raw lỗi vẫn có `posts: []`, `status: failed`, error code/context an toàn. Code chính: `authentication_failed`, `session_expired`, `checkpoint_required`, `rate_limited`, `group_not_accessible`, `group_not_found`, `network_error`, `parse_error`, `unknown_error`; có thêm lỗi cấu hình/dependency.

## Checklist cho benchmark bổ sung

| Kịch bản | Thực hiện | Cần xác minh |
|---|---|---|
| Public group | `phase0 --group <public ID/URL>` cùng session | Post thực, lỗi và fields |
| Group đã join | `phase0 --group <joined ID/URL>` | Quyền truy cập thực |
| Hai trang | `--pages 2`, group đủ post | `pages_fetched >= 2`, cursor thực |
| Lần hai | Lưu thư mục `*-2` | Overlap/field consistency cùng collector |
| Cookie invalid | Test local tự động; live chỉ nếu cần | Lỗi an toàn, không log cookie/bypass |

Không khẳng định `vegmpremium` public hay account đã join khi chưa xác minh. Feed có thể hết trước hai trang; không tự gọi đó là lỗi pagination.

## Known limitations

- Facebook **internal/non-official APIs** có thể thay doc ID/schema, collector có thể ngừng hoạt động. Session hết hạn, checkpoint/rate limiting vẫn có thể xảy ra. Đây chưa phải production system.
- fbgql dùng payload/parser upstream qua transport có kiểm soát, không dùng `Scraper` cấp cao vì thiếu page cap phù hợp và có comments/retry ngoài phạm vi. Model Post đã pin không có author/ảnh. Phase 0.5 bổ sung ảnh từ raw GraphQL có bằng chứng; author vẫn `null`, không lấy `page_name` làm author.
- facebook-go trả attachment URL tổng quát, có thể là link/video. Phase 0.5 đọc media từ raw story trước khi model upstream bỏ mất field; legacy output chỉ dùng image-extension fallback. Không tự đổi permalink thành ảnh.
- Parser upstream có thể âm thầm bỏ feed edge không nhận dạng được. Wrapper cô lập lỗi nó nhìn thấy, không thể đếm mọi silent omission nếu không sửa upstream; `errors: 0` không đảm bảo không bỏ sót.
- Feed live thay đổi giữa hai collector tuần tự; số trang bằng nhau chưa chắc cùng khoảng thời gian/số post. Chỉ báo khác biệt trong mẫu quan sát, không tự chọn collector tốt nhất.

Các lệnh POC phía trên chỉ tạo dữ liệu/report. Ứng dụng Phase 1 dùng lệnh riêng ở đầu README; xem [phạm vi Phase 1](docs/phase1-scope.md).
