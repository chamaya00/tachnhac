# Tách nhạc — MVP

Web app tách giọng hát khỏi nhạc nền. Backend chạy GPU serverless trên Modal, frontend là một file HTML tĩnh trên Vercel.

## Kiến trúc

```
index.html (Vercel, tĩnh)
   │
   ├─ POST /jobs        (multipart — thả file)
   │                         └──spawn──────────────┐
   │                                               │
   ├─ POST /jobs/link   (JSON — dán link)          │
   │                         └──spawn──▶ fetch_and_separate()  [CPU]
   │                                        │ yt-dlp + ffmpeg  │
   │                                        └──spawn───────────┤
   ▼                                                           ▼
Modal ASGI endpoint                              separate()  [GPU A10G]
   │  GET /jobs/{id}                                   │ audio-separator
   │  GET /jobs/{id}/stems/{name}                      │ BS-Roformer / HTDemucs
   │  GET /jobs/{id}/source   (bản gốc chưa tách)      │
   ▼                                                   ▼
modal.Dict (trạng thái)                     modal.Volume (weights + stem)
```

Việc tải nhạc nằm ở container CPU riêng, không phải trong `separate()`: tải một
bài mất 10–60 giây, giữ GPU A10G rảnh rỗi trong quãng đó là đốt tiền vô ích.

Không cần S3/R2 cho bản đầu — Modal Volume làm luôn phần lưu trữ tạm.

## Deploy (làm được hoàn toàn từ iPhone)

### 1. Modal — backend

Workflow `.github/workflows/deploy.yml` đã có sẵn trong repo, chỉ cần cắm token:

1. Đăng ký tại modal.com, tạo API token trong dashboard.
2. Trong repo GitHub: **Settings → Secrets and variables → Actions → New repository secret**,
   thêm `MODAL_TOKEN_ID` và `MODAL_TOKEN_SECRET`.
3. Vào tab **Actions → Deploy Modal → Run workflow** (hoặc push một commit chạm vào
   `modal_app.py`).
4. Lần build đầu mất khoảng 8–12 phút (image CUDA + torch). Log của bước
   `modal deploy` in ra URL endpoint, dạng `https://<user>--tachnhac-api.modal.run`.
   **Chép URL này lại.**

Kiểm tra nhanh: mở `https://<user>--tachnhac-api.modal.run/health` trên trình
duyệt, phải thấy `{"ok": true, ...}`.

### 2. Frontend

`index.html` và `vercel.json` nằm ngay ở gốc repo.

1. Import repo vào Vercel:
   - Framework Preset: **Other**
   - Root Directory: **để trống** (gốc repo)
   - Build Command / Output Directory: để trống
2. Deploy.
3. Mở trang, bung mục **Cấu hình backend**, dán URL Modal ở bước 1 rồi bấm **Lưu**.

URL được lưu trong trình duyệt, không phải deploy lại mỗi lần đổi. Muốn chia sẻ
link đã cấu hình sẵn thì thêm query: `https://trang-cua-ban.vercel.app/?api=https://<user>--tachnhac-api.modal.run`.

Nếu để trống ô cấu hình, trang sẽ gọi `/api` và dựa vào rewrite trong
`vercel.json` — khi đó phải tự thay `REPLACE-ME--tachnhac-api.modal.run` bằng URL
thật. **Không khuyến khích**: rewrite của Vercel giới hạn dung lượng request nên
file nhạc vài chục MB sẽ bị chặn với lỗi HTTP 413. Gửi thẳng tới Modal thì không
vướng (backend đã bật CORS `*`).

## Tải nhạc từ link

Trang bày sẵn hai cách, cùng lúc trên một trang, mỗi cách một nút riêng:

1. **Thả file từ máy** — chọn hoặc kéo thả file, như trước giờ.
2. **Dán link YouTube / Spotify / TikTok** — máy chủ tự tải bài về rồi tách.

Cố tình không dùng tab: tab nào cũng phải chọn sẵn một cái, mà chọn sẵn thì có
lúc phải tự chuyển — người dùng thấy trang tự nhảy và tưởng hỏng.

