"""
Tách nhạc & giọng — Modal GPU worker.

Deploy:  modal deploy modal_app.py
Endpoint sẽ có dạng: https://<user>--tachnhac-api.modal.run
"""

import os
import re
import shutil
import time
import uuid

import modal

APP_NAME = "tachnhac"

# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
# Nền CUDA + cuDNN có sẵn: onnxruntime-gpu cần libcudnn.so.9, debian_slim không
# có nên trước đây nó lặng lẽ tụt về CPU (hoặc chết khi tạo session).
image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04", add_python="3.11"
    )
    # clang + build-essential: demucs kéo theo diffq, gói này không có wheel dựng
    # sẵn nên pip phải biên dịch extension C. Bản Python standalone của Modal được
    # build bằng clang nên sysconfig ghi CC=clang — cài mỗi gcc sẽ không cứu được.
    .apt_install("ffmpeg", "git", "clang", "build-essential")
    .pip_install(
        # >=0.28 là mốc có tham số custom_output_names trong Separator.separate()
        "audio-separator[gpu]>=0.28,<1.0",
        "fastapi[standard]",
        "python-multipart",
    )
    # Model weights tải về /models thay vì thư mục mặc định, để cache qua Volume
    .env({"AUDIO_SEPARATOR_MODEL_DIR": "/models"})
)

app = modal.App(APP_NAME, image=image)

# Weights nặng vài trăm MB → cache vào Volume, không tải lại mỗi lần chạy.
models_vol = modal.Volume.from_name("tachnhac-models", create_if_missing=True)
# Nơi chứa file upload + stem đầu ra.
data_vol = modal.Volume.from_name("tachnhac-data", create_if_missing=True)
# Trạng thái job.
jobs = modal.Dict.from_name("tachnhac-jobs", create_if_missing=True)

MODELS = {
    # 2 stem — chất lượng vocal tốt nhất hiện tại
    "roformer": {
        "file": "model_bs_roformer_ep_317_sdr_12.9755.ckpt",
        "stems": ["Vocals", "Instrumental"],
        "label": "BS-Roformer (2 stem)",
    },
    # 4 stem
    "htdemucs_ft": {
        "file": "htdemucs_ft.yaml",
        "stems": ["Vocals", "Drums", "Bass", "Other"],
        "label": "HTDemucs FT (4 stem)",
    },
    # 4 stem, nhanh hơn htdemucs_ft khoảng 4x
    "htdemucs": {
        "file": "htdemucs.yaml",
        "stems": ["Vocals", "Drums", "Bass", "Other"],
        "label": "HTDemucs (4 stem, nhanh)",
    },
}

MAX_UPLOAD_BYTES = 60 * 1024 * 1024  # 60 MB
JOB_TTL_SECONDS = 24 * 3600

# Phần mở rộng được phép. Giữ lại đuôi file là bắt buộc: audio-separator dựa vào
# nó để chọn decoder, file tên "input" trần không mở được với m4a/aac/flac.
ALLOWED_EXTS = {".mp3", ".wav", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".aac", ".wma"}
DEFAULT_EXT = ".mp3"


def _input_path(job_id: str, ext: str) -> str:
    return f"/data/{job_id}/input{ext}"


