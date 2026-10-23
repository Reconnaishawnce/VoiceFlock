"""Low-res video stills per speaker, so a voice can be identified by looking at who is on screen.

Uploaded video: frames cut with ffmpeg at the start of the speaker's longest turns.
YouTube: the storyboard sprite sheets YouTube serves for the seek-bar preview (exposed by yt-dlp as
formats sb0/sb1/sb2) are downloaded and the tile covering the timestamp is cropped out. No video download.
Each still is ~10-20 KB."""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import urllib.request
from pathlib import Path

from . import audio, config, db

log = logging.getLogger(__name__)
STILL_BYTES_ESTIMATE = 15_000


def still_dir() -> Path:
    d = config.DATA_DIR / "stills"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ uploaded video
def extract_frame(video: Path, t: float, out: Path, width: int = 320) -> bool:
    cmd = [audio.ffmpeg_bin(), "-y", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(video),
           "-frames:v", "1", "-vf", f"scale={int(width)}:-2", "-q:v", "6", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 500


# ------------------------------------------------------------------ youtube storyboards
def youtube_storyboard(url: str) -> dict | None:
    """Pick the best storyboard format: {'fragments':[{'url','duration'}], 'rows','columns','fps','width','height'}."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    sbs = [f for f in (info or {}).get("formats", []) if str(f.get("format_id", "")).startswith("sb") and f.get("fragments")]
    if not sbs:
        return None
    sbs.sort(key=lambda f: (f.get("width") or 0) * (f.get("height") or 0), reverse=True)
    return sbs[0]


def storyboard_tile(fmt: dict, t: float) -> tuple[int, int, int, int, int] | None:
    """(fragment index, column, row, tile_w, tile_h) for time t within a storyboard format."""
    frags = fmt["fragments"]
    rows, cols = int(fmt.get("rows") or 1), int(fmt.get("columns") or 1)
    per_frag = rows * cols
    # yt-dlp reports fps = tiles per second for the storyboard; fall back to fragment duration
    fps = fmt.get("fps")
    frag_dur = frags[0].get("duration") or (per_frag / fps if fps else None)
    if not frag_dur:
        return None
    fi = int(t // frag_dur)
    if fi >= len(frags):
        fi = len(frags) - 1
    within = t - fi * frag_dur
    tile = min(per_frag - 1, int(within / frag_dur * per_frag))
    tw = int((fmt.get("width") or 0) / cols) if fmt.get("width") else None
    th = int((fmt.get("height") or 0) / rows) if fmt.get("height") else None
    return fi, tile % cols, tile // cols, tw or 0, th or 0


def storyboard_frame(fmt: dict, t: float, out: Path, cache: dict[int, Path], tmp_dir: Path) -> bool:
    tile = storyboard_tile(fmt, t)
    if tile is None:
        return False
    fi, col, row, tw, th = tile
    sheet = cache.get(fi)
    if sheet is None:
        sheet = tmp_dir / f"sb_{fi}.jpg"
        req = urllib.request.Request(fmt["fragments"][fi]["url"], headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            sheet.write_bytes(r.read())
        cache[fi] = sheet
    rows, cols = int(fmt.get("rows") or 1), int(fmt.get("columns") or 1)
    # crop by fraction so we don't depend on reported sheet size being exact
    vf = f"crop=iw/{cols}:ih/{rows}:{col}*iw/{cols}:{row}*ih/{rows}"
    r = subprocess.run([audio.ffmpeg_bin(), "-y", "-nostdin", "-loglevel", "error", "-i", str(sheet), "-vf", vf, "-q:v", "5", str(out)],
                       capture_output=True, text=True)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 300


# ------------------------------------------------------------------ per source
def times_for_speaker(conn: sqlite3.Connection, speaker_id: int, n: int) -> list[float]:
    segs = db.rows(conn, "SELECT start_s, end_s FROM segments WHERE source_speaker_id=? ORDER BY (end_s-start_s) DESC LIMIT ?", (speaker_id, n))
    return [min(s["start_s"] + 1.0, (s["start_s"] + s["end_s"]) / 2) for s in segs]


def make_stills_for_source(conn: sqlite3.Connection, source_id: int, progress=None) -> dict:
    """Create stills for every speaker of a processed source. Returns {'speakers': n, 'stills': m, 'kind': ...}."""
    import tempfile
    settings = config.Settings(conn)
    n_per = int(settings["stills_per_speaker"])
    width = int(settings["still_width"])
    src = db.row(conn, "SELECT * FROM sources WHERE id=?", (source_id,))
    if not src or n_per <= 0:
        return {"speakers": 0, "stills": 0, "kind": "disabled"}
    orig = Path(src["orig_path"]) if src["orig_path"] else None
    kind = None
    fmt = None
    if orig and orig.exists() and orig.suffix.lower() in {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".mpg", ".mpeg", ".ts"}:
        kind = "video"
    elif src["url"] and ("youtube.com" in src["url"] or "youtu.be" in src["url"]):
        try:
            fmt = youtube_storyboard(src["url"])
            kind = "youtube" if fmt else None
        except Exception as e:  # noqa: BLE001
            log.warning("storyboard lookup failed for %s: %s", src["url"], e)
    if not kind:
        return {"speakers": 0, "stills": 0, "kind": "none"}
    speakers = db.rows(conn, "SELECT id FROM source_speakers WHERE source_id=? ORDER BY speech_seconds DESC", (source_id,))
    out_dir = still_dir()
    made = 0
    with tempfile.TemporaryDirectory() as td:
        cache: dict[int, Path] = {}
        for i, sp in enumerate(speakers):
            if progress:
                progress("stills", i / max(1, len(speakers)))
            stills = []
            for j, t in enumerate(times_for_speaker(conn, sp["id"], n_per)):
                out = out_dir / f"{source_id}_{sp['id']}_{j}.jpg"
                try:
                    ok = extract_frame(orig, t, out, width) if kind == "video" else storyboard_frame(fmt, t, out, cache, Path(td))
                except Exception as e:  # noqa: BLE001
                    log.warning("still failed (%s @ %.1f): %s", source_id, t, e)
                    ok = False
                if ok:
                    stills.append({"t": round(t, 1), "file": out.name})
                    made += 1
            conn.execute("UPDATE source_speakers SET stills=? WHERE id=?", (json.dumps(stills), sp["id"]))
    return {"speakers": len(speakers), "stills": made, "kind": kind}


def delete_stills(source_id: int) -> None:
    for p in still_dir().glob(f"{source_id}_*.jpg"):
        p.unlink(missing_ok=True)
