"""
Tách nhạc & giọng — Modal GPU worker.

Deploy:  modal deploy modal_app.py
Endpoint sẽ có dạng: https://<user>--tachnhac-api.modal.run
"""

import os
import time
import uuid

import modal

APP_NAME = "tachnhac"

# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------
# audio-separator kéo theo torch + onnxruntime-gpu. Build lần đầu ~5-8 phút,
# sau đó Modal cache lại nên deploy tiếp theo gần như tức thì.
image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "audio-separator[gpu]",
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


# ---------------------------------------------------------------------------
# GPU worker
# ---------------------------------------------------------------------------
@app.function(
    gpu="A10G",
    volumes={"/models": models_vol, "/data": data_vol},
    timeout=1800,
    retries=1,
)
def separate(job_id: str, model_key: str):
    from audio_separator.separator import Separator

    cfg = MODELS[model_key]
    in_path = f"/data/{job_id}/input"
    out_dir = f"/data/{job_id}/stems"
    os.makedirs(out_dir, exist_ok=True)

    def mark(**kw):
        job = jobs.get(job_id, {})
        job.update(kw)
        jobs[job_id] = job

    try:
        mark(status="loading_model", progress=0.1)
        data_vol.reload()

        separator = Separator(
            model_file_dir="/models",
            output_dir=out_dir,
            output_format="MP3",  # đổi sang "WAV" nếu cần chất lượng gốc
        )
        separator.load_model(model_filename=cfg["file"])
        models_vol.commit()  # giữ lại weights vừa tải

        mark(status="separating", progress=0.35)
        t0 = time.time()

        # Ép tên file đầu ra thành tên stem, để frontend gọi thẳng /stems/vocals
        output_names = {stem: stem.lower() for stem in cfg["stems"]}
        separator.separate(in_path, output_names)

        produced = sorted(
            f for f in os.listdir(out_dir) if f.lower().endswith((".mp3", ".wav"))
        )
        data_vol.commit()

        mark(
            status="done",
            progress=1.0,
            stems=[os.path.splitext(f)[0] for f in produced],
            files={os.path.splitext(f)[0]: f for f in produced},
            seconds=round(time.time() - t0, 1),
            finished_at=time.time(),
        )
    except Exception as exc:  # noqa: BLE001
        mark(status="error", error=str(exc)[:400], finished_at=time.time())
        raise


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

    @web.get("/models")
    def list_models():
        return {
            k: {"label": v["label"], "stems": v["stems"]} for k, v in MODELS.items()
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

        job_id = uuid.uuid4().hex
        os.makedirs(f"/data/{job_id}", exist_ok=True)
        with open(f"/data/{job_id}/input", "wb") as fh:
            fh.write(raw)
        data_vol.commit()

        jobs[job_id] = {
            "status": "queued",
            "progress": 0.0,
            "model": model,
            "filename": file.filename,
            "created_at": time.time(),
        }
        separate.spawn(job_id, model)
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
        if job is None or job.get("status") != "done":
            raise HTTPException(404, "Stem chưa sẵn sàng.")

        filename = job.get("files", {}).get(stem)
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
    import shutil

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
