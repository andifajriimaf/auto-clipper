"""Pipeline dua fase:

- analyze: unduh → transkripsi → analisis AI → rekomendasi momen (job kind=analyze)
- render : momen terpilih → layout rasio + burn subtitle → klip (job kind=render)

Hasil analyze (video sumber + transkrip) di-cache di output/<analysis_id>/
agar render tidak perlu mengunduh & mentranskripsi ulang.
"""
import json
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import analyzer, downloader, jobs, renderer, settings, transcriber

log = logging.getLogger(__name__)

# Ukuran output per rasio per resolusi (lebar, tinggi)
RATIO_SIZES = {
    "9:16": {"720": (720, 1280), "1080": (1080, 1920)},
    "1:1": {"720": (720, 720), "1080": (1080, 1080)},
    "16:9": {"720": (1280, 720), "1080": (1920, 1080)},
}

# Ukuran kecil untuk video preview di kartu rekomendasi momen
# (bukan resolusi final — hasil render tetap mengikuti pilihan resolusi user)
PREVIEW_SIZES = {"9:16": (360, 640), "1:1": (480, 480), "16:9": (640, 360)}

PLATFORM_META = {
    "reels":  {"label": "Instagram Reels", "tags": "#reels #reelsindonesia #viral #explore"},
    "tiktok": {"label": "TikTok",          "tags": "#fyp #foryou #viral #tiktokindonesia"},
    "shorts": {"label": "YouTube Shorts",  "tags": "#shorts #shortsindonesia #viral"},
}

_CACHE_MAX_AGE_S = 2 * 3600  # cache analysis > 2 jam dibuang saat analyze baru

# Paralelisme render: beberapa ffmpeg sekaligus, thread dibagi rata biar
# tidak berebut seluruh core CPU. Saat encoder hardware (QSV) aktif, worker
# dibatasi 2 — encode hardware sudah cepat, dan iGPU yang sama juga menampilkan
# layar: terlalu banyak sesi encode membuat layar kedap-kedip.
_CPU = __import__("os").cpu_count() or 4


