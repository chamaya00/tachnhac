"""
Tách nhạc & giọng — Modal GPU worker.

Deploy:  modal deploy modal_app.py
Endpoint sẽ có dạng: https://<user>--tachnhac-api.modal.run
"""

import glob
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
import uuid
from html import unescape

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
        # Nguồn nhạc từ link. Ghim mốc tối thiểu chứ không ghim chặt: YouTube đổi
        # cách chống bot liên tục, bản yt-dlp cũ vài tháng là hỏng.
        "yt-dlp>=2025.1.15",
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

# Stem luôn ra MP3, nhưng bản gốc thì giữ nguyên định dạng người dùng đưa vào —
# trả nhầm "audio/mpeg" cho một file flac là nói dối trình duyệt, có máy sẽ mở
# trong trình phát rồi phát lỗi thay vì lưu về.
MEDIA_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
}


def _media_type(ext: str) -> str:
    return MEDIA_TYPES.get((ext or "").lower(), "application/octet-stream")


def _input_path(job_id: str, ext: str) -> str:
    return f"/data/{job_id}/input{ext}"


def _safe_download_name(name: str | None) -> str:
    """Gọt tên bài trước khi ném vào header Content-Disposition.

    Tên đến từ tiêu đề YouTube hoặc tên file người dùng đặt — có thể chứa dấu
    nháy kép (làm vỡ header), dấu / (thành đường dẫn), hay xuống dòng. Giữ lại
    dấu tiếng Việt: Starlette tự mã hoá phần non-ASCII sang filename*=UTF-8.
    """
    base = os.path.splitext(name or "")[0]
    base = re.sub(r'[\\/:*?"<>|\r\n\t]', " ", base)
    base = re.sub(r"\s+", " ", base).strip(" .")
    return base[:80] or "audio"


def _safe_ext(filename: str | None) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTS else DEFAULT_EXT


# ---------------------------------------------------------------------------
# Nguồn nhạc từ link
# ---------------------------------------------------------------------------
YT_HOSTS = {
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
    "youtu.be", "www.youtu.be", "youtube-nocookie.com", "www.youtube-nocookie.com",
}
SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com", "spotify.link"}
# vm./vt. là link rút gọn khi bấm Chia sẻ trong app — yt-dlp tự lần theo chuyển
# hướng, không cần ta giải trước.
TIKTOK_HOSTS = {
    "tiktok.com", "www.tiktok.com", "m.tiktok.com",
    "vm.tiktok.com", "vt.tiktok.com",
}

# Bài dài hơn mốc này gần như chắc chắn không phải một ca khúc: mix DJ, podcast,
# livestream vài tiếng. Chặn từ đầu, đừng để GPU chạy 20 phút rồi mới vỡ.
MAX_SOURCE_SECONDS = 12 * 60
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Đặt file cookie Netscape ở một trong hai chỗ này để vượt màn "xác nhận không
# phải robot" của YouTube (xem README). Không có thì bỏ qua, không phải lỗi.
COOKIE_PATHS = ("/data/cookies.txt", "/models/cookies.txt")

# Địa chỉ proxy (một dòng, dạng http://user:pass@host:port). Proxy dân cư là
# cách duy nhất chữa dứt điểm việc bị chặn theo IP, nhưng phải trả tiền — để
# ngỏ đường cắm vào, ai cần thì nạp file, không cần sửa code.
PROXY_PATHS = ("/data/proxy.txt", "/models/proxy.txt")

# Thứ tự thử khi bị chặn bot. Theo tài liệu PO Token của yt-dlp, ba client sau
# không cần PO token nên còn cơ may lọt khi không có cookie; client mặc định
# vẫn thử đầu tiên vì khi thông thì nó cho chất lượng tốt nhất.
PLAYER_CLIENT_CHAIN = (None, ["tv"], ["android_vr"], ["web_embedded"])


def _classify_link(url: str) -> str:
    """Trả về "youtube" | "spotify" | "tiktok", hoặc ném ValueError."""
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("Link phải bắt đầu bằng http:// hoặc https://")

    host = (parts.hostname or "").lower()
    if host in YT_HOSTS:
        return "youtube"
    if host in SPOTIFY_HOSTS:
        return "spotify"
    if host in TIKTOK_HOSTS:
        return "tiktok"
    raise ValueError("Hiện chỉ nhận link YouTube, Spotify hoặc TikTok.")


