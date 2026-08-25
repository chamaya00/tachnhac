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

### 1. Modal

1. Đăng ký tại modal.com, tạo API token trong dashboard.
2. Đưa `modal_app.py` lên một repo GitHub (dùng trình soạn thảo web của GitHub).
3. Tạo `.github/workflows/deploy.yml`:

```yaml
name: Deploy Modal
on:
  push:
    branches: [main]
  workflow_dispatch:

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install modal
      - run: modal deploy modal_app.py
        env:
          MODAL_TOKEN_ID: ${{ secrets.MODAL_TOKEN_ID }}
          MODAL_TOKEN_SECRET: ${{ secrets.MODAL_TOKEN_SECRET }}
```

4. Thêm `MODAL_TOKEN_ID` và `MODAL_TOKEN_SECRET` vào Settings → Secrets → Actions.
5. Push. Lần build đầu mất khoảng 5–8 phút (cài torch). Log sẽ in ra URL endpoint.

### 2. Frontend

Đặt `index.html` và `vercel.json` chung một thư mục (ví dụ `web/`).

1. Mở `vercel.json`, thay `REPLACE-ME--tachnhac-api.modal.run` bằng URL Modal thật.
2. Import repo vào Vercel:
   - Framework Preset: **Other**
   - Root Directory: `web`
   - Build Command / Output Directory: để trống
3. Deploy.

Frontend gọi `/api/...`, Vercel chuyển tiếp sang Modal. Trình duyệt thấy mọi thứ cùng một gốc nên không có CORS, không phải cấu hình URL ở phía client.

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

## Việc còn lại trước khi mở cho người ngoài

- [ ] Siết `allow_origins` về đúng domain thay vì `*`
- [ ] Rate limit theo IP ở endpoint `/jobs`
- [ ] Trang điều khoản: người dùng tự chịu trách nhiệm về bản quyền nội dung tải lên
- [ ] Chunk file dài trên 10 phút (hạ `segment_size` nếu gặp OOM)
