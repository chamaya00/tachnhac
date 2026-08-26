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
2. **Dán link YouTube/Spotify** — máy chủ tự tải bài về rồi tách.

Cố tình không dùng tab: tab nào cũng phải chọn sẵn một cái, mà chọn sẵn thì có
lúc phải tự chuyển — người dùng thấy trang tự nhảy và tưởng hỏng.

| Nguồn | Cách xử lý |
|---|---|
| YouTube (`youtube.com`, `youtu.be`, `music.youtube.com`) | yt-dlp tải thẳng luồng audio tốt nhất, ffmpeg chuyển sang MP3 192 kbps. |
| Spotify (`open.spotify.com/track/…`, `spotify.link`) | Đọc tên bài + nghệ sĩ từ metadata công khai, rồi tìm đúng bài đó trên YouTube. |

Spotify không phát audio ra ngoài trình phát của họ nên không có đường tải thẳng
— cách trên cũng là cách `spotdl` vẫn làm. Hệ quả: bản lấy về là bản trên
YouTube, có thể là live, cover hay remaster khác với bản trong playlist. Nghe thử
ở mixer trước khi tải track về.

**Giới hạn**

- Tối đa 12 phút mỗi bài (`MAX_SOURCE_SECONDS` trong `modal_app.py`). Dài hơn thì
  cắt ngắn rồi tải file lên.
- Chỉ nhận link **một bài**. Album, playlist, podcast đều bị từ chối ngay.
- Link ngoài hai nguồn trên bị chặn ở cả frontend lẫn backend.

### Khi YouTube đòi "xác nhận không phải robot"

Modal chạy trên IP trung tâm dữ liệu, mà YouTube đối xử với dải IP đó gắt nhất.
Backend tự thử lại một lần với `player_client=tv` (client này không cần PO
token); không ăn thì báo lỗi. Đây là hạn chế của chỗ đặt máy chủ, không phải lỗi
cấu hình — **không có cờ nào bật lên là hết**.

Ba lựa chọn, xếp theo công sức bỏ ra:

| Cách | Công sức | Bền được bao lâu |
|---|---|---|
| Tải bài về máy rồi dùng Cách 1 trên trang | Không có gì | Mãi mãi |
| Nạp cookie (dưới đây) | Cần máy tính một lần | Vài tuần, rồi phải nạp lại |
| Thuê proxy dân cư, gắn vào yt-dlp | Tốn tiền hằng tháng | Bền, nhưng phải trả phí |

Với nhu cầu thỉnh thoảng tách một bài, **cách đầu tiên là hợp lý nhất**. Cookie
chỉ đáng làm nếu bạn dùng thường xuyên và ngại thao tác tải về mỗi lần.

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

1. Đăng nhập YouTube bằng **tài khoản phụ**. Đừng dùng cửa sổ ẩn danh — đóng
   cửa sổ là cookie mất hiệu lực ngay.
2. Cài một tiện ích xuất cookie định dạng Netscape (tìm "cookies.txt" trong kho
   tiện ích của trình duyệt).
3. Mở youtube.com rồi bấm xuất. Được file `cookies.txt`.
4. Mở file bằng trình soạn thảo văn bản (Notepad, TextEdit ở chế độ văn bản
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
| Đã nạp cookie mà vẫn bị chặn | Mở `/diag/cookies`. `logged_in: false` = xuất lúc chưa đăng nhập. `expired: true` = nạp lại file mới. Đúng hết mà vẫn chặn thì IP của Modal đang bị siết, đành dùng Cách 1. |
| Link Spotify ra nhầm bài | Bản khớp nhất trên YouTube không phải bản gốc. Dán thẳng link YouTube của bản bạn muốn. |
| "Bài dài … quá mốc 12 phút" | Cắt ngắn file rồi tải lên, hoặc nới `MAX_SOURCE_SECONDS` trong `modal_app.py`. |

## Việc còn lại trước khi mở cho người ngoài

- [ ] Siết `allow_origins` về đúng domain thay vì `*`
- [ ] Rate limit theo IP ở endpoint `/jobs` và `/jobs/link` — endpoint link tốn
      băng thông ra ngoài, dễ bị lạm dụng thành máy tải nhạc chùa
- [ ] Trang điều khoản: người dùng tự chịu trách nhiệm về bản quyền nội dung tải
      lên **và nội dung dán link tới**
- [ ] Chunk file dài trên 10 phút (hạ `segment_size` nếu gặp OOM)
