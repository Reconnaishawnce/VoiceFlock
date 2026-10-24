"""SQLite helpers. One connection per call site; WAL so the web server and the
worker can read/write concurrently."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


def connect(path: Path | None = None) -> sqlite3.Connection:
    config.ensure_dirs()
    path = path or config.DB_PATH
    conn = sqlite3.connect(str(path), timeout=60, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def init_db(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('prints_version', '0')")
        _migrate(conn)
        # Orphaned 'running' jobs from a crashed worker go back to the queue.
        conn.execute("UPDATE jobs SET status='queued', stage=NULL, worker=NULL WHERE status='running'")
        conn.execute("UPDATE sources SET status='queued' WHERE status='processing'")
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _migrate(conn: sqlite3.Connection) -> None:
    """Additive migrations for databases created by older versions."""
    if "feed_id" not in _columns(conn, "sources"):
        conn.execute("ALTER TABLE sources ADD COLUMN feed_id INTEGER")
    if "result" not in _columns(conn, "jobs"):
        conn.execute("ALTER TABLE jobs ADD COLUMN result TEXT")
    if "alert" not in _columns(conn, "persons"):
        conn.execute("ALTER TABLE persons ADD COLUMN alert INTEGER NOT NULL DEFAULT 0")
    if "stills" not in _columns(conn, "source_speakers"):
        conn.execute("ALTER TABLE source_speakers ADD COLUMN stills TEXT")
    if "review_status" not in _columns(conn, "source_speakers"):
        conn.execute("ALTER TABLE source_speakers ADD COLUMN review_status TEXT")
        conn.execute("ALTER TABLE source_speakers ADD COLUMN notes TEXT")
    if "review" not in _columns(conn, "sources"):
        conn.execute("ALTER TABLE sources ADD COLUMN review INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE sources ADD COLUMN notes TEXT")
        conn.execute("ALTER TABLE sources ADD COLUMN stills_info TEXT")
    if "ignored" not in _columns(conn, "source_speakers"):
        conn.execute("ALTER TABLE source_speakers ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0")
    if "name_hints" not in _columns(conn, "source_speakers"):
        conn.execute("ALTER TABLE source_speakers ADD COLUMN name_hints TEXT")
    if "transcript_status" not in _columns(conn, "sources"):
        conn.execute("ALTER TABLE sources ADD COLUMN transcript_status TEXT")
        conn.execute("ALTER TABLE sources ADD COLUMN transcript_language TEXT")
        conn.execute("ALTER TABLE sources ADD COLUMN transcript_info TEXT")
    if "role" not in _columns(conn, "persons"):
        conn.execute("ALTER TABLE persons ADD COLUMN role TEXT")
        conn.execute("ALTER TABLE persons ADD COLUMN watch_reason TEXT")
        conn.execute("ALTER TABLE persons ADD COLUMN links TEXT")
    if "description" not in _columns(conn, "sources"):
        conn.execute("ALTER TABLE sources ADD COLUMN description TEXT")
    if "recorded_on" not in _columns(conn, "sources"):
        conn.execute("ALTER TABLE sources ADD COLUMN recorded_on TEXT")
    if "batch_id" not in _columns(conn, "sources"):
        conn.execute("ALTER TABLE sources ADD COLUMN batch_id INTEGER")
    if _columns(conn, "sightings") and "session_name" not in _columns(conn, "sightings"):
        conn.execute("ALTER TABLE sightings ADD COLUMN session_name TEXT")
    if "alert_sound" not in _columns(conn, "persons"):
        conn.execute("ALTER TABLE persons ADD COLUMN alert_sound TEXT")
        conn.execute("ALTER TABLE persons ADD COLUMN alert_message TEXT")


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Explicit transaction (connection is in autocommit mode otherwise)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def bump_prints_version(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='prints_version'")


def prints_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key='prints_version'").fetchone()
    return int(row[0]) if row else 0


# --- embedding (de)serialisation -------------------------------------------

def emb_to_blob(e: np.ndarray) -> bytes:
    return np.asarray(e, dtype=np.float32).tobytes()


def blob_to_emb(b: bytes | None) -> np.ndarray | None:
    if b is None:
        return None
    return np.frombuffer(b, dtype=np.float32).copy()


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None
