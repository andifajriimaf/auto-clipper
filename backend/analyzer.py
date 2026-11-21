"""Analisis transkrip: pilih momen terbaik untuk klip.

Dua mode:
- "single"  → 1 potongan kontinyu terbaik per klip.
- "summary" → beberapa potongan penting digabung jadi 1 klip (jump cut).

Setiap mode punya jalur LLM (Z.ai GLM Flash — gratis) dan fallback heuristik
lokal (tanpa API key): skor berdasar kepadatan bicara per jendela + sebaran.
"""
import json
import logging
import re

import requests

from . import jobs, settings

log = logging.getLogger(__name__)

PLATFORM_LIMITS = {"reels": 90, "tiktok": 60, "shorts": 60}


def analyze(segments: list[dict], duration: float, params: dict, job_id: str) -> list[dict]:
    """Return list momen.

    Mode "single"  → {start, end, ...} potongan kontinyu.
    Mode "summary" → {chunks: [{start,end}...], start, end, ...} beberapa
                     potongan penting yang digabung jadi 1 klip (jump cut).
    Durasi target mengikuti pilihan user, dibatasi batas platform.
    """
    mode = params.get("clip_mode", "single")
    if mode == "summary":
        return _analyze_summary(segments, duration, params, job_id)
    return _analyze_single(segments, duration, params, job_id)


def _analyze_single(segments: list[dict], duration: float, params: dict, job_id: str) -> list[dict]:
    """1 potongan kontinyu per klip."""
    n = int(params.get("clip_count", 3))
    target = int(params.get("clip_duration", 30))
    max_len = min(target, min(PLATFORM_LIMITS.get(p, 60) for p in params.get("platforms", ["shorts"])))
    max_len = max(15, max_len)
    min_len = max(15, min(20, max_len))

    moments = None
    if settings.get("zai_api_key"):
        try:
            moments = _analyze_llm(segments, duration, n, min_len, max_len, job_id)
        except Exception as e:  # noqa: BLE001 — fallback, jangan gagalkan job
            jobs.log_line(job_id, f"[WARN] Analisis AI gagal ({e}); pakai heuristik lokal")
            moments = None
    else:
        jobs.log_line(job_id, "[INFO] API key kosong → mode heuristik lokal")

    if not moments:
        moments = _analyze_heuristic(segments, duration, n, min_len, max_len)

    # Validasi & rapikan batas waktu ke kata terdekat
    snap = _snap_points(segments)
    result = []
    used: list[tuple[float, float]] = []
    for m in sorted(moments, key=lambda x: -x.get("score", 0)):
        start, end = m["start"], m["end"]
        start = _snap_to(snap, start)
        end = _snap_to(snap, end)
        if end - start < min_len * 0.7:
            end = start + min_len
        end = min(end, start + max_len, duration)
        if start >= end or end - start < 8:
            continue
        if any(not (end <= u0 or start >= u1) for u0, u1 in used):
            continue  # tumpang tindih → buang
        used.append((start, end))
        result.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "title": str(m.get("title") or "Momen menarik")[:80],
            "hook": str(m.get("hook") or "")[:200],
            "score": round(float(m.get("score") or 0.5), 3),
            "source": m.get("source", "heuristik"),
        })
        if len(result) >= n:
            break
    if not result:
        raise RuntimeError("Tidak ada momen valid yang bisa dipilih dari video ini")
    return result


# ---------------------------------------------------------------- Z.ai LLM
_SYSTEM = (
    "Kamu editor video viral profesional untuk Reels/TikTok/Shorts. Tugasmu memilih "
    "potongan paling menarik dari transkrip video. Pilih momen dengan hook kuat, "
    "kalimat punchy, opini berani, cerita emosional, pertanyaan menarik, atau "
    "puncak informasi. HINDARI intro/panjang lebar/outro."
)

_PROMPT = """Transkrip video (format: [mulai-detik] teks). Durasi video: {dur:.0f} detik.

{transcript}

Pilih {n} momen TERBAIK untuk klip vertikal:
- Tiap momen {min_len}-{max_len} detik, boleh dipotong di tengah kalimat jika perlu muat.
- Momen TIDAK boleh tumpang tindih dan sebaiknya tersebar di seluruh video.
- Untuk tiap momen: judul singkat menarik (max 60 karakter, bahasa Indonesia) dan
  "hook" = kalimat pembuka klip / alasan momen ini menarik.
- "score" 0.0-1.0 untuk kekuatan viral.

Balas HANYA JSON valid, tanpa teks lain:
{{"moments": [{{"start": <angka detik>, "end": <angka detik>, "title": "...", "hook": "...", "score": <0.0-1.0>}}]}}"""


