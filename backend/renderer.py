"""Render klip: potong + layout crop/blur + burn subtitle (.ass) via ffmpeg.

Mendukung dua bentuk moment:
- potongan kontinyu {start, end}
- rangkuman {chunks: [{start, end}, ...]} — beberapa potongan digabung dengan
  jump cut (tiap potongan di-burn subtitle-nya, lalu concat tanpa re-encode).

Encoder otomatis: Intel Quick Sync (h264_qsv) dipakai kalau tersedia — 2-4×
lebih cepat dari libx264 CPU — dengan fallback otomatis ke libx264.
Blur layout memakai trik downscale-blur-upscale (blur di 1/16 piksel) yang
jauh lebih murah daripada blur full-res tanpa beda visual berarti.
"""
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2

from . import jobs, settings

log = logging.getLogger(__name__)

_WIN_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

_hw_encoder: dict | None = None  # cache hasil deteksi (None = belum dites)


def _detect_hw_encoder() -> dict:
    """Deteksi encoder sesuai setting render_encoder.

    - "cpu"  → selalu libx264 (tidak menyentuh GPU — paling stabil)
    - "auto" → QSV kalau tersedia, fallback libx264
    - "qsv"  → paksa QSV (untuk mesin yang tidak bermasalah)

    Tes QSV berupa encode 0.5 detik nyata — bukan sekadar cek daftar encoder —
    supaya driver bermasalah pun tertangkap. low_power dipakai kalau didukung:
    encode berpindah ke blok hardware khusus (VDENC) sehingga tidak berebut
    unit grafis dengan tampilan layar.
    """
    global _hw_encoder
    if _hw_encoder is not None:
        return _hw_encoder

    pref = (settings.get("render_encoder") or "cpu").strip().lower()
    if pref == "cpu":
        _hw_encoder = {"type": "x264"}
        return _hw_encoder

    def _try(extra: list[str]) -> bool:
        test = [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=black:s=320x240:d=0.5",
            "-c:v", "h264_qsv", *extra, "-f", "mp4", os.devnull,
        ]
        try:
            p = subprocess.run(
                test, capture_output=True, text=True,
                creationflags=_WIN_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            return p.returncode == 0
        except Exception:  # noqa: BLE001
            return False

    if _try(["-low_power", "1"]):
        _hw_encoder = {"type": "qsv", "low_power": True}
    elif _try([]):
        _hw_encoder = {"type": "qsv", "low_power": False}
    else:
        _hw_encoder = {"type": "x264"}
    return _hw_encoder


def _video_encoder_args(crf: int, preset: str, threads: int | None) -> list[str]:
    """Args encoder video sesuai hasil deteksi hardware."""
    enc = _detect_hw_encoder()
    if enc["type"] == "qsv":
        # Quick Sync: global_quality ≈ crf; preset qsv medium = keseimbangan
        args = ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(crf)]
        if enc.get("low_power"):
            args += ["-low_power", "1"]
        if threads:
            args += ["-threads", str(threads)]
        return args
    args = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]
    if threads:
        args += ["-threads", str(threads)]
    return args


def _encoder_label() -> str:
    enc = _detect_hw_encoder()
    if enc["type"] != "qsv":
        return "libx264 (CPU)"
    return "Intel Quick Sync (hardware, low-power)" if enc.get("low_power") \
        else "Intel Quick Sync (hardware)"


def _run(args: list[str], cwd: Path | None = None) -> None:
    p = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=_WIN_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    if p.returncode != 0:
        tail = (p.stderr or p.stdout or "")[-1500:]
        raise RuntimeError(f"ffmpeg gagal: {tail}")


def probe_duration(path: Path) -> float:
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
        creationflags=_WIN_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    try:
        return float(p.stdout.strip())
    except ValueError:
        return 0.0


def probe_dimensions(path: Path) -> tuple[int, int]:
    """(lebar, tinggi) video sumber."""
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
        creationflags=_WIN_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    try:
        w, h = p.stdout.strip().split(",")
        return int(w), int(h)
    except ValueError:
        return 0, 0


# ---------------------------------------------------------- face tracking
_FACE_CASCADE = None