| Nguồn | Cách xử lý |
|---|---|
| YouTube (`youtube.com`, `youtu.be`, `music.youtube.com`) | yt-dlp tải thẳng luồng audio tốt nhất, ffmpeg chuyển sang MP3 192 kbps. |
| Spotify (`open.spotify.com/track/…`, `spotify.link`) | Đọc tên bài + nghệ sĩ từ metadata công khai, rồi tìm đúng bài đó trên YouTube. |
| TikTok (`tiktok.com`, `vm.tiktok.com`, `vt.tiktok.com`) | Tải thẳng như YouTube. Link rút gọn `vm./vt.` được yt-dlp tự lần theo chuyển hướng. |

Spotify không phát audio ra ngoài trình phát của họ nên không có đường tải thẳng
— cách trên cũng là cách `spotdl` vẫn làm. Hệ quả: bản lấy về là bản trên
YouTube, có thể là live, cover hay remaster khác với bản trong playlist. Nghe thử
ở mixer trước khi tải track về.

Với TikTok, tiếng trong video đã qua một lần nén khi đăng lên, nên tách ra không
sạch bằng file gốc — dội lại, méo tiếng ở dải cao. Không phải model chạy sai, mà
là chất lượng đầu vào. Bù lại TikTok không chặn IP máy chủ gắt như YouTube, nên
khả năng tải được cao hơn.

**Giới hạn**

- Tối đa 12 phút mỗi bài (`MAX_SOURCE_SECONDS` trong `modal_app.py`). Dài hơn thì
  cắt ngắn rồi tải file lên.
- Chỉ nhận link **một bài**. Album, playlist, podcast đều bị từ chối ngay.
- Link ngoài ba nguồn trên bị chặn ở cả frontend lẫn backend. Hai đầu dùng cùng
  một danh sách host, có test đối chiếu để không đầu nào nhận thứ đầu kia chặn.

### Khi YouTube đòi "xác nhận không phải robot"

Modal chạy trên IP trung tâm dữ liệu, mà YouTube đối xử với dải IP đó gắt nhất.
Đây là hạn chế của chỗ đặt máy chủ, không phải lỗi cấu hình — **không có cờ nào
bật lên là hết**.

Mục này chỉ nói về YouTube (và link Spotify, vì cuối cùng cũng tải từ YouTube).
TikTok không dùng chuỗi client này — `extractor_args` gắn khoá `youtube` nên thử
lại chỉ tốn thời gian; TikTok hỏng thì báo ngay.

**Backend đã tự làm sẵn** (không cần bạn động tay): khi trúng màn chặn bot, nó
thử lần lượt các player client `tv` → `android_vr` → `web_embedded`. Theo tài
liệu PO Token của yt-dlp, ba client này không cần PO token nên còn cơ may lọt
khi không có cookie. Lỗi không phải chặn bot (video riêng tư, bài quá dài) thì
báo ngay, không thử lại cho phí thời gian.

**Chuỗi khi có cookie ưu tiên client CẦN PO token** (`web` → `web_safari` →
`mweb`), rồi mới tới hai client không cần token làm dự phòng (`web_embedded`,
`android_vr`). Lý do: máy sinh PO token chỉ được gọi tới khi client thật sự cần
nó — chuỗi cũ toàn client không cần token, nên provider dựng xong rồi nằm không.

`tv` bị loại khỏi chuỗi có cookie: khi đã đăng nhập, yt-dlp đổi nó thành
`tv_downgraded`, mà client đó đang hỏng. Đo tận nơi bằng `/diag/formats?client=tv`
cũng ra đúng *"The page needs to be reloaded"*. Chuỗi không cookie vẫn giữ `tv`.

**Khi đã nạp cookie thì đi chuỗi khác.** Với tài khoản đã đăng nhập, yt-dlp mặc
định chọn client `tv_downgraded`, mà client đó đang hỏng — YouTube trả về *"The
page needs to be reloaded"*. Nghịch lý: nạp cookie xong lại hỏng theo kiểu mới.
Nên khi thấy có file cookie, backend ép `player_client=default,web_embedded`
theo đúng cách maintainer yt-dlp khuyên, để né `tv_downgraded`.

