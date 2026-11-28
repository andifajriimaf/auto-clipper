"""FastAPI app: API endpoints + layankan UI statis + file hasil klip."""
import logging
import os
import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import channels, downloader, jobs, renderer, settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
log = logging.getLogger("main")

_STATIC = Path(__file__).resolve().parent / "static"
_UPLOAD_EXTS = (".mp4", ".mkv", ".webm", ".mov", ".avi")

app = FastAPI(title="AutoClipper", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    channels.start_auto_loop()


# ------------------------------------------------------------------ models
class PreviewReq(BaseModel):
    url: str


class AnalyzeReq(BaseModel):
    """Fase 1: cari rekomendasi momen (unduh + transkripsi + analisis AI).

    Sumber video: url YouTube ATAU upload_id dari /api/upload.
    """
    url: str = ""
    upload_id: str | None = None
    kind: str = "analyze"                # analyze | auto_clip (channel monitor)
    platforms: list[str] = Field(default=["shorts"])
    resolution: str = "720"              # 720 | 1080
    ratio: str = "9:16"                  # 9:16 | 1:1 | 16:9
    layout: str = "blur"                 # blur | crop
    clip_mode: str = "summary"           # summary (rangkuman) | single (potongan)
    clip_count: int = Field(3, ge=1, le=10)
    clip_duration: int = Field(30, ge=15, le=60)   # 15 | 30 | 60
    subtitles: bool = True
    subtitle_style: str = "karaoke"      # klasik | karaoke


class RenderReq(BaseModel):
    """Fase 2: render klip dari momen terpilih (tanpa unduh ulang)."""
    analysis_id: str
    moments: list[dict]
    platforms: list[str] = Field(default=["shorts"])
    resolution: str = "720"
    ratio: str = "9:16"
    layout: str = "blur"                 # blur | crop
    clip_mode: str = "summary"           # hanya untuk label/log
    subtitles: bool = True
    subtitle_style: str = "karaoke"


class SettingsReq(BaseModel):
    zai_api_key: str | None = None
    zai_model: str | None = None
    whisper_model: str | None = None
    cookies_browser: str | None = None
    cookies_file: str | None = None
    render_encoder: str | None = None


# ------------------------------------------------------------------ api
@app.get("/api/health")
def health():
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    try:
        import faster_whisper  # noqa: F401
        whisper_ok = True
    except ImportError:
        whisper_ok = False
    try:
        import yt_dlp  # noqa: F401
        ytdlp_ok = True
    except ImportError:
        ytdlp_ok = False
    return {
        "ok": bool(ffmpeg and ffprobe and whisper_ok and ytdlp_ok),
        "ffmpeg": bool(ffmpeg), "ffprobe": bool(ffprobe),
        "whisper": whisper_ok, "yt_dlp": ytdlp_ok,
        "api_key_set": bool(settings.get("zai_api_key")),
    }


@app.post("/api/preview")
def preview(req: PreviewReq):
    url = req.url.strip()
    if not downloader.is_valid_url(url):
        raise HTTPException(400, "Link YouTube tidak valid")
    try:
        return downloader.get_metadata(url)
    except RuntimeError as e:
        # downloader sudah menerjemahkan error ke pesan ramah bahasa Indonesia
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"Gagal membaca video: {str(e)[:200]}")