def _face_cascade():
    """Haar cascade bawaan OpenCV — tanpa download model tambahan."""
    global _FACE_CASCADE
    if _FACE_CASCADE is None:
        _FACE_CASCADE = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    return _FACE_CASCADE


def _detect_face_keyframes(video: Path, start: float, end: float, work: Path,
                           job_id: str) -> list[tuple[float, float]] | None:
    """Deteksi posisi wajah sepanjang [start,end]: sample 2 frame/detik pada
    480px, ambil wajah TERBESAR (pembicara terdekat) di tiap frame.

    Return list (t_rel, cx_norm) — posisi horizontal tengah wajah 0..1 —
    atau None kalau wajah tidak cukup terdeteksi (fallback ke crop tengah).
    """
    fps = 2
    work.mkdir(parents=True, exist_ok=True)
    try:
        _run([
            "ffmpeg", "-y", "-v", "error",
            "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", str(video),
            "-vf", f"fps={fps},scale=480:-2", "-q:v", "5",
            str(work / "f_%03d.jpg"),
        ], cwd=work)
    except Exception as e:  # noqa: BLE001 — fallback tengah kalau gagal
        jobs.log_line(job_id, f"[WARN] Ekstraksi frame wajah gagal: {e}")
        return None

    cascade = _face_cascade()
    samples: list[tuple[float, float]] = []
    for i, fp in enumerate(sorted(work.glob("f_*.jpg"))):
        img = cv2.imread(str(fp))
        if img is None:
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = cascade.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5,
                                         minSize=(int(img.shape[1] * 0.08),) * 2)
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2] * f[3])  # wajah terbesar
            samples.append((i / fps, (x + w / 2) / img.shape[1]))
    for fp in work.glob("f_*.jpg"):
        fp.unlink(missing_ok=True)

    if len(samples) < max(3, (end - start) * 0.25):  # wajah jarang muncul → center
        return None
    return samples


def _build_face_crop_expr(samples: list[tuple[float, float]], dur: float,
                          width: int, height: int, src_w: int, src_h: int) -> str:
    """Bangun ekspresi x untuk filter crop yang mengikuti wajah.

    Alur: posisi wajah (0..1) → koordinat frame setelah scale-to-cover →
    posisi crop window → smoothing (rata bergerak) → interpolar linear
    per ~1 detik sebagai ekspresi nested-if ffmpeg (evaluasi per-frame).
    """
    factor = max(width / src_w, height / src_h)
    scaled_w = src_w * factor
    max_x = max(0.0, scaled_w - width)

    def to_x(cx_norm: float) -> float:
        return max(0.0, min(cx_norm * scaled_w - width / 2, max_x))

    # smoothing: rata bergerak 3 titik
    xs = [s[1] for s in samples]
    sm = []
    for i, s in enumerate(samples):
        lo, hi = max(0, i - 1), min(len(xs), i + 2)
        sm.append((s[0], sum(xs[lo:hi]) / (hi - lo)))

    # keyframes ~1 detik: interpolasi linear antar sample
    keys: list[tuple[float, float]] = []
    t_cursor = 0.0
    si = 0
    while t_cursor < dur and si < len(sm):
        t_next = min(dur, t_cursor + 1.0)
        while si + 1 < len(sm) and sm[si + 1][0] <= t_cursor:
            si += 1
        # cari nilai pada t_next (interpolasi antar sample di sekitarnya)
        a_i = max(0, min(si, len(sm) - 1))
        if a_i + 1 < len(sm) and sm[a_i + 1][0] > sm[a_i][0]:
            ratio = (t_next - sm[a_i][0]) / (sm[a_i + 1][0] - sm[a_i][0])
            cx = sm[a_i][1] + (sm[a_i + 1][1] - sm[a_i][1]) * min(1.0, max(0.0, ratio))
        else:
            cx = sm[a_i][1]
        keys.append((t_cursor, to_x(cx)))
        t_cursor = t_next
    if not keys:
        return ""
    keys.append((dur, keys[-1][1]))  # jaga posisi terakhir sampai habis

    # ekspresi nested-if dengan interpolasi linear antar keyframe
    expr = str(keys[-1][1])
    for i in range(len(keys) - 2, -1, -1):
        t0, x0 = keys[i]
        t1, x1 = keys[i + 1]
        seg = f"{x0}+({x1}-{x0})*(t-{t0:.2f})/{max(0.01, t1 - t0):.2f}"
        expr = f"if(lte(t,{t0:.2f}),{seg},{expr})"
    return f"max(0,min({expr},{max_x:.0f}))"