**yt-dlp luôn được cài lại bản mới nhất mỗi lần deploy.** Layer pip của Modal có
cache, giữ nguyên spec thì bản yt-dlp bị đóng băng ở thời điểm build đầu tiên —
mà YouTube đổi cách chặn gần như hàng tuần, nên một bản đứng yên là hỏng vĩnh
viễn. Vì vậy `yt-dlp` nằm ở layer cuối với `force_build=True`. Cái giá: mỗi lần
deploy tốn thêm khoảng 20 giây; layer nặng (torch, CUDA) vẫn dùng cache.

Gặp lỗi *"The page needs to be reloaded"* thì việc đầu tiên là **deploy lại**,
để lấy bản yt-dlp mới nhất.

Hết chuỗi đó mà vẫn bị chặn thì còn ba lựa chọn:

| Cách | Công sức | Bền được bao lâu |
|---|---|---|
| Tải bài về máy rồi dùng Cách 1 trên trang | Không có gì | Mãi mãi |
| Nạp cookie (dưới đây) | Cần máy tính một lần | Vài tuần, rồi phải nạp lại |
| Proxy dân cư | Tốn tiền hằng tháng | Bền nhất, nhưng phải trả phí |

Với nhu cầu thỉnh thoảng tách một bài, **cách đầu tiên là hợp lý nhất**. Cookie
chỉ đáng làm nếu bạn dùng thường xuyên và ngại thao tác tải về mỗi lần.

Đổi chỗ đặt backend cũng không cứu được: mọi nhà cung cấp serverless (Modal,
Fly, Render, Lambda…) đều là IP trung tâm dữ liệu. Chỉ máy chạy trên mạng gia
đình mới có IP dân cư.

#### Kết luận sau khi đo tận nơi

Đã chạy hết mọi cách và đo bằng `/diag/formats`. Kết quả trên một tài khoản đã
đăng nhập hợp lệ, yt-dlp mới nhất, PO token sinh được thật:

```json
{"client": "web", "cookies": true, "title": "<đúng tên bài>",
 "format_count": 3, "audio_count": 0,
 "sample": [{"id": "sb0", "note": "storyboard"}, ...]}
```

Ba format trả về đều là **storyboard** — ảnh thu nhỏ. Phần `streamingData`, nơi
chứa mọi luồng âm thanh và hình, **trống rỗng**.

Nghĩa là YouTube nhận yêu cầu, trả đúng metadata, nhưng từ chối giao luồng phát
cho IP này. Không phải yt-dlp lọc sai, không phải thiếu token, không phải chọn
nhầm client — `missing_pot` cũng vô nghĩa vì không có gì để mở khoá.

**Biến duy nhất còn lại là địa chỉ IP.** Những cách sau đều đã thử và KHÔNG
giải quyết được: nạp cookie, đổi player client (`web`, `web_safari`, `mweb`,
`web_embedded`, `tv`, `android_vr`), cập nhật yt-dlp, `formats=missing_pot`, và
cài máy sinh PO token bằng BotGuard.

Còn đúng hai đường: **proxy dân cư** (đường cắm đã có sẵn, chỉ cần nạp địa chỉ),
hoặc **tải file về máy rồi dùng Cách 1**. TikTok và Spotify không vướng chuyện
này.

#### Soi danh sách format YouTube trả về

`<URL backend>/diag/formats?url=<link>` chạy thẳng phần lấy thông tin rồi in ra
YouTube thật sự trả về những format nào.

Có endpoint này vì thông báo "không có format" không phân biệt được hai ca hoàn
toàn khác nhau: YouTube trả về danh sách **rỗng**, hay có trả về nhưng yt-dlp
**lọc sạch**. Cách chữa của hai ca đó khác hẳn nhau.

Tham số: `client=tv` (hoặc `tv,web_embedded`) để ép một player client cụ thể,
`cookies=false` để thử không cookie. Mất vài chục giây mỗi lần gọi.

Trong kết quả, `format_count` là tổng số, `audio_count` là số format có tiếng,
và `note` của từng dòng thường là chỗ yt-dlp nói thật lý do nó gạt format đó đi.
Không trả về cookie hay token nào.

#### Lần thử cuối: bỏ cookie ra

Hết chuỗi client mà vẫn hỏng, backend thử nốt một lần **không dùng cookie**.