def _render_workers() -> int:
    if renderer._detect_hw_encoder()["type"] == "qsv":
        return 2
    return min(4, max(2, _CPU // 2))


def _ffmpeg_threads() -> int:
    return max(1, _CPU // _render_workers())


def run(job_id: str) -> None:
    params = jobs.get_job(job_id)["params"]
    kind = params.get("kind", "analyze")
    if kind == "analyze":
        run_analyze(job_id, params)
    elif kind == "auto_clip":
        _run_auto_clip(job_id, params)
    else:
        run_render(job_id, params)


def _run_auto_clip(job_id: str, params: dict) -> None:
    """Satu job penuh untuk channel monitor: analisis → render top momen otomatis."""
    run_analyze(job_id, params)
    job = jobs.get_job(job_id)
    moments = sorted(job.get("moments") or [], key=lambda m: -m.get("score", 0))
    top = moments[:max(1, int(params.get("clip_count", 3)))]
    if not top:
        raise RuntimeError("Tidak ada momen untuk di-render otomatis dari video ini")
    jobs.log_line(job_id, f"Auto-clip: merender {len(top)} klip terbaik otomatis")
    run_render(job_id, {**params, "moments": top, "analysis_id": job_id},
               progress_start=66.0)


# ------------------------------------------------------------------ fase 1
def run_analyze(job_id: str, params: dict) -> None:
    url = params.get("url", "").strip()
    upload_id = params.get("upload_id") or ""

    # ---------- sumber: file upload lokal ATAU link YouTube
    if upload_id:
        up_dir = Path(settings.get("output_dir")) / "uploads" / upload_id
        video_path = next((p for p in sorted(up_dir.glob("source.*"))), None)
        if not video_path:
            raise ValueError("File upload tidak ditemukan — ulangi upload")
    elif downloader.is_valid_url(url):
        video_path = None  # diunduh di bawah
    else:
        raise ValueError("Link YouTube tidak valid. Contoh: https://www.youtube.com/watch?v=XXXXXXXXXXX")

    _cleanup_stale_caches()

    n_proposals = max(1, min(10, int(params.get("clip_count", 3)))) + 2  # proposal ekstra buat pilihan

    out_root = Path(settings.get("output_dir")) / job_id
    tmp_dir = out_root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    jobs.log_line(job_id, f"Analisis dimulai: cari {n_proposals} kandidat momen terbaik")

    # ---------- 1. sumber video (0-30%)
    if upload_id:
        jobs.update(job_id, stage="unduh", stage_label="Menyiapkan file", progress=28.0,
                    message="File lokal siap")
        meta = {
            "title": video_path.stem,
            "duration": int(renderer.probe_duration(video_path)),
            "channel": "upload lokal",
            "thumbnail": "",
            "url": "",
        }
        jobs.log_line(job_id, f"Video lokal: {video_path.name} ({meta['duration'] // 60}m{meta['duration'] % 60:02d}s)")
    else:
        jobs.update(job_id, stage="unduh", stage_label="Mengunduh video", progress=1.0,
                    message="Menghubungi YouTube…")
        meta = downloader.get_metadata(url)
        jobs.log_line(job_id, f"Video: {meta['title']} ({meta['duration'] // 60}m{meta['duration'] % 60:02d}s) — {meta['channel']}")

    jobs.update(job_id, video=meta)
    if meta["duration"] < 30:
        raise ValueError("Video terlalu pendek (minimal 30 detik) untuk di-auto-clip.")
    if meta["duration"] > 3 * 3600:
        raise ValueError("Video terlalu panjang (maksimal 3 jam) untuk di-auto-clip.")

    if not upload_id:
        video_path = downloader.download(url, tmp_dir, max_height=1080, job_id=job_id)

    # ---------- 2. transkripsi (30-55%)
    try:
        segments = transcriber.transcribe(video_path, job_id)
    except Exception as e:
        raise RuntimeError(f"Transkripsi gagal: {e}") from e
    if not segments:
        raise RuntimeError("Tidak ada ucapan yang terdeteksi di video ini — "
                           "subtitle & pemilihan momen butuh audio bicara.")

    # ---------- 3. analisis (55-65%)
    jobs.update(job_id, stage="analisis", stage_label="Mencari momen terbaik",
                progress=55.0, message="Menganalisis transkrip…")
    moments = analyzer.analyze(segments, meta["duration"],
                               {**params, "clip_count": n_proposals}, job_id)
    for i, m in enumerate(moments, 1):
        info = f"{len(m.get('chunks', []))} potongan, " if m.get("chunks") else ""
        jobs.log_line(job_id, f"Momen {i}: {m['start']:.0f}s–{m['end']:.0f}s ({info}"
                              f"{m.get('duration', m['end'] - m['start']):.0f}s) — {m['title']} ({m['source']})")

    # ---------- preview video per momen (rendah-res, pakai rasio & subtitle pilihan)
    jobs.update(job_id, stage="analisis", stage_label="Membuat preview momen",
                progress=62.0, message="Merender preview momen…")
    moments = _render_previews(job_id, moments, video_path, tmp_dir, out_root, params, segments)

    # ---------- cache hasil analyze untuk fase render
    (out_root / "analysis.json").write_text(
        json.dumps({"segments": segments, "moments": moments, "meta": meta},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    jobs.update(job_id, moments=moments,
                message=f"{len(moments)} rekomendasi momen siap")
    jobs.log_line(job_id, "Rekomendasi siap — pilih momen lalu buat klip")


# ------------------------------------------------------------------ fase 2
def run_render(job_id: str, params: dict, progress_start: float = 5.0) -> None:
    analysis_id = params.get("analysis_id", "")
    out_root = Path(settings.get("output_dir"))
    analysis_dir = out_root / analysis_id
    analysis_file = analysis_dir / "analysis.json"
    if not analysis_file.is_file():
        raise ValueError("Data analisis tidak ditemukan / sudah kedaluwarsa — ulangi pencarian momen")

    data = json.loads(analysis_file.read_text(encoding="utf-8"))
    segments = data["segments"]
    meta = data.get("meta", {})

    video_path = next(
        (p for p in sorted((analysis_dir / "tmp").glob("source.*"))
         if p.suffix.lower() in (".mp4", ".mkv", ".webm")),
        None,
    )
    if not video_path:
        raise ValueError("Video sumber dari analisis sudah tidak ada — ulangi pencarian momen")

    moments = [m for m in params.get("moments", []) if _valid_moment(m)]
    if not moments:
        raise ValueError("Tidak ada momen yang dipilih — centang minimal satu momen")

    res_key = str(params.get("resolution", "720"))
    ratio = params.get("ratio", "9:16")
    layout = params.get("layout", "blur")
    width, height = RATIO_SIZES.get(ratio, RATIO_SIZES["9:16"]).get(res_key, (720, 1280))
    platforms = [p for p in params.get("platforms", ["shorts"]) if p in PLATFORM_META] or ["shorts"]
    sub_on = bool(params.get("subtitles", True))
    sub_style = params.get("subtitle_style", "karaoke")

    out_job = out_root / job_id
    out_job.mkdir(parents=True, exist_ok=True)
    tmp_render = out_job / "tmp"
    tmp_render.mkdir(exist_ok=True)

    jobs.log_line(job_id, f"Render {len(moments)} klip: rasio {ratio}, layout {layout}, "
                          f"resolusi {res_key}p ({width}×{height}), "
                          f"platform {', '.join(platforms)}, subtitle {'ON' if sub_on else 'OFF'}, "
                          f"encoder {renderer._encoder_label()}")
    jobs.update(job_id, stage="render", stage_label="Merender klip",
                progress=progress_start, message="Merender klip…", video=meta)

    audio_words = [w for s in segments for w in (s.get("words") or [])]
    clips = []

    def render_one(i: int, moment: dict) -> dict:
        slug = _slug(moment.get("title") or f"momen-{i}") or f"klip-{i}"
        name = f"klip-{i:02d}-{slug}.mp4"
        out_path = out_job / name
        renderer.render_clip(
            video_path, moment, out_path, tmp_render, width, height,
            {"words": audio_words, "style": sub_style} if sub_on else None, job_id,
            layout=layout, threads=_ffmpeg_threads(),
        )
        thumb = out_job / f"klip-{i:02d}-thumb.jpg"
        try:
            renderer.extract_thumb(out_path, min(1.0, (float(moment.get("duration")) or
                                  (float(moment["end"]) - float(moment["start"]))) * 0.25), thumb)
        except Exception:  # noqa: BLE001 — thumbnail nice-to-have
            thumb = None

        hook = moment.get("hook") or moment.get("title", "")
        # caption berbeda per platform: hashtag & gaya bahasa masing-masing
        captions = [
            {"platform": PLATFORM_META[p]["label"], "text": f"{hook}\n\n{PLATFORM_META[p]['tags']}"}
            for p in platforms
        ]
        return {
            "index": i,
            "file": name,
            "title": moment.get("title", "Momen"),
            "hook": moment.get("hook", ""),
            "start": float(moment["start"]),
            "end": float(moment["end"]),
            "chunks": len(moment.get("chunks") or []),
            "duration": round(float(moment.get("duration") or (float(moment["end"]) - float(moment["start"]))), 1),
            "score": moment.get("score", 0.5),
            "source": moment.get("source", "AI"),
            "captions": captions,
            "caption": captions[0]["text"],  # kompatibilitas: caption platform pertama
            "platforms": [PLATFORM_META[p]["label"] for p in platforms],
            "video_url": f"/files/{job_id}/{name}",
            "thumb_url": f"/files/{job_id}/{thumb.name}" if thumb else "",
        }

    with ThreadPoolExecutor(max_workers=_render_workers()) as pool:
        futures = {pool.submit(render_one, i, m): (i, m) for i, m in enumerate(moments, 1)}
        done = 0
        for fut in as_completed(futures):
            i, _ = futures[fut]
            try:
                clips.append(fut.result())
            except Exception as e:  # noqa: BLE001 — satu klip gagal tak harus gagalkan semuanya
                jobs.log_line(job_id, f"[ERROR] Klip {i} gagal dirender: {e}")
            done += 1
            jobs.update(job_id, progress=round(progress_start + (99.0 - progress_start)
                                                * done / len(moments), 1),
                        message=f"Klip {done}/{len(moments)} selesai")

    clips.sort(key=lambda c: c["index"])
    if not clips:
        raise RuntimeError("Semua klip gagal dirender")

    jobs.update(job_id, clips=clips, message=f"{len(clips)} klip berhasil dibuat")
    jobs.log_line(job_id, f"SELESAI — {len(clips)} klip tersimpan di {out_job}")

    try:
        shutil.rmtree(tmp_render, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ util
def _render_previews(job_id: str, moments: list[dict], video_path: Path,
                     tmp_dir: Path, out_root: Path, params: dict,
                     segments: list[dict]) -> list[dict]:
    """Render video preview kecil per momen (rasio, layout, subtitle sesuai pilihan).

    Dipakai UI di kartu rekomendasi supaya user bisa menonton momen dulu
    sebelum memilih. Ultrafast + crf 28 agar cepat; ukuran kecil.
    """
    ratio = params.get("ratio", "9:16")
    pw, ph = PREVIEW_SIZES.get(ratio, PREVIEW_SIZES["9:16"])
    layout = params.get("layout", "blur")
    sub_on = bool(params.get("subtitles", True))
    sub_style = params.get("subtitle_style", "karaoke")
    audio_words = [w for s in segments for w in (s.get("words") or [])]
    subtitle = {"words": audio_words, "style": sub_style} if sub_on else None

    def _one(i: int, m: dict) -> dict:
        name = f"preview-{i:02d}.mp4"
        out = out_root / name
        try:
            renderer.render_clip(video_path, m, out, tmp_dir, pw, ph, subtitle,
                                 job_id, crf=24, preset="ultrafast", layout=layout,
                                 threads=_ffmpeg_threads())
            return {**m, "preview_url": f"/files/{job_id}/{name}"}
        except Exception as e:  # noqa: BLE001 — preview gagal ≠ analisis gagal
            jobs.log_line(job_id, f"[WARN] Preview momen {i} gagal: {e}")
            return m

    with ThreadPoolExecutor(max_workers=_render_workers()) as pool:
        futures = [pool.submit(_one, i, m) for i, m in enumerate(moments, 1)]
        moments = [f.result() for f in futures]
    jobs.log_line(job_id, "Preview momen selesai dibuat")
    return moments


def _valid_moment(m: dict) -> bool:
    try:
        return float(m["end"]) - float(m["start"]) >= 3
    except (KeyError, TypeError, ValueError):
        return False


def _cleanup_stale_caches() -> None:
    """Buang cache video sumber & upload yang sudah > 2 jam."""
    now = time.time()
    out_root = Path(settings.get("output_dir"))
    if not out_root.exists():
        return
    for tmp in list(out_root.glob("*/tmp")) + list(out_root.glob("uploads/*")):
        try:
            if now - tmp.stat().st_mtime > _CACHE_MAX_AGE_S:
                shutil.rmtree(tmp, ignore_errors=True)
        except OSError:
            continue


def _slug(text: str) -> str:
    keep = "-".join(w for w in "".join(
        c if c.isalnum() else " " for c in text).split()).lower()
    return keep[:50]
