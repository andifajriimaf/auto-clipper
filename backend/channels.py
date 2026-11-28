"""Channel monitor: pantau channel YouTube, auto-clip video baru.

- User tambah channel (URL @handle / /channel/UC...)
- "Cek sekarang" atau loop otomatis tiap 30 menit: bandingkan daftar video
  terbaru dengan yang sudah diproses → video baru langsung masuk antrean
  job auto_clip (analisis + render otomatis semua momen terbaik).
"""
import logging
import threading
import time
import uuid

import yt_dlp

from . import downloader, jobs, settings

log = logging.getLogger(__name__)

_CHECK_INTERVAL_S = 1800  # 30 menit
_MAX_VIDEOS_PER_CHECK = 2
_MAX_AUTO_DURATION_S = 3600  # auto-clip channel: video lebih panjang dari 1 jam di-skip
_AUTO_THREAD: threading.Thread | None = None


def list_channels() -> list[dict]:
    return list(settings.get("channels") or [])


def _save(channels: list[dict]) -> None:
    settings.update(channels=channels)


def set_auto(enabled: bool) -> None:
    settings.update(channel_auto=bool(enabled))


def is_auto() -> bool:
    return bool(settings.get("channel_auto"))


def _normalize_channel_url(url: str) -> str:
    """Channel root → tab /videos, supaya extract_flat mengembalikan VIDEO
    asli, bukan entry tab (Videos/Live/Shorts) yang bukan video."""
    url = url.strip().rstrip("/")
    if not url.endswith(("/videos", "/streams", "/shorts", "/featured", "/live")):
        url += "/videos"
    return url


def add_channel(url: str) -> dict:
    url = url.strip()
    if "youtube.com/" not in url and "youtu.be/" not in url:
        raise ValueError("Masukkan URL channel YouTube (mis. youtube.com/@nama)")
    fetch_url = _normalize_channel_url(url)
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "playlistend": 1,
            **downloader.base_opts()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(fetch_url, download=False)
    except Exception as e:  # noqa: BLE001 — pesan ramah dari downloader bila perlu
        raise ValueError(f"Channel tidak bisa dibaca: {str(e)[:150]}") from e
    name = info.get("channel") or info.get("uploader") or info.get("title") or url
    cid = uuid.uuid4().hex[:8]
    ch = {"id": cid, "url": url, "fetch_url": fetch_url, "name": name,
          "last_ids": [], "last_check": 0}
    chs = list_channels()
    if any(c["url"] == url for c in chs):
        raise ValueError("Channel ini sudah ada di daftar")
    chs.append(ch)
    _save(chs)
    return ch


def remove_channel(cid: str) -> None:
    _save([c for c in list_channels() if c["id"] != cid])


def _fetch_videos(ch: dict) -> list[dict]:
    """Daftar video terbaru channel (id, title, duration)."""
    fetch_url = _normalize_channel_url(ch.get("fetch_url") or ch["url"])
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "extract_flat": "in_playlist", "playlistend": 8,
            **downloader.base_opts()}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(fetch_url, download=False)
    out = []
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        # video asli punya id 11 karakter; entry tab/playlist bukan
        if len(e["id"]) != 11 or e["id"].startswith("UC"):
            continue
        dur = e.get("duration") or 0
        if dur and dur < 30:
            continue  # terlalu pendek untuk di-clip
        if dur and dur > _MAX_AUTO_DURATION_S:
            continue  # terlalu panjang untuk auto-clip (transkripsi berjam-jam)
        out.append({"id": e["id"], "title": e.get("title") or e["id"],
                    "url": f"https://www.youtube.com/watch?v={e['id']}",
                    "duration": int(dur) if dur else 0})
    return out


def _enqueue(video: dict, channel_name: str) -> str:
    params = {
        "url": video["url"], "kind": "auto_clip",
        "platforms": ["shorts"], "resolution": "720", "ratio": "9:16",
        "layout": "blur", "clip_mode": "summary",
        "clip_count": 3, "clip_duration": 30,
        "subtitles": True, "subtitle_style": "karaoke",
        "source_title": video["title"], "source_channel": channel_name,
    }
    return jobs.create_job(params)


def check_channel(cid: str) -> dict:
    """Cari video baru channel → enqueuing job auto_clip (maks 2 per cek)."""
    chs = list_channels()
    ch = next((c for c in chs if c["id"] == cid), None)
    if not ch:
        raise ValueError("Channel tidak ditemukan")

    videos = _fetch_videos(ch)
    seen = set(ch.get("last_ids") or [])
    fresh = [v for v in videos if v["id"] not in seen]

    created = []
    for v in fresh[:_MAX_VIDEOS_PER_CHECK]:
        jid = _enqueue(v, ch["name"])
        created.append({"job_id": jid, "title": v["title"]})
        jobs.log_line(jid, f"Auto-clip dari channel {ch['name']}: {v['title']}")

    ch["last_ids"] = [v["id"] for v in videos][-50:]  # catat yang sudah dilihat
    ch["last_check"] = time.time()
    _save(chs)
    return {"channel": ch["name"], "found": len(fresh),
            "processed": len(created), "jobs": created}


def check_all(force: bool = False) -> list[dict]:
    """Cek semua channel (dipakai tombol manual & loop otomatis)."""
    results = []
    for ch in list_channels():
        try:
            r = check_channel(ch["id"])
            if r["found"] or force:
                results.append(r)
        except Exception as e:  # noqa: BLE001 — satu channel gagal tak apa
            log.warning("Cek channel %s gagal: %s", ch["name"], e)
    return results


def _auto_loop() -> None:
    while True:
        time.sleep(_CHECK_INTERVAL_S)
        try:
            if is_auto():
                check_all(force=False)
        except Exception:  # noqa: BLE001 — loop jalan selamanya
            pass


def start_auto_loop() -> None:
    global _AUTO_THREAD
    if _AUTO_THREAD is not None and _AUTO_THREAD.is_alive():
        return
    _AUTO_THREAD = threading.Thread(target=_auto_loop, daemon=True,
                                    name="channel-auto")
    _AUTO_THREAD.start()