def _build_transcript(segments: list[dict], max_chars: int = 28000) -> str:
    lines = []
    used = 0
    for s in segments:
        line = f"[{s['start']:.0f}s] {s['text']}"
        if used + len(line) > max_chars:
            lines.append("…[transkrip dipotong]…")
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)


def _analyze_llm(segments, duration, n, min_len, max_len, job_id) -> list[dict]:
    api_key = settings.get("zai_api_key")
    base_url = settings.get("zai_base_url")
    model = settings.get("zai_model") or "glm-4.5-flash"

    jobs.update(job_id, stage="analisis", stage_label=f"Analisis AI ({model})",
                progress=56.0, message="Mengirim transkrip ke AI…")
    jobs.log_line(job_id, f"Analisis momen via {model}…")

    resp = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _PROMPT.format(
                    dur=duration, transcript=_build_transcript(segments),
                    n=n, min_len=min_len, max_len=max_len)},
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(_extract_json(content))
    moments = data.get("moments") or []
    jobs.log_line(job_id, f"AI mengusulkan {len(moments)} momen")
    out = []
    for m in moments:
        try:
            out.append({
                "start": float(m["start"]), "end": float(m["end"]),
                "title": str(m.get("title", "")), "hook": str(m.get("hook", "")),
                "score": float(m.get("score", 0.5)), "source": "AI",
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _extract_json(text: str) -> str:
    """Ambil objek JSON pertama dari balasan model (toleran markdown fence)."""
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return fence.group(1)
    brace = re.search(r"\{.*\}", text, re.S)
    return brace.group(0) if brace else text


# ------------------------------------------------------------- summary mode
_SUM_PROMPT = """Transkrip video (format: [mulai-detik] teks). Durasi video: {dur:.0f} detik.

{transcript}

Buat {n} RANGKUMAN klip vertikal gaya TikTok (jump cut):
- Tiap rangkuman = gabungan {kmin}-{kmax} potongan (chunks) kalimat TERPENTING dari
  bagian video yang BERBEDA — inti sari video dipadatkan jadi satu klip.
- Tiap chunk {cmin}-{cmax} detik, dipotong di batas kalimat/kata.
- Total durasi tiap rangkuman sekitar {target} detik.
- Chunk di dalam satu rangkuman URUT waktu dan saat dibaca berurutan nyambung.
- Rangkuman satu sama lain tidak boleh tumpang tindih.
- Tiap rangkuman diberi judul singkat (max 60 karakter, bahasa Indonesia), hook,
  dan skor viral 0.0-1.0.

Balas HANYA JSON valid, tanpa teks lain:
{{"summaries": [{{"title": "...", "hook": "...", "score": <0.0-1.0>,
  "chunks": [{{"start": <detik>, "end": <detik>}}]}}]}}"""


def _analyze_summary(segments: list[dict], duration: float, params: dict, job_id: str) -> list[dict]:
    """Mode rangkuman: beberapa chunk penting digabung jadi 1 klip per rangkuman."""
    n = int(params.get("clip_count", 3))
    target = int(params.get("clip_duration", 30))
    target = min(max(target, 15), min(PLATFORM_LIMITS.get(p, 60) for p in params.get("platforms", ["shorts"])))
    kmin, kmax = 3, 7

    results = None
    if settings.get("zai_api_key"):
        try:
            results = _analyze_llm_summary(segments, duration, n, target, kmin, kmax, job_id)
        except Exception as e:  # noqa: BLE001 — fallback ke heuristik
            jobs.log_line(job_id, f"[WARN] Analisis AI gagal ({e}); pakai heuristik lokal")
            results = None
    else:
        jobs.log_line(job_id, "[INFO] API key kosong → mode heuristik lokal")

    if not results:
        results = _analyze_heuristic_summary(segments, duration, n, target, job_id)

    moments = _validate_summaries(results, segments, duration, n, target, job_id)
    if not moments:
        raise RuntimeError("Tidak ada rangkuman valid yang bisa dibuat dari video ini")
    return moments


def _analyze_llm_summary(segments, duration, n, target, kmin, kmax, job_id) -> list[dict]:
    api_key = settings.get("zai_api_key")
    model = settings.get("zai_model") or "glm-4.5-flash"
    jobs.update(job_id, stage="analisis", stage_label=f"Analisis AI ({model})",
                progress=56.0, message="Merangkum transkrip dengan AI…")
    jobs.log_line(job_id, f"AI menyusun {n} rangkuman via {model}…")

    resp = requests.post(
        settings.get("zai_base_url"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": _SUM_PROMPT.format(
                    dur=duration, transcript=_build_transcript(segments),
                    n=n, kmin=kmin, kmax=kmax, cmin=3, cmax=8, target=target)},
            ],
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    data = json.loads(_extract_json(content))
    out = []
    for s in data.get("summaries") or []:
        try:
            chunks = [{"start": float(c["start"]), "end": float(c["end"])}
                      for c in (s.get("chunks") or [])]
            if chunks:
                out.append({"title": str(s.get("title", "")), "hook": str(s.get("hook", "")),
                            "score": float(s.get("score", 0.5)), "chunks": chunks,
                            "source": "AI"})
        except (TypeError, ValueError, KeyError):
            continue
    jobs.log_line(job_id, f"AI mengusulkan {len(out)} rangkuman")
    return out


def _analyze_heuristic_summary(segments, duration, n, target, job_id) -> list[dict]:
    """Heuristik: pilih jendela 5 detik dengan kepadatan bicara tertinggi,
    sebar di video, lalu potong-potong ke dalam n rangkuman.

    Ketat dulu (jarak antar potongan 8s), lalu melonggar kalau kandidat tidak
    cukup — video dengan dialog jarang (mis. gameplay + narasi) tetap dapat
    rangkuman valid.
    """
    jobs.update(job_id, stage="analisis", stage_label="Analisis heuristik",
                progress=56.0, message="Mencari kalimat terpenting…")
    words = [w for s in segments for w in s.get("words") or []]
    win, stride = 5.0, 2.5
    cands = []
    t = 0.0
    while t + win <= duration:
        w_in = [w for w in words if t <= w["start"] < t + win]
        if len(w_in) >= 4:  # jendela terlalu hening → skip
            span = max(1.0, w_in[-1]["end"] - w_in[0]["start"])
            density = len(w_in) / span
            pos_bonus = 0.3 * (1 - t / max(duration, 1))
            cands.append({"start": t, "end": t + win, "score": min(1.0, density / 3.0) + pos_bonus})
        t += stride

    if not cands:
        raise ValueError("Video ini hampir tidak memiliki dialog yang terdeteksi — "
                         "mode rangkuman butuh audio bicara. Coba mode 'Momen tunggal'.")

    chunks_per = max(3, round(target / win))  # ~6 potongan untuk target 30s
    need = n * chunks_per
    ordered = sorted(cands, key=lambda x: -x["score"])
    picked, used = [], []
    # jaga sebaran: mulai ketat, longgarkan bertahap kalau kandidat kurang
    for min_gap in (8.0, 5.0, 3.0, 0.0):
        for c in ordered:
            if len(picked) >= need:
                break
            if any(not (c["end"] <= u0 or c["start"] >= u1) for u0, u1 in used):
                continue
            if any(abs((c["start"] + c["end"]) / 2 - (p["start"] + p["end"]) / 2) < min_gap for p in picked):
                continue
            used.append((c["start"], c["end"]))
            picked.append(c)
        if len(picked) >= need:
            break

    # kelompokkan jadi rangkuman berurutan waktu; tiap grup dijamin ≥2 potongan
    picked.sort(key=lambda x: x["start"])
    k = min(n, max(1, len(picked) // 2))
    base, extra = divmod(len(picked), k)
    groups, idx = [], 0
    for gi in range(k):
        size = base + (1 if gi < extra else 0)
        groups.append(picked[idx:idx + size])
        idx += size

    out = []
    for g in groups:
        if len(g) < 2:
            continue
        score = sum(c["score"] for c in g) / len(g)
        out.append({"title": "", "hook": "", "score": score, "chunks": g, "source": "heuristik"})
    jobs.log_line(job_id, f"Heuristik menyusun {len(out)} rangkuman dari {len(picked)} "
                          f"potongan terbaik (kandidat: {len(cands)})")
    return out


def _validate_summaries(results, segments, duration, n, target, job_id) -> list[dict]:
    """Rapikan chunk: snap ke batas kata, buang overlap & chunk terlalu pendek,
    urutkan waktu, hitung start/end keseluruhan."""
    snap = _snap_points(segments)
    words_flat = [w for s in segments for w in (s.get("words") or [])]
    used_global: list[tuple[float, float]] = []
    out = []

    for m in sorted(results, key=lambda x: -x.get("score", 0)):
        if len(out) >= n:
            break
        chunks = []
        for c in sorted(m.get("chunks", []), key=lambda x: x["start"]):
            s = max(0.0, _snap_to(snap, c["start"], tol=1.0))
            e = min(duration, _snap_to(snap, c["end"], tol=1.0))
            if e - s < 1.5:
                continue
            if any(not (e <= u0 or s >= u1) for u0, u1 in used_global):
                continue
            if chunks and s < chunks[-1]["end"] + 0.5:
                continue  # bertabrakan dengan chunk sebelumnya di rangkuman ini
            chunks.append({"start": round(s, 2), "end": round(e, 2)})
            used_global.append((s, e))
        # rangkuman butuh minimal 2 chunk dan total cukup panjang
        total = sum(c["end"] - c["start"] for c in chunks)
        if len(chunks) < 2 or total < 8:
            continue
        if not m.get("title"):
            first = chunks[0]["start"]
            text = " ".join(w["word"].strip() for w in words_flat if first <= w["start"] < chunks[0]["end"])
            m["title"] = text[:60] or "Rangkuman"
        out.append({
            "start": chunks[0]["start"],
            "end": chunks[-1]["end"],
            "duration": round(total, 1),
            "title": str(m.get("title"))[:80],
            "hook": str(m.get("hook") or "")[:200],
            "score": round(float(m.get("score") or 0.5), 3),
            "source": m.get("source", "heuristik"),
            "chunks": chunks,
        })
    for i, m in enumerate(out, 1):
        jobs.log_line(job_id, f"Rangkuman {i}: {len(m['chunks'])} potongan, "
                              f"total {m['duration']}s — {m['title']}")
    return out


# ------------------------------------------------------------- heuristik
def _analyze_heuristic(segments, duration, n, min_len, max_len) -> list[dict]:
    """Skor jendela geser: kepadatan kata + posisi. Tanpa API, jalan offline."""
    words = [w for s in segments for w in s.get("words") or []]
    if not words:
        # tanpa data kata: pecah durasi rata
        step = min(max_len, max(min_len, duration / n))
        return [{"start": duration * i / n, "end": min(duration, duration * i / n + step),
                 "title": f"Momen #{i + 1}", "hook": "", "score": 0.5, "source": "heuristik"}
                for i in range(n)]

    win = max_len
    stride = max(5.0, win / 4)
    cands = []
    t = 0.0
    while t + win <= duration:
        w_in = [w for w in words if t <= w["start"] < t + win]
        n_words = len(w_in)
        span = max(1.0, (w_in[-1]["end"] - w_in[0]["start"]) if w_in else 1.0)
        density = n_words / span                       # kata per detik
        gap_penalty = max(0.0, (win - span) * 0.02)    # banyak hening → penalti
        pos_bonus = 0.25 * (1 - t / max(duration, 1))  # awal video sedikit lebih diutamakan
        score = min(1.0, density / 3.0) + pos_bonus - gap_penalty
        cands.append({"start": t, "end": t + win, "score": score})
        t += stride

    picked, used = [], []
    for c in sorted(cands, key=lambda x: -x["score"]):
        if any(not (c["end"] <= u0 or c["start"] >= u1) for u0, u1 in used):
            continue
        used.append((c["start"], c["end"]))
        picked.append(c)
        if len(picked) >= n:
            break

    # Judul dari kata-kata di jendela
    out = []
    for i, c in enumerate(picked):
        text = " ".join(w["word"].strip() for w in words if c["start"] <= w["start"] < c["end"])[:60]
        out.append({"start": c["start"], "end": c["end"],
                    "title": text or f"Momen #{i + 1}", "hook": "",
                    "score": c["score"], "source": "heuristik"})
    return out


# ------------------------------------------------------------- util
def _snap_points(segments) -> list[float]:
    pts = [0.0]
    for s in segments:
        pts.append(float(s["start"]))
        pts.append(float(s["end"]))
    return sorted(set(pts))


def _snap_to(points: list[float], t: float, tol: float = 2.0) -> float:
    """Geser t ke batas segmen terdekat (dalam toleransi) agar potongan rapi."""
    best, best_d = t, tol
    for p in points:
        d = abs(p - t)
        if d < best_d:
            best, best_d = p, d
    return max(0.0, best)
