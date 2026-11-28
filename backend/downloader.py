"""Download video YouTube via yt-dlp + ambil metadata."""
import re
import subprocess
from pathlib import Path

import yt_dlp

from . import jobs, settings

# Pola URL YouTube yang didukung: watch, youtu.be, shorts, embed, live
_URL_RE = re.compile(
    r"^(https?://)?(www\.|m\.|music\.)?"
    r"(youtube\.com/(watch\?v=[\w-]{11}|shorts/[\w-]{11}|embed/[\w-]{11}|live/[\w-]{11})"
    r"|youtu\.be/[\w-]{11})"
    r"(&\S*)?$"
)

_WIN_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def is_valid_url(url: str) -> bool:
    return bool(_URL_RE.match(url.strip()))


def base_opts() -> dict:
    """Opsi yt-dlp dasar + cookies (untuk lolos deteksi bot YouTube).

    Sumber cookies bisa dua: dari browser terpasang (cookies_browser) atau
    file cookies.txt hasil ekspor ekstensi (cookies_file). Kalau dua-duanya
    kosong → akses anonim.
    """
    opts: dict = {}
    browser = (settings.get("cookies_browser") or "").strip().lower()
    cookie_file = (settings.get("cookies_file") or "").strip()
    if cookie_file and Path(cookie_file).is_file():
        opts["cookiefile"] = cookie_file
    elif browser:
        opts["cookiesfrombrowser"] = (browser,)
    return opts


def _friendly_error(e: Exception) -> str:
    """Terjemahkan error yt-dlp ke pesan Indonesia yang bisa ditindaklanjuti."""
    msg = str(e)
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low or "unavailable" in low:
        return ("Video diblokir deteksi bot YouTube (video mungkin tetap ada di YouTube). "
                "Solusi: isi Cookies di ⚙️ Pengaturan — ekspor cookies.txt dari browser "
                "yang sudah login YouTube (pakai ekstensi 'Get cookies.txt LOCALLY'), "
                "lalu isi path file-nya di pengaturan.")
    if "age" in low and ("restrict" in low or "confirm" in low):
        return ("Video dibatasi usia — butuh cookies akun YouTube yang sudah login "
                "di ⚙️ Pengaturan.")
    if "private" in low:
        return "Video bersifat privat"
    if "unavailable" in low:
        return "Video tidak ditemukan / sudah dihapus"
    return f"Gagal membaca video: {msg[:200]}"


def get_metadata(url: str) -> dict:
    """Metadata ringan tanpa download (judul, durasi, thumbnail, channel)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, **base_opts()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:  # noqa: BLE001 — dilempar dengan pesan ramah
        raise RuntimeError(_friendly_error(e)) from e
    return {
        "id": info.get("id", ""),
        "title": info.get("title", "Tanpa judul"),
        "duration": int(info.get("duration") or 0),
        "channel": info.get("uploader") or info.get("channel") or "",
        "thumbnail": info.get("thumbnail") or "",
        "url": url,
    }


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    h, m, s = seconds // 3600, (seconds % 3600) // 60, seconds % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _probe_height(path: Path) -> int:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True,
            creationflags=_WIN_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        return int(float(p.stdout.strip()))
    except Exception:  # noqa: BLE001 — probe gagal bukan fatal
        return 0


def download(url: str, dest_dir: Path, max_height: int, job_id: str) -> Path:
    """Unduh video (tinggi <= max_height). Progress di-report 0..30%.

    Codec apa pun diterima (avc1/vp9/av1) agar resolusi tertinggi yang
    tersedia selalu terpakai — hasil digabung ke mkv. Tanpa ini, video tertentu
    jatuh ke format progresif rendah (720p) dan render 1080p terlihat pecah.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    def hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            if total:
                pct = min(0.30, 0.30 * done / total)
                jobs.update(job_id, progress=round(pct, 1),
                            message=f"Mengunduh video {done / 1e6:.0f}/{total / 1e6:.0f} MB")
        elif d.get("status") == "finished":
            jobs.log_line(job_id, "Unduhan selesai, menggabungkan video+audio…")

    opts = {
        "quiet": True,
        "no_warnings": True,
        "outtmpl": str(dest_dir / "source.%(ext)s"),
        # resolusi tertinggi yang tersedia <= max_height, codec bebas
        "format": f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
        "merge_output_format": "mkv",
        "progress_hooks": [hook],
        "noplaylist": True,
        "retries": 3,
        "concurrent_fragment_downloads": 8,
        # solver challenge YouTube (butuh deno; komponen resmi dari GitHub yt-dlp)
        "remote_components": ["ejs:github"],
        **base_opts(),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:  # noqa: BLE001 — dilempar dengan pesan ramah
        raise RuntimeError(_friendly_error(e)) from e

    # Cari file hasil (source.mp4 / source.mkv / source.webm)
    video_path = None
    for cand in sorted(dest_dir.glob("source.*")):
        if cand.suffix.lower() in (".mp4", ".mkv", ".webm"):
            video_path = cand
            break
    if not video_path:
        raise RuntimeError("File video hasil unduhan tidak ditemukan")

    src_h = _probe_height(video_path)
    jobs.log_line(job_id, f"Video tersimpan: {video_path.name} "
                          f"({video_path.stat().st_size / 1e6:.0f} MB, durasi {_fmt(info.get('duration') or 0)}, "
                          f"resolusi sumber: {src_h}p)")
    if src_h and src_h < max_height:
        jobs.log_line(job_id, f"[INFO] YouTube hanya menyediakan {src_h}p untuk video ini — "
                              f"output {max_height}p akan di-upscale (hasil kurang tajam). "
                              f"Pilih resolusi {src_h}p agar sesuai sumber.")
    return video_path