def _http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _follow_redirect(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl()


def _dig(data, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _spotify_query(url: str) -> str:
    """Đọc tên bài + nghệ sĩ từ trang công khai của Spotify.

    Spotify không phát audio ra ngoài trình phát của họ, nên không có đường nào
    tải thẳng từ đó — cũng không tìm cách đó ở đây. Ta chỉ lấy phần metadata
    công khai (đúng thứ trình duyệt nào cũng đọc được để hiện preview link) rồi
    dùng nó tìm lại đúng bài trên YouTube. Đây cũng là cách spotdl vẫn làm.
    """
    if (urllib.parse.urlparse(url).hostname or "").lower() == "spotify.link":
        url = _follow_redirect(url)

    match = re.search(
        r"open\.spotify\.com/(?:intl-[a-z]{2}/)?([a-z]+)/([A-Za-z0-9]+)", url
    )
    if not match:
        raise ValueError("Link Spotify không đọc được. Dùng link dạng .../track/...")

    kind, sid = match.group(1), match.group(2)
    if kind != "track":
        raise ValueError(
            "Mới nhận link một bài hát (.../track/...). "
            "Link album, playlist hay podcast thì chưa."
        )

    title = artist = ""

    # 1. Trang embed — đầy đủ nhất, có cả danh sách nghệ sĩ.
    try:
        html = _http_get(f"https://open.spotify.com/embed/track/{sid}")
        blob = re.search(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        if blob:
            entity = _dig(
                json.loads(blob.group(1)),
                "props", "pageProps", "state", "data", "entity",
            ) or {}
            title = entity.get("name") or ""
            artist = ", ".join(
                a.get("name", "") for a in (entity.get("artists") or []) if a.get("name")
            )
    except Exception:  # noqa: BLE001
        pass

    # 2. oEmbed — API công khai, ổn định, nhưng chỉ có tên bài.
    if not title:
        try:
            data = json.loads(
                _http_get(
                    "https://open.spotify.com/oembed?url="
                    + urllib.parse.quote(f"https://open.spotify.com/track/{sid}", safe="")
                )
            )
            title = data.get("title") or ""
        except Exception:  # noqa: BLE001
            pass

    # 3. Thẻ og: trên trang thường — og:description dạng "Nghệ sĩ · Song · 1987".
    if not title or not artist:
        try:
            html = _http_get(f"https://open.spotify.com/track/{sid}")
            if not title:
                tag = re.search(r'<meta property="og:title" content="([^"]*)"', html)
                if tag:
                    title = unescape(tag.group(1))
            if not artist:
                tag = re.search(r'<meta property="og:description" content="([^"]*)"', html)
                if tag:
                    artist = unescape(tag.group(1)).split("\u00b7")[0].strip()
        except Exception:  # noqa: BLE001
            pass

    if not title:
        raise ValueError(
            "Không đọc được tên bài từ link Spotify. "
            "Thử dán link YouTube của bài đó, hoặc tải file lên."
        )
    return f"{artist} {title}".strip()


def _looks_like_bot_check(message: str) -> bool:
    low = message.lower()
    return any(
        s in low
        for s in ("sign in to confirm", "not a bot", "captcha", "cookies", "http error 429")
    )


def _friendly_download_error(exc: Exception, kind: str = "youtube",
                            tried: list | None = None) -> str:
    msg = str(exc)
    if _looks_like_bot_check(msg):
        # Nói ra đã thử những gì. Không có dòng này thì lỗi lúc chuỗi client
        # chạy đủ 4 lần trông y hệt lỗi lúc backend còn bản cũ chưa có chuỗi —
        # nhìn ảnh chụp màn hình không tài nào phân biệt được.
        note = f" Đã thử {len(tried)} cách: {', '.join(tried)}." if tried else ""
        if kind == "tiktok":
            return (
                "TikTok đang chặn máy chủ và đòi xác minh không phải robot."
                + note +
                " Tải video về máy rồi dùng Cách 1 ở trên."
            )
        return (
            "YouTube đang chặn máy chủ và đòi xác minh không phải robot."
            + note +
            " Cách nhanh nhất: tải bài về máy rồi dùng Cách 1 ở trên. "
            "Cách chữa lâu dài: nạp cookie cho backend — chạy workflow "
            "\"Nạp cookie YouTube\" trên GitHub (xem README, mục Tải nhạc từ link)."
        )
    low = msg.lower()
    if "private" in low or "members-only" in low:
        return "Video này ở chế độ riêng tư hoặc chỉ dành cho thành viên."
    if "unavailable" in low or "removed" in low:
        return "Video không còn tồn tại hoặc bị chặn ở khu vực của máy chủ."
    if "age" in low and "restrict" in low:
        return "Video bị giới hạn độ tuổi nên máy chủ không tải được."
    return f"Không tải được nhạc từ link: {msg}"[:400]


def _cookie_file() -> str | None:
    for path in COOKIE_PATHS:
        if os.path.exists(path):
            return path
    return None


def _proxy_url() -> str | None:
    for path in PROXY_PATHS:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value.startswith(("http://", "https://", "socks5://", "socks5h://")):
            return value
    return None


# Cookie đăng nhập của Google. Thiếu sạch nhóm này thì file chỉ là cookie khách
# vãng lai — có nạp cũng không qua được màn chặn bot.
AUTH_COOKIE_NAMES = {
    "SID", "HSID", "SSID", "APISID", "SAPISID", "LOGIN_INFO",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PAPISID", "__Secure-3PAPISID",
}


def _cookie_status() -> dict:
    """Tóm tắt tình trạng file cookie. Không trả về giá trị cookie nào.

    Chỉ đếm và xem hạn — đủ để biết đã nạp đúng chưa mà không biến endpoint này
    thành chỗ rò cookie đăng nhập.
    """
    path = _cookie_file()
    if not path:
        return {"present": False, "hint": "Chưa nạp cookie. Xem README, mục Tải nhạc từ link."}

    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = [
                l for l in fh.read().splitlines()
                if l.strip() and not l.lstrip().startswith("#") and "\t" in l
            ]
    except OSError as exc:
        return {"present": True, "readable": False, "error": str(exc)[:200]}

    names, expiries = set(), []
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        names.add(parts[5])
        try:
            value = int(parts[4])
            if value > 0:
                expiries.append(value)
        except ValueError:
            pass

    now = time.time()
    live = [e for e in expiries if e > now]
    auth = sorted(names & AUTH_COOKIE_NAMES)

    out = {
        "present": True,
        "path": path,
        "cookies": len(lines),
        "auth_cookies": auth,
        "logged_in": bool(auth),
    }
    if live:
        out["expires_in_days"] = round((min(live) - now) / 86400, 1)
    elif expiries:
        out["expired"] = True

    if not auth:
        out["hint"] = "Thiếu cookie đăng nhập — có thể đã xuất lúc chưa đăng nhập."
    elif out.get("expired"):
        out["hint"] = "Cookie đã hết hạn, nạp lại file mới."
    return out


def _ytdlp_download(job_id: str, target: str, mark, player_client=None) -> dict:
    """Tải audio về /data/<job_id>/input.mp3, trả về info dict của yt-dlp.

    `target` là URL YouTube, hoặc chuỗi "ytsearch1:..." khi nguồn là Spotify.
    """
    import yt_dlp

    last_tick = [0.0]

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            # modal.Dict là network call — đừng ghi mỗi chunk.
            if total and time.time() - last_tick[0] > 1.0:
                last_tick[0] = time.time()
                mark(progress=round(0.05 + 0.20 * min(1.0, done / total), 3))
        elif d.get("status") == "finished":
            mark(status="converting", progress=0.27)

    opts = {
        "format": "bestaudio/best",
        "outtmpl": f"/data/{job_id}/input.%(ext)s",
        "noplaylist": True,      # link kèm &list= thì chỉ lấy đúng video đó
        "playlist_items": "1",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "progress_hooks": [hook],
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
    }

    cookies = _cookie_file()
    if cookies:
        opts["cookiefile"] = cookies
    proxy = _proxy_url()
    if proxy:
        opts["proxy"] = proxy
    if player_client:
        opts["extractor_args"] = {"youtube": {"player_client": list(player_client)}}

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(target, download=False)

        # ytsearch: và link playlist đều trả về kiểu "playlist" — lấy mục đầu.
        if info.get("_type") == "playlist" or "entries" in info:
            entries = [e for e in (info.get("entries") or []) if e]
            if not entries:
                raise RuntimeError("Không tìm thấy bài nào khớp với link này.")
            info = entries[0]

        duration = info.get("duration") or 0
        if duration and duration > MAX_SOURCE_SECONDS:
            raise RuntimeError(
                f"Bài dài {int(duration // 60)} phút, quá mốc "
                f"{MAX_SOURCE_SECONDS // 60} phút. Cắt ngắn rồi tải file lên."
            )

        mark(
            title=info.get("title") or "",
            uploader=info.get("uploader") or info.get("channel") or "",
            duration=duration or None,
            webpage_url=info.get("webpage_url") or target,
        )

        ydl.extract_info(info.get("webpage_url") or target, download=True)

    return info


def _find_downloaded(job_id: str) -> str:
    """Tìm file yt-dlp vừa ghi ra. Ưu tiên .mp3 do postprocessor sinh."""
    found = [
        p for p in glob.glob(f"/data/{job_id}/input.*") if os.path.isfile(p)
    ]
    if not found:
        raise RuntimeError("Tải xong nhưng không thấy file audio nào trên đĩa.")
    found.sort(key=lambda p: (os.path.splitext(p)[1].lower() != ".mp3", p))
    return found[0]


def _clear_partials(job_id: str) -> None:
    """Xoá file dở của lần thử trước.

    Mỗi lần thử dùng chung một outtmpl, nên mảnh .part còn sót lại có thể làm
    _find_downloaded() vớ nhầm file hỏng của lần trước.
    """
    for path in glob.glob(f"/data/{job_id}/input.*"):
        try:
            os.remove(path)
        except OSError:
            pass


def _download_with_fallbacks(job_id: str, target: str, mark, kind: str = "youtube",
                             tried: list | None = None) -> dict:
    """Thử lần lượt các player client cho tới khi qua được màn chặn bot.

    Chỉ đổi client khi lỗi đúng là bị chặn bot; lỗi khác (video riêng tư, bài
    quá dài, mạng hỏng) thì ném ra ngay — thử lại chỉ tổ mất thêm vài chục giây
    rồi cũng hỏng y như vậy.

    Chuỗi client là thứ riêng của YouTube (extractor_args gắn khoá "youtube").
    Nguồn khác thì thử lại y hệt 4 lần, chỉ mất thời gian — nên chạy một lần.
    """
    chain = PLAYER_CLIENT_CHAIN if kind in ("youtube", "spotify") else (None,)
    if tried is None:
        tried = []

    last = None
    for attempt, client in enumerate(chain):
        if attempt:
            _clear_partials(job_id)
            mark(status="downloading", progress=0.05,
                 attempt=attempt + 1, player_client=",".join(client))
        tried.append(",".join(client) if client else "mặc định")
        try:
            return _ytdlp_download(job_id, target, mark, player_client=client)
        except Exception as exc:  # noqa: BLE001
            if not _looks_like_bot_check(str(exc)):
                raise
            last = exc
            # Ghi vào trạng thái job để /jobs/{id} soi được sau, không chỉ nằm
            # trong log Modal.
            mark(tried_clients=list(tried))

    raise last if last else RuntimeError("Không tải được, không rõ nguyên nhân.")


@app.function(volumes={"/data": data_vol, "/models": models_vol}, timeout=1200, retries=0)
def fetch_and_separate(job_id: str, url: str, model_key: str):
    """Tải nhạc từ link rồi chuyển tiếp sang worker GPU.

    Tách khỏi separate() để không giữ GPU trong lúc chờ mạng — tải một bài mất
    10–60 giây, trả tiền A10G cho quãng đó là phí.
    """
    def mark(**kw):
        job = jobs.get(job_id, {})
        job.update(kw)
        jobs[job_id] = job

    # Gán trước khi vào try: nhánh báo lỗi bên dưới đọc kind, mà nếu hỏng ngay ở
    # mark() đầu tiên thì kind chưa kịp có giá trị — NameError sẽ che mất lỗi thật.
    kind = "youtube"
    tried: list[str] = []

    try:
        mark(status="resolving", progress=0.02, started_at=time.time())

        kind = _classify_link(url)
        if kind == "spotify":
            query = _spotify_query(url)
            mark(query=query)
            target = f"ytsearch1:{query}"
        else:
            target = url

        data_vol.reload()
        os.makedirs(f"/data/{job_id}", exist_ok=True)
        mark(status="downloading", progress=0.05)

        info = _download_with_fallbacks(job_id, target, mark, kind, tried)

        path = _find_downloaded(job_id)
        size = os.path.getsize(path)
        if size == 0:
            raise RuntimeError("File tải về rỗng.")

        ext = os.path.splitext(path)[1].lower()
        if ext not in ALLOWED_EXTS:
            raise RuntimeError(f"Định dạng tải về không dùng được: {ext}")

        # separate() đọc theo _input_path(job_id, ext) nên tên phải khớp đúng.
        expected = _input_path(job_id, ext)
        if path != expected:
            shutil.move(path, expected)

        data_vol.commit()

        title = info.get("title") or "audio"
        mark(
            status="queued",
            progress=0.30,
            ext=ext,
            filename=f"{title}{ext}",
            downloaded_bytes=size,
        )
        separate.spawn(job_id, model_key, ext)
    except ValueError as exc:
        # Lỗi do link người dùng đưa vào — nói thẳng, không bọc thêm.
        mark(status="error", error=str(exc)[:400], finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        mark(status="error", error=_friendly_download_error(exc, kind, tried),
             finished_at=time.time())
        raise


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
            # Cờ để frontend biết backend này có /jobs/{id}/source. Job cũ (hoặc
            # backend cũ) thiếu khoá này thì nút tải bản gốc ẩn đi, thay vì hiện
            # ra rồi bấm vào ăn 404.
            source_ext=ext,
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
    from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
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
        return {
            "ok": True,
            "app": APP_NAME,
            "models": list(MODELS),
            # Frontend dùng cờ này để ẩn tab "Dán link" nếu backend còn bản cũ.
            "link_import": True,
            "link_sources": ["youtube", "spotify", "tiktok"],
            "max_source_seconds": MAX_SOURCE_SECONDS,
        }

    @web.get("/diag")
    def diag():
        """Khởi động một container GPU và báo cáo nó nạp được gì.

        Chặn tới khi container chạy xong nên có thể mất 1-2 phút lần đầu.
        """
        try:
            return {"ok": True, "probe": gpu_probe.remote()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:600]}

    @web.get("/diag/cookies")
    def diag_cookies():
        """Cookie đã nạp chưa, còn hạn bao lâu. Không trả về giá trị cookie."""
        data_vol.reload()   # container API có thể đang giữ view cũ của volume
        return _cookie_status()

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

    @web.post("/jobs/link")
    def create_link_job(payload: dict = Body(...)):
        """Nhận link YouTube/Spotify, trả job_id ngay rồi tải nền.

        Không tải trong request này: một bài mất 10–60 giây, giữ kết nối HTTP
        lâu vậy là cầm chắc timeout ở proxy hoặc trên 4G.
        """
        url = str(payload.get("url") or "").strip()
        model = str(payload.get("model") or "roformer")

        if model not in MODELS:
            raise HTTPException(400, f"Model không hợp lệ: {model}")
        if not url:
            raise HTTPException(400, "Chưa dán link.")
        if len(url) > 2000:
            raise HTTPException(400, "Link quá dài.")

        try:
            kind = _classify_link(url)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        job_id = uuid.uuid4().hex
        os.makedirs(f"/data/{job_id}", exist_ok=True)
        data_vol.commit()

        jobs[job_id] = {
            "status": "resolving",
            "progress": 0.0,
            "model": model,
            "source": kind,
            "source_url": url,
            "filename": None,
            "ext": DEFAULT_EXT,
            "created_at": time.time(),
        }
        fetch_and_separate.spawn(job_id, url, model)
        return {"job_id": job_id, "source": kind}

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

        base = _safe_download_name(job.get("filename"))
        ext = os.path.splitext(filename)[1]
        return FileResponse(
            path,
            media_type=_media_type(ext),
            filename=f"{base} - {stem}{ext}",
        )

    @web.get("/jobs/{job_id}/source")
    def download_source(job_id: str):
        """Trả về nguyên bài chưa tách — cùng file mà worker GPU đã đọc vào.

        Dùng chung cho cả hai cách tách: file tự tải lên và bài máy chủ tải từ
        link đều nằm ở một chỗ. Với cách 2 đây là đường duy nhất lấy được bản
        gốc, vì bài chưa từng đi qua máy người dùng.
        """
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Không tìm thấy job.")

        ext = job.get("ext") or DEFAULT_EXT
        data_vol.reload()
        path = _input_path(job_id, ext)
        if not os.path.exists(path):
            # Job từ link chưa tải xong thì chưa có gì để trả — khác hẳn với
            # file đã bị dọn sau 24 giờ, nên tách hai mã lỗi.
            if job.get("status") in ("resolving", "downloading"):
                raise HTTPException(409, "Máy chủ chưa tải xong bản gốc.")
            raise HTTPException(410, "File đã bị xoá.")

        base = _safe_download_name(job.get("filename"))
        return FileResponse(path, media_type=_media_type(ext), filename=f"{base}{ext}")

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
