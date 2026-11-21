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

from . import jobs

log = logging.getLogger(__name__)

_WIN_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW

_hw_encoder: dict | None = None  # cache hasil deteksi (None = belum dites)


def _detect_hw_encoder() -> dict:
    """Deteksi encoder hardware sekali per proses: qsv → fallback libx264.

    Tes berupa encode 0.5 detik nyata — bukan sekadar cek daftar encoder —
    supaya driver bermasalah pun tertangkap. low_power dipakai kalau didukung:
    encode berpindah ke blok hardware khusus (VDENC) sehingga tidak berebut
    unit grafis dengan tampilan layar (mencegah layar kedap-kedip).
    """
    global _hw_encoder
    if _hw_encoder is not None:
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


# ------------------------------------------------------------------ ASS
def generate_ass(words: list[dict], clip_start: float, clip_end: float,
                 out_path: Path, style: str, width: int, height: int) -> bool:
    """Buat file .ass dari kata-kata dalam jendela klip.

    style: "klasik"  → 1 baris kalimat, putih bold outline hitam
           "karaoke" → highlight kata aktif kuning ala TikTok (tag \\k)
    Return True kalau ada subtitle, False kalau jendela tanpa kata.
    """
    in_window = [w for w in words if w["start"] < clip_end and w["end"] > clip_start]
    if not in_window:
        return False

    fs = int(height * 0.045)          # ukuran font proporsional tinggi video
    margin_v = int(height * 0.16)     # aman dari UI TikTok/Reels di bawah
    if style == "karaoke":
        header = _ass_header_karaoke(width, height, fs)
        events = _events_karaoke(in_window, clip_start, clip_end, margin_v)
    else:
        header = _ass_header_klasik(width, height, fs)
        events = _events_klasik(in_window, clip_start, clip_end, margin_v)

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