Nghe ngược đời, nhưng cookie là con dao hai lưỡi: nó giải được cửa chặn bot,
đổi lại YouTube phục vụ phiên đăng nhập bằng một đường khác — có lúc đường đó
không kèm luồng tải được. Chưa bỏ cookie ra thì chưa thể kết luận cookie là bạn
hay là thù.

Lần thử này hiện trong thông báo lỗi dưới tên `không cookie`.

#### Khi vẫn không lấy được format nào

YouTube đang siết dần việc bắt buộc **PO token** — một chuỗi do JavaScript của
chính họ sinh ra trong trình duyệt thật, để chứng minh yêu cầu đến từ chỗ hợp
lệ. yt-dlp không tự tạo được.

Thiếu nó thì mọi format bị lọc sạch, kể cả khi cookie đã đăng nhập thành công:
qua được cửa, vào được nhà, nhưng không lấy được gì ra.

Backend đã bật `formats=missing_pot` để dùng cả những format thiếu token. Cách
này thường đủ, nhưng các format đó có thể bị bóp băng thông hoặc đứt giữa chừng.

**Đã cài sẵn máy sinh PO token** (`bgutil-ytdlp-pot-provider`): image kèm Node
22 và bản build của BotGuard ở `/opt/bgutil/server`. Chạy kiểu *script* — mỗi
lần gọi bung một tiến trình Node rồi tắt, không có máy chủ nền chạy suốt, hợp
với container tạm của Modal. Bản thân nó cache token 6 giờ.

Kiểm tra: mở `<URL backend>/diag/pot`, phải thấy `"ready": true`.

```json
{"script_exists": true, "node_version": "v22.23.2", "node_ok": true,
 "yt_dlp": "2026.08.19", "plugin_import": true, "ready": true}
```

Thêm `?live=1` để **chạy thử sinh một token thật** (mất vài chục giây):
`<URL backend>/diag/pot?live=1`. `"token_generated": true` là BotGuard hoạt
động; `false` kèm `live_stderr` là Google từ chối BotGuard từ IP máy chủ này —
giới hạn của chỗ đặt máy chủ, không sửa được bằng code. Dù thế nào cũng chỉ trả
về độ dài token, không bao giờ trả về chính chuỗi đó.

`ready` chỉ nói **đã cài đủ**, không nói **sinh được token** — hai chuyện khác
nhau, và nhìn từ ngoài chúng giống hệt nhau. Đó là lý do có `?live=1`.

`ready: false` thì xem khoá nào sai — thiếu file build, Node cũ hơn v22, hay
plugin không nạp được — rồi deploy lại. Endpoint này chỉ báo trạng thái, không
trả về token nào.

Có endpoint này vì kiểu hỏng tệ nhất ở đây là hỏng **âm thầm**: thiếu Node hay
thiếu file build thì plugin lặng lẽ bỏ qua, yt-dlp vẫn chạy và vẫn báo "không
có format" y hệt như khi chưa cài gì.

#### Proxy dân cư

Backend đọc địa chỉ proxy ở `/data/proxy.txt` (hoặc `/models/proxy.txt`) và cắm
thẳng vào yt-dlp. Chưa có file thì bỏ qua.

Mua proxy dân cư ở đâu thì tuỳ bạn — repo này không đính kèm nhà cung cấp nào.
Có địa chỉ rồi: tạo secret `YTDLP_PROXY` (dạng `http://user:pass@host:port`),
rồi chạy workflow **Nạp cookie YouTube** với ô **Nạp cả proxy** bật lên.

#### Nạp cookie là gì

yt-dlp mang theo cookie đăng nhập của bạn để YouTube tưởng đây là trình duyệt
của một người thật đã đăng nhập, thay vì một máy chủ lạ. `cookies.txt` chính là
trạng thái đăng nhập đó xuất ra file, ở định dạng Netscape.

> **Đọc trước khi làm.** Cookie đăng nhập cho phép truy cập tài khoản Google đó.
> Dùng **tài khoản phụ, tạo riêng cho việc này** — đừng bao giờ dùng tài khoản
> chính. Google có thể khoá tài khoản khi thấy cookie của nó được dùng từ IP
> trung tâm dữ liệu, nên hãy coi tài khoản phụ này là thứ có thể mất.

