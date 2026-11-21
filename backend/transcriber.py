"""Transkripsi audio dengan faster-whisper → segmen kata + timestamp.

Cepat: BatchedInferencePipeline + VAD (skip hening/musik). Untuk video musik
(vokal tanpa ucapan terdeteksi VAD) otomatis fallback ke transkripsi biasa.
"""
import logging
import os
import threading
from pathlib import Path

from faster_whisper import BatchedInferencePipeline, WhisperModel

from . import jobs, settings

log = logging.getLogger(__name__)

_model_lock = threading.Lock()
_model_cache: dict[str, WhisperModel] = {}


def get_model(name: str | None = None, job_id: str = "") -> WhisperModel:
    """Muat model sekali, cache untuk job berikutnya (model download otomatis saat pertama)."""
    name = name or settings.get("whisper_model") or "base"
    with _model_lock:
        if name not in _model_cache:
            jobs.log_line(job_id, f"Memuat model Whisper '{name}' (sekali saja, cached)…")
            _model_cache[name] = WhisperModel(
                name, device="cpu", compute_type="int8",
                cpu_threads=min(6, os.cpu_count() or 4),
            )
        return _model_cache[name]


def _collect(segments_iter, info, job_id: str) -> list[dict]:
    """Kumpulkan segmen + report progress (30-54%)."""
    total = max(1.0, info.duration)
    segments: list[dict] = []
    for seg in segments_iter:
        words = [
            {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
            for w in (seg.words or []) if w.word
        ]
        segments.append({
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "text": seg.text.strip(),
            "words": words,
        })
        pct = 30.0 + min(24.0, 24.0 * seg.end / total)
        jobs.update(job_id, progress=round(pct, 1),
                    message=f"Transkripsi {seg.end:.0f}/{total:.0f} detik")
        if len(segments) % 20 == 0:
            jobs.log_line(job_id, f"Transkripsi berjalan… {seg.end:.0f}s / {total:.0f}s")
    return segments


def transcribe(video_path: Path, job_id: str) -> list[dict]:
    model = get_model(job_id=job_id)
    jobs.update(job_id, stage="transkripsi", stage_label="Transkripsi audio",
                progress=30.0, message="Menjalankan Whisper (mode batched)…")

    # jalur cepat: batched + VAD (lewati hening & musik)
    pipe = BatchedInferencePipeline(model=model)
    segments_iter, info = pipe.transcribe(
        str(video_path),
        word_timestamps=True,
        vad_filter=True,
        batch_size=8,
    )
    segments = _collect(segments_iter, info, job_id)

    if not segments:
        # video musik/vokal: VAD tidak menemukan ucapan → jalur klasik tanpa VAD
        jobs.log_line(job_id, "[INFO] VAD tidak menemukan ucapan (video musik?) "
                              "→ ulang tanpa VAD")
        jobs.update(job_id, progress=30.0, message="Menjalankan Whisper (mode klasik)…")
        segments_iter, info = model.transcribe(str(video_path), word_timestamps=True)
        segments = _collect(segments_iter, info, job_id)

    lang = f", bahasa terdeteksi: {info.language} ({info.language_probability:.0%})" \
        if info.language else ""
    jobs.log_line(job_id, f"Transkripsi selesai: {len(segments)} segmen{lang}")
    return segments
