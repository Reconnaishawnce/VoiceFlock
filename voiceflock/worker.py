"""Job queue worker. Claims queued jobs from SQLite one at a time and runs the
pipeline. Run inside the web process (default) or as separate processes:
`voiceflock worker`."""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback

from . import db, gallery
from .pipeline import process_source

log = logging.getLogger(__name__)


def enqueue(conn, source_id: int, job_type: str = "process_source", priority: int = 0) -> int:
    cur = conn.execute("INSERT INTO jobs(type, source_id, priority) VALUES(?,?,?)", (job_type, source_id, priority))
    conn.execute("UPDATE sources SET status='queued', error=NULL WHERE id=?", (source_id,))
    return int(cur.lastrowid)


def claim_job(conn, worker_name: str) -> dict | None:
    with db.tx(conn):
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' ORDER BY priority DESC, id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE jobs SET status='running', worker=?, started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                     " heartbeat_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), progress=0, stage='starting' WHERE id=?",
                     (worker_name, row["id"]))
    return db.row(conn, "SELECT * FROM jobs WHERE id=?", (row["id"],))


def run_job(conn, job: dict, gcache: gallery.GalleryCache) -> None:
    jid = job["id"]
    last = [0.0]

    def progress(stage: str, frac: float):
        now = time.time()
        if now - last[0] < 0.5 and frac < 1.0:
            return
        last[0] = now
        try:
            conn.execute("UPDATE jobs SET stage=?, progress=?, heartbeat_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                         " WHERE id=?", (stage, float(frac) * 100.0, jid))
        except Exception:
            pass

    try:
        if job["type"] == "process_source":
            process_source(conn, int(job["source_id"]), progress, gcache)
        else:
            raise ValueError(f"unknown job type {job['type']}")
        conn.execute("UPDATE jobs SET status='done', progress=100, stage='done',"
                     " finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (jid,))
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        log.error("job %s failed: %s\n%s", jid, err, traceback.format_exc())
        conn.execute("UPDATE jobs SET status='failed', error=?, log=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                     " WHERE id=?", (err, traceback.format_exc()[-4000:], jid))
        if job["source_id"] is not None:
            conn.execute("UPDATE sources SET status='failed', error=? WHERE id=?", (err, job["source_id"]))


def worker_loop(stop: threading.Event | None = None, poll_s: float = 1.5, name: str | None = None) -> None:
    name = name or f"{socket.gethostname()}-{os.getpid()}-{threading.get_ident()}"
    conn = db.connect()
    gcache = gallery.GalleryCache()
    log.info("worker %s started", name)
    while not (stop and stop.is_set()):
        try:
            job = claim_job(conn, name)
        except Exception as e:  # noqa: BLE001
            log.warning("claim failed: %s", e)
            job = None
        if job is None:
            time.sleep(poll_s)
            continue
        log.info("job %s (%s source=%s) start", job["id"], job["type"], job["source_id"])
        run_job(conn, job, gcache)
        log.info("job %s end", job["id"])


def start_background_worker() -> tuple[threading.Thread, threading.Event]:
    stop = threading.Event()
    t = threading.Thread(target=worker_loop, args=(stop,), name="voiceflock-worker", daemon=True)
    t.start()
    return t, stop