#### Các bước

**Bước 1 — xuất cookie (cần máy tính, làm một lần)**

Bước này không làm được trên iPhone: Safari iOS không có tiện ích xuất cookie.
Mượn máy tính bất kỳ, dùng Chrome hay Firefox:

1. Cài một tiện ích xuất cookie định dạng Netscape (tìm "cookies.txt" trong kho
   tiện ích của trình duyệt). Nên chọn loại mã nguồn mở, xuất ngay trên máy.

2. **Bật tiện ích đó cho chế độ ẩn danh.** Trình duyệt mặc định tắt hết tiện
   ích ở cửa sổ ẩn danh, nên bỏ qua bước này là tới bước sau không có nút nào
   để bấm.

   - Chrome / Edge / Cốc Cốc: vào `chrome://extensions`, tìm tiện ích, bấm
     **Details** (Chi tiết), bật **Allow in Incognito** (Cho phép ở chế độ ẩn
     danh).
   - Firefox: vào `about:addons` → **Extensions**, bấm vào tiện ích, mục
     **Run in Private Windows** chọn **Allow**.

   Xuất xong tắt lại công tắc này cũng được — nó cho tiện ích đọc cookie của cả
   phiên ẩn danh, chỉ nên bật khi cần.

3. **Phải dùng cửa sổ ẩn danh, và làm đúng trình tự này.** YouTube xoay vòng
   cookie liên tục trên các tab đang mở, nên cookie xuất từ phiên thường sẽ bị
   vô hiệu ngay sau đó. Trình tự dưới đây là của chính tài liệu yt-dlp:

   1. Mở một cửa sổ **ẩn danh** mới, đăng nhập YouTube bằng **tài khoản phụ**.
   2. Vẫn trong tab đó, vào `https://www.youtube.com/robots.txt`.
      Bảo đảm đây là tab ẩn danh **duy nhất** đang mở.
   3. Bấm xuất cookie của `youtube.com` bằng tiện ích.
   4. **Đóng cửa sổ ẩn danh ngay lập tức**, đừng đăng xuất.

   Đóng cửa sổ ngay là để phiên đó không còn hoạt động và không xoay vòng
   cookie nữa — đúng ngược với trực giác, nhưng đây mới là cách giữ cookie sống.
4. **Xuất riêng youtube.com, đừng xuất cookie của mọi trang.** GitHub Secret
   chỉ nhận tối đa 48 KB; file "tất cả các trang" thường vài trăm KB và sẽ báo
   `value is too large` ở **Bước 2 — Cất vào GitHub Secret**. Đa số tiện ích có
   tuỳ chọn xuất riêng trang đang mở.

   Lỡ xuất cả rồi thì lọc lại (cần Python trên máy đó):

   ```
   python3 scripts/check_cookies.py cookies.txt --loc yt.txt
   ```

   Lệnh này giữ lại đúng cookie của `google.com` và `youtube.com` — cookie đăng
   nhập nằm rải trên cả hai, `LOGIN_INFO` ở youtube còn `SID`/`SAPISID`/
   `__Secure-1PSID` ở google — rồi báo dung lượng trước/sau. Dùng `yt.txt`.

   Không có Python thì lọc bằng terminal:

   - macOS/Linux: `grep -E '^\.?(www\.)?(youtube|google)\.com' cookies.txt > yt.txt`
   - Windows PowerShell: `Select-String -Path cookies.txt -Pattern '(youtube|google)\.com' | ForEach-Object { $_.Line } | Set-Content yt.txt`

5. Mở file bằng trình soạn thảo văn bản (Notepad, TextEdit ở chế độ văn bản
   thuần) và copy **toàn bộ** nội dung.

   Lỗi hay gặp nhất ở đây: copy từ cửa sổ xem trước của trình duyệt làm ký tự
   TAB biến thành dấu cách, và file mất tác dụng hoàn toàn mà không báo gì.
   Workflow ở bước 3 sẽ bắt được lỗi này và nói rõ.

**Bước 2 — cất vào GitHub Secret (làm được trên iPhone)**

