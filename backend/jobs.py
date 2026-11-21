"""Store status job + antrean worker (satu job diproses per waktu)."""
import logging
import threading
import time
import uuid
from collections import deque
from typing import Any, Callable

log = logging.getLogger(__name__)

# Status job dibatasi memori: cukup untuk pemakaian lokal.
_jobs: dict[str, dict] = {}
_queue: deque[str] = deque()
_lock = threading.Lock()
_worker: threading.Thread | None = None


def create_job(params: dict) -> str:
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",           # queued | running | done | failed
        "stage": "antre",             # antre|unduh|transkripsi|analisis|render|selesai
        "stage_label": "Menunggu antrean",
        "progress": 0.0,
        "message": "",
        "logs": [],
        "clips": [],
        "moments": [],
        "video": {},
        "params": params,
        "error": "",
        "created": time.time(),
    }
    with _lock:
        _jobs[job_id] = job
        _queue.append(job_id)
    _ensure_worker()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


def update(job_id: str, **fields: Any) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return
        job.update(fields)


def log_line(job_id: str, text: str) -> None:
    with _lock:
        job = _jobs.get(job_id)
        if job:
            job["logs"].append(text)
            job["logs"] = job["logs"][-200:]  # batasi memori


def run_pipeline_fn(job_id: str, fn: Callable[[str], None]) -> None:
    """Dipanggil worker: eksekusi pipeline, tangkap error apa pun."""
    try:
        update(job_id, status="running")
        fn(job_id)
        if _jobs.get(job_id, {}).get("status") == "running":
            update(job_id, status="done", progress=100.0, stage="selesai", stage_label="Selesai")
    except Exception as e:  # noqa: BLE001 — job tidak boleh bikin worker mati
        log.exception("Job %s gagal", job_id)
        update(job_id, status="failed", error=str(e), stage="gagal", stage_label="Gagal")
        log_line(job_id, f"[ERROR] {e}")


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _worker = threading.Thread(target=_worker_loop, daemon=True, name="clipper-worker")
        _worker.start()


def _worker_loop() -> None:
    while True:
        with _lock:
            if not _queue:
                return  # tidak ada kerja → thread mati, dibuat lagi saat job baru
            job_id = _queue.popleft()
            job = _jobs.get(job_id)
        if job is None:
            continue
        from . import pipeline  # import lambat: hindari siklus import
        run_pipeline_fn(job_id, pipeline.run)


def list_jobs(limit: int = 20) -> list[dict]:
    with _lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        return [dict(j) for j in jobs[:limit]]