@app.post("/api/upload")
async def upload_video(file: UploadFile = File(...)):
    """Upload video lokal untuk di-clip tanpa link YouTube."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in _UPLOAD_EXTS:
        raise HTTPException(400, "Format harus mp4/mkv/webm/mov/avi")
    uid = uuid.uuid4().hex[:12]
    dest = Path(settings.get("output_dir")) / "uploads" / uid
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"source{ext}"
    size = 0
    try:
        with path.open("wb") as w:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                w.write(chunk)
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(500, "Upload gagal tersimpan")
    dur = renderer.probe_duration(path)
    if dur < 30:
        shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(400, f"Video terlalu pendek ({dur:.0f}s) — minimal 30 detik")
    return {"upload_id": uid, "filename": file.filename,
            "duration": int(dur), "size_mb": round(size / 1e6, 1)}


@app.post("/api/analyze")
def start_analyze(req: AnalyzeReq):
    url = req.url.strip()
    if not req.upload_id and not downloader.is_valid_url(url):
        raise HTTPException(400, "Link YouTube tidak valid — atau pakai upload video")
    if req.ratio not in ("9:16", "1:1", "16:9"):
        raise HTTPException(400, "Rasio tidak didukung")
    if req.layout not in ("blur", "crop", "crop_face"):
        raise HTTPException(400, "Layout tidak didukung")
    if req.upload_id:
        # pastikan file upload masih ada
        up = Path(settings.get("output_dir")) / "uploads" / req.upload_id
        if not up.is_dir() or not any(up.iterdir()):
            raise HTTPException(400, "File upload tidak ditemukan — ulangi upload")
    kind = req.kind if req.kind in ("analyze", "auto_clip") else "analyze"
    job_id = jobs.create_job({**req.model_dump(), "kind": kind})
    return {"job_id": job_id}


@app.post("/api/jobs")
def create_render_job(req: RenderReq):
    analysis_dir = Path(settings.get("output_dir")) / req.analysis_id
    if not (analysis_dir / "analysis.json").is_file():
        raise HTTPException(400, "Analisis tidak ditemukan — ulangi pencarian momen")
    if not req.moments:
        raise HTTPException(400, "Pilih minimal satu momen dulu")
    job_id = jobs.create_job(req.model_dump() | {"kind": "render"})
    return {"job_id": job_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job tidak ditemukan")
    # params tidak perlu dikirim balik lengkap
    job.pop("params", None)
    return job


@app.get("/api/jobs")
def recent_jobs():
    return [j for j in jobs.list_jobs()]


# ------------------------------------------------------------------ channels
class ChannelReq(BaseModel):
    url: str


@app.get("/api/channels")
def get_channels():
    return {"channels": channels.list_channels(), "auto": channels.is_auto()}


@app.post("/api/channels")
def add_channel(req: ChannelReq):
    try:
        ch = channels.add_channel(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return ch


@app.delete("/api/channels/{cid}")
def delete_channel(cid: str):
    channels.remove_channel(cid)
    return {"ok": True}


@app.post("/api/channels/auto")
def set_channel_auto(req: dict):
    channels.set_auto(bool(req.get("enabled")))
    channels.start_auto_loop()
    return {"auto": channels.is_auto()}


@app.post("/api/channels/{cid}/check")
def check_channel(cid: str):
    try:
        return channels.check_channel(cid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/channels/check-all")
def check_all_channels():
    try:
        return channels.check_all(force=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)[:200])


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str):
    if not jobs.cancel_job(job_id):
        raise HTTPException(400, "Job sudah berjalan/selesai — tidak bisa dibatalkan")
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return settings.masked()


@app.post("/api/settings")
def set_settings(req: SettingsReq):
    settings.update(**req.model_dump(exclude_none=True))
    return settings.masked()


@app.post("/api/open-folder")
def open_folder(job_id: str | None = None):
    """Buka folder hasil di Explorer Windows."""
    target = Path(settings.get("output_dir"))
    if job_id:
        target = target / job_id
    if not target.exists():
        raise HTTPException(404, "Folder belum ada")
    os.startfile(str(target))  # noqa: S606 — Windows-only, by design
    return {"ok": True}


# ------------------------------------------------------------------ static
@app.get("/files/{job_id}/{name}")
def get_file(job_id: str, name: str):
    # name dibatasi supaya tidak bisa path traversal
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(400, "Nama file tidak valid")
    p = Path(settings.get("output_dir")) / job_id / name
    if not p.is_file():
        raise HTTPException(404, "File tidak ditemukan")
    media = "video/mp4" if name.endswith(".mp4") else "image/jpeg"
    return FileResponse(p, media_type=media, filename=name)


app.mount("/", StaticFiles(directory=_STATIC, html=True), name="ui")