Repo → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret**. Tên: `YTDLP_COOKIES`. Dán toàn bộ nội dung vừa copy
vào ô Secret rồi lưu.

Secret được mã hoá, không hiện lại trong giao diện và không lọt vào log Actions.

**Bước 3 — bấm nạp (làm được trên iPhone)**

Tab **Actions** → **Nạp cookie YouTube** → **Run workflow**.

Workflow soi file trước khi nạp (`scripts/check_cookies.py`): thiếu TAB, hết
hạn, chưa đăng nhập, hay nhầm cookie trang khác đều bị chặn kèm lời giải thích.
Qua được thì nó đẩy file lên Modal Volume `tachnhac-data`.

**Không cần deploy lại** — job tách nhạc sau đó tự nhặt file cookie.

**Bước 4 — kiểm tra**

Mở `https://<user>--tachnhac-api.modal.run/diag/cookies`. Đúng thì thấy đại loại:

```json
{"present": true, "cookies": 42, "logged_in": true,
 "auth_cookies": ["SAPISID", "SID", "__Secure-1PSID"], "expires_in_days": 58.6}
```

Endpoint này chỉ đếm và xem hạn, không trả về giá trị cookie nào.

#### Khi cookie hết hạn

`expires_in_days` tụt về 0, hoặc lỗi chặn bot quay lại. YouTube huỷ cookie nhanh
hơn khi thấy nó dùng từ IP trung tâm dữ liệu, nên vài tuần một lần là chuyện
thường. Làm lại bước 1–3 (sửa lại secret cũ, không cần tạo mới).

Muốn gỡ cookie ra: chạy lại workflow, bật ô **Xoá cookie đang có**.

**Bản quyền**: chỉ dán link tới nội dung bạn có quyền sử dụng. Việc tải nhạc có
bản quyền về thường vi phạm điều khoản dịch vụ của YouTube và có thể vi phạm luật
bản quyền nơi bạn ở — người vận hành trang và người dán link tự chịu trách nhiệm.
Trước khi mở cho người ngoài, xem lại mục "Việc còn lại" ở cuối README.

## Tải bản gốc

Tách xong, ngay trên mixer có thêm hàng **Bản gốc**: tải về nguyên bài chưa
tách — vocal và nhạc nền còn nằm chung một file. Dùng được cho cả hai cách tách:

- **Cách 1 (thả file)** — trả lại đúng file bạn đưa lên, nguyên định dạng gốc
  (mp3, m4a, flac…), không phải bản mp3 do máy tách xuất ra.
- **Cách 2 (dán link)** — đây là đường duy nhất lấy được bản gốc, vì bài do máy
  chủ tải về, chưa từng đi qua máy bạn. Tên file lấy theo tiêu đề bài hát.

Backend phục vụ từ `GET /jobs/{id}/source`, đọc thẳng file input mà worker GPU
đã dùng — không tách thêm, không tốn thêm chỗ chứa. File vẫn bị dọn cùng job sau
24 giờ.

Nút chỉ hiện khi job có khoá `source_ext`. Backend bản cũ không trả khoá này nên
nút ẩn hẳn, thay vì hiện ra rồi bấm vào ăn 404.

## Mô hình

| Key | Model | Track ra | Ghi chú |
|---|---|---|---|
| `roformer` | BS-Roformer | Giọng + Nhạc nền | Sạch nhất cho vocal |
| `htdemucs_ft` | HTDemucs FT | Giọng, trống, bass, nhạc cụ | Chậm hơn ~4x |
| `htdemucs` | HTDemucs | Giọng, trống, bass, nhạc cụ | Nhanh, hơi kém sạch |

Weights tự tải lần chạy đầu rồi cache vào Volume `tachnhac-models`.

## Chỉnh tay

- **Chất lượng cao hơn**: đổi `output_format="MP3"` thành `"WAV"` trong `modal_app.py`. File nặng hơn nhiều, tải chậm hơn.
- **Rẻ hơn**: đổi `gpu="A10G"` thành `gpu="T4"` — chậm hơn khoảng 2x, giá thấp hơn.
- **Preview 30s**: cắt input bằng ffmpeg trước khi tách, chỉ trả full stem sau khi thanh toán.
- **Giữ container ấm**: thêm `min_containers=1` vào `@app.function` của `separate` để bỏ cold start (~15s), đổi lại trả tiền GPU liên tục — chỉ bật khi có traffic thật.

