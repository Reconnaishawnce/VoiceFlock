"""Job queue worker. Claims queued jobs from SQLite one at a time and runs them.
Runs inside the web process (default) or as separate processes: `voiceflock worker`.

Job types: process_source (ML pipeline), rematch (gallery-only re-scoring),
selftest (load models on synthetic audio). The loop also polls RSS feeds."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import traceback

from . import db, gallery

log = logging.getLogger(__name__)
FEED_POLL_EVERY_S = 60.0


class JobCancelled(Exception):
    pass


def enqueue(conn, source_id: int | None, job_type: str = "process_source", priority: int = 0,
            dedupe: bool = False) -> int:
    """Queue a job. With dedupe=True, return the id of an identical already-queued job instead of adding one."""
    if dedupe:
        row = conn.execute("SELECT id FROM jobs WHERE type=? AND status='queued' AND source_id IS ?",
                           (job_type, source_id)).fetchone()
        if row:
            return int(row["id"])
    cur = conn.execute("INSERT INTO jobs(type, source_id, priority) VALUES(?,?,?)", (job_type, source_id, priority))
    if job_type == "process_source" and source_id is not None:
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
    last = [0.0, 0.0]          # last progress write, last cancel check
    cur_stage = [None]
    stage_log: list[str] = []

    def progress(stage: str, frac: float):
        now = time.time()
        if stage != cur_stage[0]:
            cur_stage[0] = stage
            stage_log.append(f"{time.strftime('%H:%M:%S')}  {stage}")
            conn.execute("UPDATE jobs SET log=? WHERE id=?", ("\n".join(stage_log[-40:]), jid))
            last[0] = 0.0
        if now - last[1] > 2.0:
            last[1] = now
            st = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if st and st[0] == "cancelled":
                raise JobCancelled()
        if now - last[0] < 0.5 and 0.0 < frac < 1.0:
            return
        last[0] = now
        try:
            conn.execute("UPDATE jobs SET stage=?, progress=?, heartbeat_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                         " WHERE id=?", (stage, float(frac) * 100.0, jid))
        except Exception:
            pass

    result = None
    try:
        if job["type"] == "process_source":
            from .pipeline import process_source
            result = process_source(conn, int(job["source_id"]), progress, gcache)
        elif job["type"] == "rematch":
            from .rematch import rematch
            ids = [int(job["source_id"])] if job["source_id"] is not None else None
            result = rematch(conn, ids, gcache, progress)
        elif job["type"] == "export_model":
            from .onnx_export import export
            progress("exporting ECAPA to ONNX", 0.1)
            result = export()
        elif job["type"] == "selftest":
            from .selftest import run as selftest
            result = selftest(conn, progress)
            if not result.get("ok"):
                raise RuntimeError(result.get("diarizer_error") or result.get("embedder_error") or "self-test failed")
        else:
            raise ValueError(f"unknown job type {job['type']}")
        conn.execute("UPDATE jobs SET status='done', progress=100, stage='done', result=?,"
                     " finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (json.dumps(result, default=str), jid))
    except JobCancelled:
        log.info("job %s cancelled", jid)
        conn.execute("UPDATE jobs SET status='cancelled', stage='cancelled', finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (jid,))
        if job["type"] == "process_source" and job["source_id"] is not None:
            conn.execute("UPDATE sources SET status='failed', error='cancelled' WHERE id=?", (job["source_id"],))
        return
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        log.error("job %s failed: %s\n%s", jid, err, traceback.format_exc())
        conn.execute("UPDATE jobs SET status='failed', error=?, log=?, result=?, finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                     " WHERE id=?", (err, traceback.format_exc()[-4000:], json.dumps(result, default=str) if result else None, jid))
        if job["type"] == "process_source" and job["source_id"] is not None:
            conn.execute("UPDATE sources SET status='failed', error=? WHERE id=?", (err, job["source_id"]))


def worker_loop(stop: threading.Event | None = None, poll_s: float = 1.5, name: str | None = None,
                poll_feeds: bool = True) -> None:
    name = name or f"{socket.gethostname()}-{os.getpid()}-{threading.get_ident()}"
    conn = db.connect()
    gcache = gallery.GalleryCache()
    log.info("worker %s started", name)
    last_feed_poll = 0.0
    while not (stop and stop.is_set()):
        if poll_feeds and time.time() - last_feed_poll > FEED_POLL_EVERY_S:
            last_feed_poll = time.time()
            try:
                from .feeds import poll_due
                n = poll_due(conn)
                if n:
                    log.info("feeds: queued %d new items", n)
            except Exception as e:  # noqa: BLE001
                log.warning("feed poll failed: %s", e)
            try:
                from .mobile import export_if_dirty
                out = export_if_dirty(conn)
                if out:
                    log.info("phone library exported to %s", out)
            except Exception as e:  # noqa: BLE001
                log.warning("library export failed: %s", e)
        try:
            job = claim_job(conn, name)
        except Exception as e:  # noqa: BLE001
            log.warning("claim failed: %s", e)
            job = None
        if job is None:
            if stop:
                stop.wait(poll_s)
            else:
                time.sleep(poll_s)
            continue
        log.info("job %s (%s source=%s) start", job["id"], job["type"], job["source_id"])
        run_job(conn, job, gcache)
        log.info("job %s end", job["id"])


def start_background_worker(n: int = 1) -> tuple[list[threading.Thread], threading.Event]:
    """Start n worker threads sharing one stop event. Only the first polls feeds."""
    stop = threading.Event()
    threads = []
    for i in range(max(1, n)):
        t = threading.Thread(target=worker_loop, args=(stop,), kwargs={"poll_feeds": i == 0, "name": f"worker-{i+1}"},
                             name=f"voiceflock-worker-{i+1}", daemon=True)
        t.start()
        threads.append(t)
    return threads, stop
