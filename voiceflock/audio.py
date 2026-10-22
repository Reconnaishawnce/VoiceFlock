"""Audio I/O: ffmpeg conversion, URL download, waveform loading, clip cutting,
and the clip-quality score (speech duration, SNR, clipping)."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

import numpy as np

SR = 16000
AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif",
              ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".amr", ".caf"}


class AudioError(RuntimeError):
    pass


_FFMPEG: str | None = None


def ffmpeg_bin() -> str:
    """ffmpeg from $VOICEFLOCK_FFMPEG, PATH, or the usual Windows install spots (winget / chocolatey / C:\\ffmpeg)."""
    global _FFMPEG
    if _FFMPEG and Path(_FFMPEG).exists():
        return _FFMPEG
    cands = [os.environ.get("VOICEFLOCK_FFMPEG"), shutil.which("ffmpeg")]
    if os.name == "nt":
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", ""), "C:\\"):
            if not base:
                continue
            cands += [str(p) for p in Path(base).glob("Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/ffmpeg.exe")]
            cands += [str(p) for p in Path(base).glob("ffmpeg*/bin/ffmpeg.exe")]
        cands += ["C:\\ProgramData\\chocolatey\\bin\\ffmpeg.exe"]
    for c in cands:
        if c and Path(c).exists():
            _FFMPEG = c
            return c
    raise AudioError("ffmpeg not found. Install it (Windows: winget install Gyan.FFmpeg; Mac: brew install ffmpeg; "
                     "Linux: apt install ffmpeg) or set VOICEFLOCK_FFMPEG to the ffmpeg executable.")


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def ffprobe_duration(src: Path) -> float | None:
    """Duration of any media file via ffprobe (next to ffmpeg); None if unavailable."""
    probe = Path(ffmpeg_bin()).with_name("ffprobe" + (".exe" if os.name == "nt" else ""))
    if not probe.exists():
        probe = shutil.which("ffprobe")
        if not probe:
            return None
    r = subprocess.run([str(probe), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def to_wav16k(src: Path, dst: Path, progress: Callable[[float], None] | None = None) -> Path:
    """Convert anything ffmpeg can read (audio or video: the video stream is dropped) to 16 kHz mono PCM wav.
    `progress` receives 0..1 as ffmpeg reports how far into the file it is."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    total = ffprobe_duration(src) if progress else None
    cmd = [ffmpeg_bin(), "-y", "-nostdin", "-loglevel", "error", "-nostats", "-progress", "pipe:1", "-i", str(src),
           "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(SR), "-c:a", "pcm_s16le", str(dst)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        if progress and total and line.startswith("out_time_us="):
            try:
                progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / total))
            except ValueError:
                pass
    err = proc.stderr.read() if proc.stderr else ""
    proc.wait()
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size < 100:
        raise AudioError(f"ffmpeg failed: {err.strip()[:500] or 'no audio stream found'}")
    return dst


def duration_seconds(path: Path) -> float:
    import soundfile as sf
    info = sf.info(str(path))
    return float(info.frames) / float(info.samplerate)


def load_wav(path: Path) -> np.ndarray:
    import soundfile as sf
    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != SR:
        raise AudioError(f"expected {SR} Hz wav, got {sr}")
    return np.ascontiguousarray(data, dtype=np.float32)


def ytdlp_cmd() -> list[str]:
    """yt-dlp from the same Python environment as VoiceFlock (never the system `python`)."""
    exe = Path(sys.executable).with_name("yt-dlp" + (".exe" if os.name == "nt" else ""))
    if exe.exists():
        return [str(exe)]
    try:
        import yt_dlp  # noqa: F401
        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        pass
    found = shutil.which("yt-dlp")
    if found:
        return [found]
    raise AudioError("yt-dlp is not installed in VoiceFlock's environment: run  pip install yt-dlp  "
                     "(Windows: .venv\\Scripts\\pip install yt-dlp) and retry.")


