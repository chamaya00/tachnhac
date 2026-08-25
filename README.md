# Tách nhạc — MVP

Web app tách giọng hát khỏi nhạc nền. Backend chạy GPU serverless trên Modal, frontend là một file HTML tĩnh trên Vercel.

## Kiến trúc

```
index.html (Vercel, tĩnh)
   │  POST /jobs  (multipart)
   ▼
Modal ASGI endpoint ──spawn──▶ separate()  [GPU A10G]
   │                              │
   │  GET /jobs/{id}              │ audio-separator
   │  GET /jobs/{id}/stems/{name} │ BS-Roformer / HTDemucs
   ▼                              ▼
modal.Dict (trạng thái)     modal.Volume (weights + stem)
```

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

## Việc còn lại trước khi mở cho người ngoài

- [ ] Siết `allow_origins` về đúng domain thay vì `*`
- [ ] Rate limit theo IP ở endpoint `/jobs`
- [ ] Trang điều khoản: người dùng tự chịu trách nhiệm về bản quyền nội dung tải lên
- [ ] Chunk file dài trên 10 phút (hạ `segment_size` nếu gặp OOM)