def _safe_ext(filename: str | None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTS else DEFAULT_EXT


# ---------------------------------------------------------------------------
# GPU worker
# ---------------------------------------------------------------------------
@app.function(
    gpu="A10G",
    volumes={"/models": models_vol, "/data": data_vol},
    timeout=1800,
    # retries=0: lần chạy lại sẽ ghi đè trạng thái "error" mà frontend vừa đọc
    # được, làm giao diện nhảy loạn. Thà báo lỗi dứt khoát một lần.
    retries=0,
)
def separate(job_id: str, model_key: str, ext: str = DEFAULT_EXT):
    def mark(**kw):
        job = jobs.get(job_id, {})
        job.update(kw)
        jobs[job_id] = job

    # Mọi thứ nằm trong try, kể cả import. Trước đây import đứng ngoài, nên khi
    # audio_separator không nạp được (torch/onnxruntime thiếu thư viện CUDA) thì
    # ngoại lệ bay ra mà không ai ghi lại — job treo ở "queued" vĩnh viễn.
    try:
        # Dấu hiệu container đã thực sự nhận việc. Job đứng mãi ở "queued" nghĩa
        # là container chưa từng khởi động, thường vì chưa cấp phát được GPU.
        mark(status="starting", progress=0.05, started_at=time.time())

        import torch

        from audio_separator.separator import Separator

        # Ghi lại thiết bị thật sự dùng. Rơi về CPU thì BS-Roformer chậm gấp
        # hàng chục lần, đủ để chạm hạn chờ 20 phút của frontend.
        on_gpu = torch.cuda.is_available()
        mark(device=torch.cuda.get_device_name(0) if on_gpu else "cpu")

        cfg = MODELS[model_key]
        mark(status="loading_model", progress=0.1)

        # reload trước khi tạo thư mục: reload dựng lại view của volume, làm
        # trước rồi mới ghi thì chắc chắn thấy file input do container API đẩy lên.
        data_vol.reload()

        in_path = _input_path(job_id, ext)
        if not os.path.exists(in_path):
            raise RuntimeError("Không tìm thấy file đã tải lên trên volume.")

        out_dir = f"/data/{job_id}/stems"
        os.makedirs(out_dir, exist_ok=True)

        separator = Separator(
            model_file_dir="/models",
            output_dir=out_dir,
            output_format="MP3",  # đổi sang "WAV" nếu cần chất lượng gốc
        )
        separator.load_model(model_filename=cfg["file"])
        models_vol.commit()  # giữ lại weights vừa tải

        mark(status="separating", progress=0.35)
        t0 = time.time()

        # Ép tên file đầu ra thành tên stem, để frontend gọi thẳng /stems/vocals.
        # Đưa cả hai kiểu viết hoa vì tuỳ kiến trúc model mà audio-separator dùng
        # "Vocals" hay "vocals" làm khoá tra cứu; khoá thừa bị bỏ qua vô hại.
        output_names = {}
        for stem in cfg["stems"]:
            output_names[stem] = stem.lower()
            output_names[stem.lower()] = stem.lower()

        try:
            separator.separate(in_path, custom_output_names=output_names)
        except TypeError:
            # Bản audio-separator quá cũ chưa có custom_output_names — vẫn chạy
            # được, tên file sẽ được chuẩn hoá ở bước dưới.
            separator.separate(in_path)

        mark(status="finalizing", progress=0.9)
        produced = _normalize_stem_files(out_dir, cfg["stems"])
        if not produced:
            raise RuntimeError("Model chạy xong nhưng không sinh ra file stem nào.")

        data_vol.commit()

        mark(
            status="done",
            progress=1.0,
            stems=list(produced.keys()),
            files=produced,
            seconds=round(time.time() - t0, 1),
            finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001
        mark(status="error", error=f"{type(exc).__name__}: {exc}"[:400],
             finished_at=time.time())
        raise


def _normalize_stem_files(out_dir: str, stems: list[str]) -> dict[str, str]:
    """Đổi tên file đầu ra thành đúng <stem>.<ext>.

    custom_output_names không phải lúc nào cũng ăn — tuỳ kiến trúc model, tên
    file có thể ra dạng "input_(Vocals)_model_bs_roformer_....mp3". Frontend gọi
    /stems/vocals nên tên phải đoán trước được, không thể phó mặc cho model.
    """
    found: dict[str, str] = {}
    leftovers: list[str] = []

    for name in sorted(os.listdir(out_dir)):
        if not name.lower().endswith((".mp3", ".wav", ".flac")):
            continue
        base, ext = os.path.splitext(name)
        low = base.lower()

        match = None
        for stem in stems:
            key = stem.lower()
            if low == key or re.search(rf"[(\[_\-]{key}[)\]_\-]?", low):
                match = key
                break

        if match is None or match in found:
            leftovers.append(name)
            continue

        target = f"{match}{ext.lower()}"
        if name != target:
            shutil.move(os.path.join(out_dir, name), os.path.join(out_dir, target))
        found[match] = target

    # Không khớp được stem nào (tên file lạ hoàn toàn) → vẫn phục vụ, đặt theo
    # thứ tự stem còn trống, hơn là trả về rỗng và hỏng cả job.
    for name in leftovers:
        remaining = [s.lower() for s in stems if s.lower() not in found]
        if not remaining:
            break
        ext = os.path.splitext(name)[1].lower()
        target = f"{remaining[0]}{ext}"
        shutil.move(os.path.join(out_dir, name), os.path.join(out_dir, target))
        found[remaining[0]] = target

    # Trả về theo đúng thứ tự khai báo trong MODELS cho giao diện gọn mắt.
    return {s.lower(): found[s.lower()] for s in stems if s.lower() in found}


# ---------------------------------------------------------------------------
# Dò môi trường
# ---------------------------------------------------------------------------
@app.function(gpu="A10G", volumes={"/models": models_vol}, timeout=600)
def gpu_probe():
    """Kiểm tra container GPU nạp được những gì. Gọi qua GET /diag."""
    info = {}

    try:
        import torch

        info["torch"] = torch.__version__
        info["cuda_available"] = torch.cuda.is_available()
        info["cuda_version"] = torch.version.cuda
        if torch.cuda.is_available():
            info["gpu"] = torch.cuda.get_device_name(0)
    except Exception as exc:  # noqa: BLE001
        info["torch_error"] = f"{type(exc).__name__}: {exc}"[:300]

    try:
        import onnxruntime as ort

        info["onnxruntime"] = ort.__version__
        info["providers"] = ort.get_available_providers()
    except Exception as exc:  # noqa: BLE001
        info["onnxruntime_error"] = f"{type(exc).__name__}: {exc}"[:300]

    try:
        from audio_separator.separator import Separator  # noqa: F401

        info["audio_separator"] = "import OK"
    except Exception as exc:  # noqa: BLE001
        info["audio_separator_error"] = f"{type(exc).__name__}: {exc}"[:300]

    return info


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
@app.function(volumes={"/data": data_vol}, timeout=600)
@modal.asgi_app()
def api():
    from fastapi import FastAPI, File, Form, HTTPException, UploadFile
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse

    web = FastAPI(title="Tách nhạc API")
    web.add_middleware(
        CORSMiddleware,
        # Siết lại thành domain của bạn khi lên production
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @web.get("/health")
    def health():
        return {"ok": True, "app": APP_NAME, "models": list(MODELS)}

    @web.get("/diag")
    def diag():
        """Khởi động một container GPU và báo cáo nó nạp được gì.

        Chặn tới khi container chạy xong nên có thể mất 1-2 phút lần đầu.
        """
        try:
            return {"ok": True, "probe": gpu_probe.remote()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]}

    @web.get("/models")
    def list_models():
        return {
            k: {"label": v["label"], "stems": [s.lower() for s in v["stems"]]}
            for k, v in MODELS.items()
        }

    @web.post("/jobs")
    async def create_job(
        file: UploadFile = File(...),
        model: str = Form("roformer"),
    ):
        if model not in MODELS:
            raise HTTPException(400, f"Model không hợp lệ: {model}")

        raw = await file.read()
        if not raw:
            raise HTTPException(400, "File rỗng.")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413, f"File quá lớn. Giới hạn {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
            )

        ext = _safe_ext(file.filename)
        job_id = uuid.uuid4().hex
        os.makedirs(f"/data/{job_id}", exist_ok=True)
        with open(_input_path(job_id, ext), "wb") as fh:
            fh.write(raw)
        data_vol.commit()

        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "model": model,
            "filename": file.filename,
            "ext": ext,
            "created_at": time.time(),
        }
        separate.spawn(job_id, model, ext)
        return {"job_id": job_id}

    @web.get("/jobs/{job_id}")
    def job_status(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Không tìm thấy job.")
        return job

    @web.get("/jobs/{job_id}/stems/{stem}")
    def download_stem(job_id: str, stem: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Không tìm thấy job.")
        if job.get("status") != "done":
            raise HTTPException(409, "Stem chưa sẵn sàng.")

        filename = job.get("files", {}).get(stem.lower())
        if not filename:
            raise HTTPException(404, f"Không có stem '{stem}'.")

        data_vol.reload()
        path = f"/data/{job_id}/stems/{filename}"
        if not os.path.exists(path):
            raise HTTPException(410, "File đã bị xoá.")

        base = os.path.splitext(job.get("filename") or "audio")[0]
        ext = os.path.splitext(filename)[1]
        return FileResponse(
            path,
            media_type="audio/mpeg" if ext == ".mp3" else "audio/wav",
            filename=f"{base} - {stem}{ext}",
        )

    return web


# ---------------------------------------------------------------------------
# Dọn file cũ
# ---------------------------------------------------------------------------
@app.function(volumes={"/data": data_vol}, schedule=modal.Period(hours=6))
def cleanup():
    data_vol.reload()
    now = time.time()
    removed = 0

    for job_id in list(jobs.keys()):
        job = jobs.get(job_id) or {}
        if now - job.get("created_at", now) > JOB_TTL_SECONDS:
            shutil.rmtree(f"/data/{job_id}", ignore_errors=True)
            del jobs[job_id]
            removed += 1

    data_vol.commit()
    print(f"Đã dọn {removed} job.")