def download_url(url: str, out_dir: Path, progress: Callable[[float], None] | None = None) -> Path:
    """Download the best audio for a URL with yt-dlp (YouTube, podcast pages, direct links)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    tmpl = str(out_dir / "%(title).120B [%(id)s].%(ext)s")
    cmd = ytdlp_cmd() + ["--no-playlist", "-f", "bestaudio/best", "-x", "--audio-format", "m4a", "--newline",
                         "--restrict-filenames", "--ffmpeg-location", str(Path(ffmpeg_bin()).parent), "-o", tmpl, url]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        tail.append(line.rstrip())
        tail = tail[-30:]
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if progress and m:
            progress(float(m.group(1)) / 100.0)
    proc.wait()
    if proc.returncode != 0:
        raise AudioError("yt-dlp failed: " + " | ".join(tail[-6:])[-600:])
    files = sorted(out_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    files = [f for f in files if f.is_file() and not f.name.endswith(".part")]
    if not files:
        raise AudioError("yt-dlp produced no file")
    return files[0]


def write_clip(wav: np.ndarray, start_s: float, end_s: float, dst: Path) -> Path:
    import soundfile as sf
    dst.parent.mkdir(parents=True, exist_ok=True)
    a, b = int(max(0, start_s) * SR), int(min(len(wav) / SR, end_s) * SR)
    seg = wav[a:b]
    peak = float(np.abs(seg).max()) if seg.size else 0.0
    if peak > 0:
        seg = seg * min(1.0, 0.9 / peak)  # normalise quiet speakers for listening
    sf.write(str(dst), seg, SR, subtype="PCM_16")
    return dst


def write_clip_concat(wav: np.ndarray, segments: list[tuple[float, float]], max_seconds: float, dst: Path) -> Path:
    """Concatenate the longest segments (in time order) up to max_seconds into one preview clip."""
    import soundfile as sf
    dst.parent.mkdir(parents=True, exist_ok=True)
    chosen: list[tuple[float, float]] = []
    total = 0.0
    for s, e in sorted(segments, key=lambda x: -(x[1] - x[0])):
        if total >= max_seconds:
            break
        take = min(e - s, max_seconds - total)
        chosen.append((s, s + take))
        total += take
    chosen.sort()
    parts = []
    gap = np.zeros(int(0.15 * SR), dtype=np.float32)
    for s, e in chosen:
        parts.append(wav[int(s * SR):int(e * SR)])
        parts.append(gap)
    seg = np.concatenate(parts) if parts else np.zeros(SR // 10, dtype=np.float32)
    peak = float(np.abs(seg).max()) if seg.size else 0.0
    if peak > 0:
        seg = seg * min(1.0, 0.9 / peak)
    sf.write(str(dst), seg, SR, subtype="PCM_16")
    return dst


# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

def frame_energy(wav: np.ndarray, frame_ms: float = 25.0, hop_ms: float = 10.0) -> tuple[np.ndarray, float]:
    """RMS energy per frame (dB) and hop in seconds."""
    fl, hop = int(SR * frame_ms / 1000), int(SR * hop_ms / 1000)
    if len(wav) < fl:
        wav = np.pad(wav, (0, fl - len(wav)))
    n = 1 + (len(wav) - fl) // hop
    idx = np.arange(fl)[None, :] + hop * np.arange(n)[:, None]
    frames = wav[idx]
    rms = np.sqrt((frames ** 2).mean(axis=1) + 1e-12)
    return 20 * np.log10(rms + 1e-9), hop / SR


def estimate_snr_db(energy_db: np.ndarray, hop_s: float, segments: list[tuple[float, float]],
                    all_speech: list[tuple[float, float]] | None = None) -> float:
    """SNR = this speaker's speech-frame energy vs the recording's noise floor.

    Noise floor: median energy of frames outside *all* speech (when >= 0.5 s of such frames exist),
    else the 5th percentile of all frames."""
    if energy_db.size == 0:
        return 0.0

    def mask_of(segs):
        m = np.zeros(energy_db.shape[0], dtype=bool)
        for s, e in segs:
            m[int(s / hop_s):int(e / hop_s)] = True
        return m

    mask = mask_of(segments)
    if mask.sum() < 3:
        return 0.0
    nonspeech = ~mask_of(all_speech) if all_speech is not None else ~mask
    if nonspeech.sum() * hop_s >= 0.5:
        noise = float(np.median(energy_db[nonspeech]))
    else:
        noise = float(np.percentile(energy_db, 5))
    # the louder half of the speaker's frames tracks the voice better than pauses between words
    sp = float(np.percentile(energy_db[mask], 60))
    return float(np.clip(sp - noise, -5.0, 50.0))


def clipping_fraction(wav: np.ndarray, segments: list[tuple[float, float]], thresh: float = 0.985) -> float:
    n = 0
    c = 0
    for s, e in segments:
        seg = wav[int(s * SR):int(e * SR)]
        n += seg.size
        c += int((np.abs(seg) >= thresh).sum())
    return c / n if n else 0.0


def quality_score(speech_seconds: float, snr_db: float, clip_frac: float) -> float:
    """0..100 composite: duration (saturates at 30 s), SNR (5..25 dB), clipping penalty."""
    dur = min(1.0, speech_seconds / 30.0) ** 0.5
    snr = float(np.clip((snr_db - 5.0) / 20.0, 0.0, 1.0))
    clip = float(np.clip(1.0 - clip_frac * 20.0, 0.0, 1.0))
    return round(100.0 * (0.45 * dur + 0.45 * snr + 0.10 * clip), 1)