# ------------------------------------------------------------------ ASS
def generate_ass(words: list[dict], clip_start: float, clip_end: float,
                 out_path: Path, style: str, width: int, height: int) -> bool:
    """Buat file .ass dari kata-kata dalam jendela klip.

    style: "klasik"  → kalimat putih bold outline hitam
           "karaoke" → highlight kata aktif kuning ala TikTok (tag \\k)
           "box"     → teks dalam kotak solid
           "glow"    → teks dengan glow kuning lembut
           "word"    → satu kata besar per layar (TikTok style)
    Return True kalau ada subtitle, False kalau jendela tanpa kata.
    """
    in_window = [w for w in words if w["start"] < clip_end and w["end"] > clip_start]
    if not in_window:
        return False

    fs = int(height * 0.045)          # ukuran font proporsional tinggi video
    if style == "word":
        fs = int(height * 0.075)      # satu kata per layar → jauh lebih besar
    margin_v = int(height * 0.16)     # aman dari UI TikTok/Reels di bawah

    dispatch = {
        "klasik":  (_ass_header_klasik, _events_klasik),
        "karaoke": (_ass_header_karaoke, _events_karaoke),
        "box":     (_ass_header_box, _events_box),
        "glow":    (_ass_header_glow, _events_glow),
        "word":    (_ass_header_word, _events_word),
    }
    header_fn, events_fn = dispatch.get(style, (_ass_header_klasik, _events_klasik))
    header = header_fn(width, height, fs)
    events = events_fn(in_window, clip_start, clip_end, margin_v)

    out_path.write_text(header + "\n[Events]\n" + "\n".join(events), encoding="utf-8")
    return True


