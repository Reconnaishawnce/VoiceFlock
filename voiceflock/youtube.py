"""YouTube / playlist bulk import: list a channel's videos without downloading, filter, queue."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from . import audio, db, worker


@dataclass
class Video:
    id: str
    url: str
    title: str
    duration_s: float | None
    date: str | None          # YYYY-MM-DD when the listing provides it
    view_count: int | None
    already_imported: int | None = None   # source id when this URL is already in the library


def normalise_channel_url(url: str) -> str:
    """Channel home pages list tabs; point at /videos so entries are videos."""
    u = url.strip()
    if re.search(r"youtube\.com/(@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)/?$", u):
        u = u.rstrip("/") + "/videos"
    return u


def _entry_to_video(e: dict) -> Video | None:
    vid = e.get("id") or ""
    url = e.get("url") or e.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None)
    if not url:
        return None
    if url.startswith("http") and "youtube" in url and "watch?v=" not in url and re.fullmatch(r"[\w-]{11}", vid or ""):
        url = f"https://www.youtube.com/watch?v={vid}"
    date = None
    if e.get("upload_date"):
        d = str(e["upload_date"])
        date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
    elif e.get("timestamp") or e.get("release_timestamp"):
        date = datetime.utcfromtimestamp(int(e.get("timestamp") or e.get("release_timestamp"))).strftime("%Y-%m-%d")
    return Video(vid, url, (e.get("title") or url).strip(), e.get("duration"), date, e.get("view_count"))


def list_videos(url: str, limit: int = 500) -> tuple[str | None, list[Video]]:
    """Return (channel/playlist title, videos) using yt-dlp's flat extraction (fast, no downloads)."""
    import yt_dlp
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True,
            "playlistend": int(limit), "ignoreerrors": True, "ffmpeg_location": str(__import__("pathlib").Path(audio.ffmpeg_bin()).parent)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(normalise_channel_url(url), download=False)
    if not info:
        raise audio.AudioError("yt-dlp returned nothing for that URL")
    entries = info.get("entries")
    if entries is None:  # a single video URL
        v = _entry_to_video(info)
        return info.get("title"), [v] if v else []
    videos = []
    for e in entries:
        if not e:
            continue
        if e.get("_type") == "playlist" and e.get("entries"):  # channel root -> tabs
            for e2 in e["entries"]:
                v = _entry_to_video(e2) if e2 else None
                if v:
                    videos.append(v)
            continue
        v = _entry_to_video(e)
        if v:
            videos.append(v)
    return info.get("title"), videos[: int(limit)]


def filter_videos(videos: list[Video], title_regex: str | None = None, min_minutes: float | None = None,
                  max_minutes: float | None = None, date_from: str | None = None, date_to: str | None = None,
                  max_count: int | None = None) -> list[Video]:
    out = []
    rx = re.compile(title_regex, re.I) if title_regex else None
    for v in videos:
        if rx and not rx.search(v.title):
            continue
        if min_minutes is not None and v.duration_s is not None and v.duration_s < min_minutes * 60:
            continue
        if max_minutes is not None and v.duration_s is not None and v.duration_s > max_minutes * 60:
            continue
        if date_from and v.date and v.date < date_from:
            continue
        if date_to and v.date and v.date > date_to:
            continue
        out.append(v)
    return out[: max_count] if max_count else out


def mark_imported(conn: sqlite3.Connection, videos: list[Video]) -> None:
    for v in videos:
        r = conn.execute("SELECT id FROM sources WHERE url=? AND status!='failed'", (v.url,)).fetchone()
        v.already_imported = int(r[0]) if r else None


def import_videos(conn: sqlite3.Connection, videos: list[Video], mode: str = "bulk", incognito: bool = False,
                  prefix: str | None = None) -> dict:
    from .sources import new_batch
    created, skipped = [], 0
    with db.tx(conn):
        batch_id = new_batch(conn, f"YouTube: {prefix or 'import'} ({len(videos)} videos)", "youtube") if len(videos) > 1 else None
        for v in videos:
            if conn.execute("SELECT 1 FROM sources WHERE url=? AND status!='failed'", (v.url,)).fetchone():
                skipped += 1
                continue
            name = f"{prefix} – {v.title}" if prefix else v.title
            sid = conn.execute("INSERT INTO sources(name, url, mode, incognito, status, batch_id) VALUES(?,?,?,?,'queued',?)",
                               (name[:200], v.url, mode, int(incognito), batch_id)).lastrowid
            worker.enqueue(conn, int(sid), priority=-1)
            created.append(int(sid))
    return {"created": created, "skipped": skipped}


def video_dicts(videos: list[Video]) -> list[dict]:
    return [asdict(v) for v in videos]
