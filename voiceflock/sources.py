"""Shared helpers for recordings: import batches and full deletion."""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from . import config, db


def new_batch(conn: sqlite3.Connection, name: str, kind: str) -> int:
    """A batch groups the recordings of one import (a multi-file upload, a channel import, one feed poll)."""
    return int(conn.execute("INSERT INTO import_batches(name, kind) VALUES(?,?)", (name[:200], kind)).lastrowid)


def delete_empty_provisional(conn: sqlite3.Connection, person_id: int | None) -> bool:
    if not person_id:
        return False
    p = db.row(conn, "SELECT kind FROM persons WHERE id=?", (person_id,))
    if p and p["kind"] == "provisional":
        if conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (person_id,)).fetchone()[0] == 0:
            conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
            return True
    return False


def delete_source(conn: sqlite3.Connection, sid: int) -> bool:
    """Remove a recording, its speakers, prints, transcript, clips and files. Returns False when it does not exist."""
    s = db.row(conn, "SELECT * FROM sources WHERE id=?", (sid,))
    if not s:
        return False
    with db.tx(conn):
        pids = [r["person_id"] for r in conn.execute(
            "SELECT DISTINCT person_id FROM source_speakers WHERE source_id=? AND person_id IS NOT NULL", (sid,))]
        clips = [r["clip_path"] for r in conn.execute("SELECT clip_path FROM source_speakers WHERE source_id=?", (sid,))]
        conn.execute("DELETE FROM prints WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM transcript_segments WHERE source_id=?", (sid,))
        conn.execute("DELETE FROM source_speakers WHERE source_id=?", (sid,))
        conn.execute("UPDATE jobs SET status='cancelled' WHERE source_id=? AND status='queued'", (sid,))
        conn.execute("DELETE FROM sources WHERE id=?", (sid,))
        for p in pids:
            delete_empty_provisional(conn, p)
        db.bump_prints_version(conn)
    from .transcribe import raw_path
    try:
        raw_path(sid).unlink(missing_ok=True)
    except OSError:
        pass
    for p in (s["orig_path"], s["wav_path"]):
        if p and Path(p).exists():
            try:
                Path(p).unlink()
            except OSError:
                pass
    if s["url"]:
        shutil.rmtree(config.ORIG_DIR / f"url_{sid}", ignore_errors=True)
    for c in clips:
        if c:
            try:
                (config.CLIP_DIR / c).unlink(missing_ok=True)
            except OSError:
                pass
    from .stills import delete_stills
    delete_stills(sid)
    return True