def _ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_header_klasik(w: int, h: int, fs: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,Arial Black,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{int(fs * 0.13)},0,2,60,60,{int(h * 0.16)},1
"""


def _ass_header_karaoke(w: int, h: int, fs: int) -> str:
    # Primary = kuning (kata yang sudah/sedang dinyanyikan → highlight),
    # Secondary = putih (kata yang belum). Hasil akhirnya ala karaoke TikTok:
    # teks putih lalu menyala kuning kata demi kata.
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,Arial Black,{fs},&H0000FFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{int(fs * 0.13)},0,2,60,60,{int(h * 0.16)},1
"""


def _group_lines(words: list[dict], max_chars: int = 26, max_dur: float = 3.2) -> list[list[dict]]:
    """Kelompokkan kata jadi baris subtitle pendek (mudah dibaca di HP)."""
    lines, cur = [], []
    for w in words:
        text = w["word"].strip()
        cur_dur = cur[-1]["end"] - cur[0]["start"] if cur else 0
        cur_chars = sum(len(x["word"].strip()) for x in cur)
        if cur and (cur_chars + len(text) > max_chars or cur_dur > max_dur):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def _events_klasik(words, clip_start, clip_end, margin_v) -> list[str]:
    events = []
    for line in _group_lines(words):
        start = max(0.0, line[0]["start"] - clip_start)
        end = min(clip_end - clip_start, line[-1]["end"] - clip_start)
        if end <= start:
            continue
        text = " ".join(w["word"].strip() for w in line).upper()
        text = text.replace("{", "").replace("}", "")
        events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Pop,,0,0,0,,{text}")
    return events


def _ass_header_box(w: int, h: int, fs: int) -> str:
    # BorderStyle=3 = kotak solid di belakang teks (warna = OutlineColour)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Box,Arial Black,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,{int(fs * 0.25)},0,2,60,60,{int(h * 0.16)},1
"""


def _events_box(words, clip_start, clip_end, margin_v) -> list[str]:
    return _events_klasik(words, clip_start, clip_end, margin_v)


def _ass_header_glow(w: int, h: int, fs: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Glow,Arial Black,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,2,60,60,{int(h * 0.16)},1
"""


def _events_glow(words, clip_start, clip_end, margin_v) -> list[str]:
    """Dua layer per baris: layer 0 = glow kuning blur, layer 1 = teks tajam."""
    events = []
    for line in _group_lines(words):
        start = max(0.0, line[0]["start"] - clip_start)
        end = min(clip_end - clip_start, line[-1]["end"] - clip_start)
        if end <= start:
            continue
        text = " ".join(w["word"].strip() for w in line).upper()
        text = text.replace("{", "").replace("}", "")
        # layer 0: glow kuning besar + blur (di belakang)
        events.append(
            f"Dialogue: 0,{_ts(start)},{_ts(end)},Glow,,0,0,0,,"
            f"{{\\blur14\\1c&H0000E1FF&\\alpha&H30&}}{text}")
        # layer 1: teks tajam putih outline hitam (di atas glow)
        events.append(f"Dialogue: 1,{_ts(start)},{_ts(end)},Glow,,0,0,0,,{text}")
    return events


def _ass_header_word(w: int, h: int, fs: int) -> str:
    # Alignment 5 = tengah-tengah layar; kata besar ala TikTok
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,Arial Black,{fs},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{int(fs * 0.14)},0,5,40,40,0,1
"""


def _events_word(words, clip_start, clip_end, margin_v) -> list[str]:
    """Satu kata besar per layar — tampil sampai kata berikutnya mulai."""
    words = sorted(words, key=lambda w: w["start"])
    events = []
    for i, w in enumerate(words):
        start = max(0.0, w["start"] - clip_start)
        next_start = words[i + 1]["start"] - clip_start if i + 1 < len(words) else None
        end = min(clip_end - clip_start,
                  next_start if next_start is not None else w["end"] - clip_start + 0.4)
        if end <= start:
            end = start + 0.25
        if end <= start:
            continue
        text = w["word"].strip().upper().replace("{", "").replace("}", "")
        if not text:
            continue
        events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Word,,0,0,0,,{text}")
    return events


def _events_karaoke(words, clip_start, clip_end, margin_v) -> list[str]:
    """ASS \\k karaoke: SecondaryColour (putih, belum dinyanyikan) → PrimaryColour (kuning)."""
    events = []
    for line in _group_lines(words):
        start = max(0.0, line[0]["start"] - clip_start)
        end = min(clip_end - clip_start, line[-1]["end"] - clip_start)
        if end <= start:
            continue
        parts = []
        prev = line[0]["start"] - clip_start
        for w in line:
            gap = max(0.0, w["start"] - clip_start - prev)
            if gap > 0.01:
                parts.append(f"{{\\k{int(gap * 100)}}} ")  # jeda hening sebelum kata ini
            dur = max(1, int((w["end"] - max(w["start"], clip_start)) * 100))
            word = w["word"].strip().upper().replace("{", "").replace("}", "")
            # spasi mengikuti kata yang sudah dinyanyikan (warna putih)
            parts.append(f"{{\\k{dur}}}{word} ")
            prev = w["end"] - clip_start
        text = "".join(parts).rstrip()
        events.append(f"Dialogue: 0,{_ts(start)},{_ts(end)},Karaoke,,0,0,0,,{text}")
    return events


# ------------------------------------------------------------------ render
def render_clip(video: Path, moment: dict, out_path: Path, tmp_dir: Path,
                width: int, height: int, subtitle: dict | None, job_id: str,
                crf: int = 20, preset: str = "veryfast", layout: str = "crop",
                threads: int | None = None) -> None:
    """Render klip dari moment.

    Tanpa "chunks"  → potongan kontinyu [start,end].
    Dengan "chunks" → beberapa potongan digabung (mode rangkuman): tiap chunk
    dirender terpisah (subtitle di-burn per chunk dengan waktu lokal), lalu
    disambung dengan concat demuxer tanpa re-encode.

    threads: batas thread ffmpeg per proses — dipakai saat beberapa ffmpeg
    berjalan paralel agar tidak berebut seluruh core.
    """
    chunks = moment.get("chunks") or []
    # folder kerja unik per panggilan: render berjalan paralel, jangan biarkan
    # subs.ass / part_XX.mp4 / concat.txt saling menimpa antar thread
    work = Path(tempfile.mkdtemp(prefix="rw_", dir=tmp_dir))
    try:
        if len(chunks) > 1:
            # mode rangkuman: render tiap potongan → concat tanpa re-encode
            parts = []
            for k, ch in enumerate(chunks):
                part_moment = {"start": ch["start"], "end": ch["end"], "title": f"chunk-{k}"}
                part_path = work / f"part_{k:02d}.mp4"
                _render_segment(video, part_moment, part_path, work,
                                width, height, subtitle, job_id, crf, preset, layout, threads)
                parts.append(part_path)
            list_file = work / "concat.txt"
            list_file.write_text("\n".join(f"file '{p.name}'" for p in parts), encoding="ascii")
            _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "concat.txt",
                  "-c", "copy", "-movflags", "+faststart", str(out_path)], cwd=work)
        else:
            _render_segment(video, moment, out_path, work, width, height, subtitle,
                            job_id, crf, preset, layout, threads)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _render_segment(video: Path, moment: dict, out_path: Path, tmp_dir: Path,
                    width: int, height: int, subtitle: dict | None, job_id: str,
                    crf: int, preset: str, layout: str, threads: int | None = None) -> None:
    """Render SATU potongan kontinyu: layout crop/blur → (burn subs) → mp4."""
    start, end = float(moment["start"]), float(moment["end"])
    dur = end - start

    ass_file = None
    if subtitle and subtitle.get("words"):
        ass_file = tmp_dir / "subs.ass"
        ok = generate_ass(subtitle["words"], start, end, ass_file,
                          subtitle.get("style", "klasik"), width, height)
        if not ok:
            ass_file = None
            jobs.log_line(job_id, f"[WARN] '{str(moment.get('title', ''))[:40]}': "
                                  f"jendela tanpa transkrip → tanpa subtitle")

    codec = [
        *_video_encoder_args(crf, preset, threads),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2",
        "-movflags", "+faststart",
        "-r", "30",
    ]

    if layout == "crop_face":
        # crop dinamis mengikuti wajah pembicara (fallback tengah kalau
        # wajah tidak cukup terdeteksi)
        src_w, src_h = probe_dimensions(video)
        keys = _detect_face_keyframes(video, start, end, tmp_dir, job_id) if src_w else None
        if keys is None:
            jobs.log_line(job_id, f"[INFO] '{str(moment.get('title', ''))[:40]}': "
                                  f"wajah tidak terdeteksi stabil → crop tengah")
            layout = "crop"
        else:
            expr = _build_face_crop_expr(keys, dur, width, height, src_w, src_h)
            if not expr:
                layout = "crop"
            else:
                vf = (
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}:x='{expr}':y=0,setsar=1"
                )
                if ass_file:
                    vf += f",subtitles=filename={ass_file.name}"
                args = [
                    "ffmpeg", "-y",
                    "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
                    "-vf", vf,
                    *codec, str(out_path),
                ]
                _run(args, cwd=tmp_dir)
                return

    if layout == "blur":
        # gambar utuh di tengah + sisi diisi versi blur dari video itu sendiri.
        # trik cepat: blur di 1/4 resolusi lalu upscale — hasil visual hampir
        # identik, biaya blur 1/16 piksel.
        bw = max(2, (width // 4) // 2 * 2)
        bh = max(2, (height // 4) // 2 * 2)
        fc = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={bw}:{bh}:force_original_aspect_ratio=increase,"
            f"crop={bw}:{bh},boxblur=8[bgb];"
            f"[bgb]scale={width}:{height}[bgb2];"
            f"[fg]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs];"
            f"[bgb2][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
        )
        if ass_file:
            fc += f",subtitles=filename={ass_file.name}"
        fc += "[v]"
        args = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
            "-filter_complex", fc,
            "-map", "[v]", "-map", "0:a:0",
            *codec, str(out_path),
        ]
    else:
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},setsar=1"
        )
        if ass_file:
            vf += f",subtitles=filename={ass_file.name}"
        args = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(video),
            "-vf", vf,
            *codec, str(out_path),
        ]
    _run(args, cwd=tmp_dir)


def extract_thumb(video: Path, at: float, out_jpg: Path) -> None:
    _run([
        "ffmpeg", "-y", "-ss", f"{max(0.5, at):.2f}", "-i", str(video),
        "-frames:v", "1", "-q:v", "4", str(out_jpg),
    ])