## Khi có trục trặc

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| Banner đỏ "Chưa kết nối được backend" | Chưa deploy Modal, hoặc URL trong ô cấu hình sai. Thử mở `/health` trực tiếp. |
| Lỗi HTTP 413 khi tải lên | Đang đi qua `/api` của Vercel. Dán URL Modal vào ô cấu hình để gửi thẳng. |
| Kẹt ở "Đang xếp hàng…" | Container GPU chưa khởi động xong (lần đầu ~1–2 phút do phải tải weights), hoặc worker chết. Xem log trong dashboard Modal. |
| "Quá thời gian chờ" | Job treo — kiểm tra log `separate` trên Modal. |
| Báo lỗi tên model | Weights chưa tải được. Xoá Volume `tachnhac-models` rồi chạy lại. |
| Ô dán link hiện khung vàng "Backend đang chạy bản cũ" | Backend chưa có `/jobs/link`. Vào Actions → Deploy Modal → Run workflow, chờ build xong rồi tải lại trang. Trong lúc đó vẫn tách được bằng cách thả file. |
| "YouTube đang chặn máy chủ…" | Cách nhanh: tải bài về máy rồi dùng Cách 1. Cách lâu dài: nạp cookie theo mục *Khi YouTube đòi "xác nhận không phải robot"*. |
| GitHub báo `value is too large` khi lưu secret | File cookie quá 48 KB vì xuất cookie của mọi trang. Lọc lại bằng `python3 scripts/check_cookies.py cookies.txt --loc yt.txt` rồi dán `yt.txt`. |
| Mở cửa sổ ẩn danh nhưng không thấy nút xuất cookie | Chưa bật tiện ích cho chế độ ẩn danh. Xem bước 2 ở mục *Các bước*. |
| `Requested format is not available` | Client vượt được cửa chặn nhưng không trả về luồng audio nào — thường do thiếu PO token. Backend đã bật `formats=missing_pot` và tự thử client kế tiếp. Hết chuỗi vẫn vậy thì cần PO token thật (xem dưới). |
| `The page needs to be reloaded` | Bản yt-dlp trên máy chủ cũ hơn thay đổi mới nhất của YouTube. Deploy lại app Modal (layer yt-dlp luôn cài lại bản mới). |
| Đã nạp cookie mà vẫn bị chặn | Mở `/diag/cookies`. `logged_in: false` = xuất lúc chưa đăng nhập. `expired: true` = nạp lại file mới. Đúng hết mà vẫn chặn thì nhiều khả năng cookie đã bị YouTube xoay vòng — xuất lại đúng trình tự cửa sổ ẩn danh ở trên. |
| `/diag/cookies` báo `expires_in_days` rất nhỏ | Số này tính trên cookie **đăng nhập**, không phải mọi cookie. Nhỏ thật thì cookie sắp hết hạn, xuất lại. |
| Link Spotify ra nhầm bài | Bản khớp nhất trên YouTube không phải bản gốc. Dán thẳng link YouTube của bản bạn muốn. |
| Không thấy hàng "Bản gốc" trong mixer | Job này chạy trên backend bản cũ (chưa có `/jobs/{id}/source`). Deploy lại qua Actions → Deploy Modal rồi tách lại — job cũ không có bản gốc để lấy. |
| "Bài dài … quá mốc 12 phút" | Cắt ngắn file rồi tải lên, hoặc nới `MAX_SOURCE_SECONDS` trong `modal_app.py`. |

## Việc còn lại trước khi mở cho người ngoài

- [ ] Siết `allow_origins` về đúng domain thay vì `*`
- [ ] Rate limit theo IP ở endpoint `/jobs` và `/jobs/link` — endpoint link tốn
      băng thông ra ngoài, dễ bị lạm dụng thành máy tải nhạc chùa
- [ ] Trang điều khoản: người dùng tự chịu trách nhiệm về bản quyền nội dung tải
      lên **và nội dung dán link tới**
- [ ] Chunk file dài trên 10 phút (hạ `segment_size` nếu gặp OOM)
