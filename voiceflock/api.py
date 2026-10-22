"""FastAPI app: JSON API + static single-page UI."""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, audio, config, db, gallery, worker

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")


class UrlIn(BaseModel):
    url: str
    mode: str = "bulk"
    incognito: bool = False
    person_name: str | None = None

class PersonIn(BaseModel):
    name: str
    notes: str | None = None

class PersonPatch(BaseModel):
    name: str | None = None
    notes: str | None = None

class MergeIn(BaseModel):
    into_id: int

class NameIn(BaseModel):
    name: str | None = None
    person_id: int | None = None


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name or "upload").strip() or "upload"
    return name[:150]


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def create_app(run_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db()
        stop = None
        if run_worker:
            _, stop = worker.start_background_worker()
        yield
        if stop:
            stop.set()

    app = FastAPI(title="VoiceFlock", version=__version__, lifespan=lifespan)
    app.state.gcache = gallery.GalleryCache()

    # ---------------------------------------------------------------- helpers
    def source_out(conn, s: dict) -> dict:
        s = dict(s)
        s["has_audio"] = bool(s.get("wav_path") and Path(s["wav_path"]).exists())
        job = db.row(conn, "SELECT id, status, stage, progress, error FROM jobs WHERE source_id=? ORDER BY id DESC LIMIT 1",
                     (s["id"],))
        s["job"] = job
        return s

    def speaker_out(conn, sp: dict) -> dict:
        sp = dict(sp)
        sp.pop("embedding", None)
        sp["clip_url"] = f"/clips/{sp['clip_path']}" if sp.get("clip_path") else None
        sp["segments"] = db.rows(conn, "SELECT start_s, end_s FROM segments WHERE source_speaker_id=? ORDER BY start_s",
                                 (sp["id"],))
        sp["candidates"] = db.rows(conn,
            "SELECT m.*, p.name AS person_name, p.kind AS person_kind FROM matches m JOIN persons p ON p.id=m.person_id"
            " WHERE m.source_speaker_id=? ORDER BY m.rank", (sp["id"],))
        for c in sp["candidates"]:
            c["heard_in"] = person_sources(conn, c["person_id"], exclude_source=sp["source_id"], limit=3)
        if sp.get("person_id"):
            p = db.row(conn, "SELECT id, name, kind FROM persons WHERE id=?", (sp["person_id"],))
            sp["person"] = p
        else:
            sp["person"] = None
        return sp

    def person_sources(conn, person_id: int, exclude_source: int | None = None, limit: int = 20) -> list[dict]:
        """Recordings where this person's voice was heard (via assigned speakers), oldest first."""
        q = ("SELECT DISTINCT s.id, s.name, s.created_at, ss.first_start_s, ss.id AS speaker_id, ss.assignment, ss.clip_path"
             " FROM source_speakers ss JOIN sources s ON s.id=ss.source_id WHERE ss.person_id=? AND s.status!='deleted'")
        params: list[Any] = [person_id]
        if exclude_source is not None:
            q += " AND s.id!=?"
            params.append(exclude_source)
        q += " ORDER BY s.id LIMIT ?"
        params.append(limit)
        out = db.rows(conn, q, tuple(params))
        for o in out:
            o["clip_url"] = f"/clips/{o['clip_path']}" if o.get("clip_path") else None
        return out

    def person_out(conn, p: dict) -> dict:
        p = dict(p)
        p["n_prints"] = conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (p["id"],)).fetchone()[0]
        p["n_sources"] = conn.execute("SELECT COUNT(DISTINCT source_id) FROM source_speakers WHERE person_id=?",
                                      (p["id"],)).fetchone()[0]
        p["speech_seconds"] = conn.execute("SELECT COALESCE(SUM(speech_seconds),0) FROM source_speakers WHERE person_id=?",
                                           (p["id"],)).fetchone()[0]
        p["heard_in"] = person_sources(conn, p["id"], limit=50)
        return p

    def delete_empty_provisional(conn, person_id: int | None) -> bool:
        if not person_id:
            return False
        p = db.row(conn, "SELECT kind FROM persons WHERE id=?", (person_id,))
        if p and p["kind"] == "provisional":
            n = conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (person_id,)).fetchone()[0]
            if n == 0:
                conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
                return True
        return False

    def speaker_embs(conn, speaker_id: int):
        sp = db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,))
        if sp is None:
            raise HTTPException(404, "speaker not found")
        mean = db.blob_to_emb(sp["embedding"])
        chunks = [db.blob_to_emb(r["embedding"]) for r in conn.execute(
            "SELECT embedding FROM prints WHERE source_speaker_id=? AND is_mean=0", (speaker_id,))]
        return sp, mean, chunks

    def assign_speaker(conn, speaker_id: int, person_id: int, origin: str, assignment: str) -> None:
        """Move a recording-speaker (and its prints) to a person."""
        sp, mean, chunks = speaker_embs(conn, speaker_id)
        if mean is None:
            raise HTTPException(400, "speaker has no embedding")
        old_pid = sp["person_id"]
        settings = config.Settings(conn)
        # drop whatever prints this speaker contributed elsewhere, then re-add under the target person
        conn.execute("DELETE FROM prints WHERE source_speaker_id=?", (speaker_id,))
        if not chunks:
            chunks = []
        gallery.add_prints(conn, person_id, sp["source_id"], speaker_id, mean, chunks, origin,
                           sp["speech_seconds"], sp["quality_score"])
        gallery.prune_prints(conn, person_id, int(settings["max_prints_per_person"]))
        conn.execute("UPDATE source_speakers SET person_id=?, assignment=? WHERE id=?", (person_id, assignment, speaker_id))
        conn.execute("UPDATE matches SET status='confirmed', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                     " WHERE source_speaker_id=? AND person_id=?", (speaker_id, person_id))
        conn.execute("UPDATE matches SET status='rejected' WHERE source_speaker_id=? AND person_id!=? AND status IN ('open','auto')",
                     (speaker_id, person_id))
        if old_pid and old_pid != person_id:
            delete_empty_provisional(conn, old_pid)

    def unassign_speaker(conn, speaker_id: int) -> None:
        sp = db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,))
        if sp is None:
            raise HTTPException(404, "speaker not found")
        conn.execute("DELETE FROM prints WHERE source_speaker_id=?", (speaker_id,))
        db.bump_prints_version(conn)
        conn.execute("UPDATE source_speakers SET person_id=NULL, assignment=NULL WHERE id=?", (speaker_id,))
        conn.execute("UPDATE matches SET status='open', decided_at=NULL WHERE source_speaker_id=? AND status IN ('auto','confirmed')",
                     (speaker_id,))
        delete_empty_provisional(conn, sp["person_id"])

    def find_or_create_person(conn, name: str) -> int:
        name = name.strip()
        if not name:
            raise HTTPException(400, "name required")
        p = db.row(conn, "SELECT id FROM persons WHERE name=? COLLATE NOCASE AND kind='named'", (name,))
        if p:
            return int(p["id"])
        cur = conn.execute("INSERT INTO persons(name, kind) VALUES(?, 'named')", (name,))
        return int(cur.lastrowid)

    # ---------------------------------------------------------------- meta
    @app.get("/api/health")
    def health(conn=Depends(get_conn)):
        info: dict[str, Any] = {"version": __version__, "ffmpeg": shutil.which("ffmpeg") is not None,
                                "yt_dlp": shutil.which("yt-dlp") is not None}
        try:
            import torch
            from .diarize import pick_device
            info["torch"] = torch.__version__
            info["device"] = pick_device(config.Settings(conn)["device"])
        except Exception as e:  # noqa: BLE001
            info["torch"] = None
            info["torch_error"] = str(e)
        info["hf_token_set"] = bool(config.Settings(conn)["hf_token"])
        info["queue"] = db.row(conn, "SELECT SUM(status='queued') AS queued, SUM(status='running') AS running,"
                                     " SUM(status='failed') AS failed, SUM(status='done') AS done FROM jobs")
        return info

    @app.get("/api/stats")
    def stats(conn=Depends(get_conn)):
        g = app.state.gcache.get(conn)
        return {
            "sources": db.row(conn, "SELECT COUNT(*) AS n, COALESCE(SUM(duration_s),0) AS seconds FROM sources WHERE status!='deleted'"),
            "persons_named": conn.execute("SELECT COUNT(*) FROM persons WHERE kind='named'").fetchone()[0],
            "persons_unknown": conn.execute("SELECT COUNT(*) FROM persons WHERE kind='provisional'").fetchone()[0],
            "prints": conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0],
            "cohort_size": len(g.cohort), "score_mode": g.score_mode,
            "calibration": {m: {"a": c.a, "b": c.b} for m, c in g.calibrations.items()},
            "calibrated": bool(db.row(conn, "SELECT 1 AS x FROM calibrations WHERE active=1 LIMIT 1")),
            "open_alerts": conn.execute(
                "SELECT COUNT(*) FROM matches m JOIN persons p ON p.id=m.person_id WHERE m.status='auto' AND p.kind='named'").fetchone()[0],
        }

    # ---------------------------------------------------------------- settings
    @app.get("/api/settings")
    def get_settings(conn=Depends(get_conn)):
        s = config.Settings(conn)
        return {"values": s.all(), "specs": s.specs()}

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any], conn=Depends(get_conn)):
        s = config.Settings(conn)
        with db.tx(conn):
            for k, v in body.items():
                if k not in config.SETTING_SPECS:
                    raise HTTPException(400, f"unknown setting {k}")
                if config.SETTING_SPECS[k]["type"] == "secret" and isinstance(v, str) and v.startswith("•"):
                    continue  # masked value echoed back; keep the stored one
                try:
                    s.set(k, v)
                except (ValueError, TypeError) as e:
                    raise HTTPException(400, f"bad value for {k}: {e}")
        db.bump_prints_version(conn)  # scoring settings affect the gallery snapshot
        return {"values": s.all()}

    # ---------------------------------------------------------------- uploads
    def _create_source(conn, name: str, filename: str | None, url: str | None, orig_path: str | None,
                       mode: str, incognito: bool, person_name: str | None) -> dict:
        if mode not in ("bulk", "match", "enroll"):
            raise HTTPException(400, "mode must be bulk|match|enroll")
        pid = None
        if mode == "enroll":
            if not person_name:
                raise HTTPException(400, "enroll mode needs a person name")
            pid = find_or_create_person(conn, person_name)
            incognito = False
        cur = conn.execute(
            "INSERT INTO sources(name, original_filename, url, orig_path, mode, incognito, enroll_person_id, status)"
            " VALUES(?,?,?,?,?,?,?, 'queued')", (name, filename, url, orig_path, mode, int(incognito), pid))
        sid = int(cur.lastrowid)
        jid = worker.enqueue(conn, sid)
        return {"source_id": sid, "job_id": jid, "name": name}

    @app.post("/api/upload")
    async def upload(files: list[UploadFile] = File(...), mode: str = Form("bulk"), incognito: bool = Form(False),
                     person_name: str | None = Form(None), conn=Depends(get_conn)):
        config.ensure_dirs()
        out = []
        for f in files:
            fname = _safe_name(f.filename or "upload")
            if Path(fname).suffix.lower() not in audio.AUDIO_EXTS:
                out.append({"name": fname, "error": "unsupported file type"})
                continue
            with db.tx(conn):
                cur = conn.execute("INSERT INTO sources(name, original_filename, mode, incognito, status) VALUES(?,?,?,?, 'queued')",
                                   (Path(fname).stem, fname, mode, int(incognito)))
                sid = int(cur.lastrowid)
            dest = config.ORIG_DIR / f"{sid}_{fname}"
            with open(dest, "wb") as fh:
                while True:
                    chunk = await f.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
            with db.tx(conn):
                conn.execute("UPDATE sources SET orig_path=? WHERE id=?", (str(dest), sid))
                if mode == "enroll":
                    if not person_name:
                        raise HTTPException(400, "enroll mode needs a person name")
                    pid = find_or_create_person(conn, person_name)
                    conn.execute("UPDATE sources SET enroll_person_id=?, incognito=0 WHERE id=?", (pid, sid))
                jid = worker.enqueue(conn, sid)
            out.append({"source_id": sid, "job_id": jid, "name": fname})
        return {"created": out}


    @app.post("/api/upload_url")
    def upload_url(body: UrlIn, conn=Depends(get_conn)):
        if not re.match(r"^https?://", body.url.strip()):
            raise HTTPException(400, "url must start with http(s)://")
        with db.tx(conn):
            res = _create_source(conn, body.url.strip(), None, body.url.strip(), None, body.mode, body.incognito,
                                 body.person_name)
        return res

    # ---------------------------------------------------------------- sources
    @app.get("/api/sources")
    def list_sources(mode: str | None = None, status: str | None = None, limit: int = 100, offset: int = 0,
                     conn=Depends(get_conn)):
        q = "SELECT * FROM sources WHERE status!='deleted'"
        params: list[Any] = []
        if mode:
            q += " AND mode=?"; params.append(mode)
        if status:
            q += " AND status=?"; params.append(status)
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        out = []
        for s in db.rows(conn, q, tuple(params)):
            s = source_out(conn, s)
            s["speakers"] = db.rows(conn,
                "SELECT ss.id, ss.label, ss.speech_seconds, ss.quality_score, ss.person_id, ss.assignment, ss.clip_path,"
                " p.name AS person_name, p.kind AS person_kind,"
                " (SELECT probability FROM matches m WHERE m.source_speaker_id=ss.id AND m.rank=1) AS top_prob,"
                " (SELECT p2.name FROM matches m JOIN persons p2 ON p2.id=m.person_id WHERE m.source_speaker_id=ss.id AND m.rank=1) AS top_name"
                " FROM source_speakers ss LEFT JOIN persons p ON p.id=ss.person_id WHERE ss.source_id=? ORDER BY ss.speech_seconds DESC",
                (s["id"],))
            for sp in s["speakers"]:
                sp["clip_url"] = f"/clips/{sp['clip_path']}" if sp.get("clip_path") else None
            out.append(s)
        return {"sources": out}

    @app.get("/api/sources/{sid}")
    def get_source(sid: int, conn=Depends(get_conn)):
        s = db.row(conn, "SELECT * FROM sources WHERE id=? AND status!='deleted'", (sid,))
        if not s:
            raise HTTPException(404, "source not found")
        s = source_out(conn, s)
        s["speakers"] = [speaker_out(conn, sp) for sp in
                         db.rows(conn, "SELECT * FROM source_speakers WHERE source_id=? ORDER BY speech_seconds DESC", (sid,))]
        s["settings"] = {k: config.Settings(conn)[k] for k in ("match_threshold", "confirm_threshold")}
        return s

    @app.get("/api/sources/{sid}/audio")
    def source_audio(sid: int, conn=Depends(get_conn)):
        s = db.row(conn, "SELECT wav_path, orig_path, original_filename FROM sources WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        p = s["wav_path"] if s["wav_path"] and Path(s["wav_path"]).exists() else s["orig_path"]
        if not p or not Path(p).exists():
            raise HTTPException(404, "audio file missing")
        return FileResponse(p)

    @app.post("/api/sources/{sid}/reprocess")
    def reprocess(sid: int, conn=Depends(get_conn)):
        if not db.row(conn, "SELECT id FROM sources WHERE id=? AND status!='deleted'", (sid,)):
            raise HTTPException(404)
        with db.tx(conn):
            jid = worker.enqueue(conn, sid)
        return {"job_id": jid}

    @app.post("/api/sources/{sid}/forget")
    def forget_source(sid: int, conn=Depends(get_conn)):
        """Undo what this recording added to the voice database (prints, new unknown voices),
        keeping the recording and its match results."""
        with db.tx(conn):
            pids = [r["person_id"] for r in conn.execute(
                "SELECT DISTINCT person_id FROM source_speakers WHERE source_id=? AND person_id IS NOT NULL", (sid,))]
            conn.execute("DELETE FROM prints WHERE source_id=?", (sid,))
            conn.execute("UPDATE source_speakers SET person_id=NULL, assignment=NULL WHERE source_id=?", (sid,))
            conn.execute("UPDATE matches SET status='open' WHERE status IN ('auto','confirmed') AND source_speaker_id IN"
                         " (SELECT id FROM source_speakers WHERE source_id=?)", (sid,))
            removed = sum(delete_empty_provisional(conn, p) for p in pids)
            conn.execute("UPDATE sources SET incognito=1 WHERE id=?", (sid,))
            db.bump_prints_version(conn)
        return {"removed_unknown_voices": removed}

    @app.delete("/api/sources/{sid}")
    def delete_source(sid: int, conn=Depends(get_conn)):
        s = db.row(conn, "SELECT * FROM sources WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        with db.tx(conn):
            pids = [r["person_id"] for r in conn.execute(
                "SELECT DISTINCT person_id FROM source_speakers WHERE source_id=? AND person_id IS NOT NULL", (sid,))]
            clips = [r["clip_path"] for r in conn.execute("SELECT clip_path FROM source_speakers WHERE source_id=?", (sid,))]
            conn.execute("DELETE FROM prints WHERE source_id=?", (sid,))
            conn.execute("DELETE FROM source_speakers WHERE source_id=?", (sid,))
            conn.execute("UPDATE jobs SET status='cancelled' WHERE source_id=? AND status='queued'", (sid,))
            conn.execute("DELETE FROM sources WHERE id=?", (sid,))
            for p in pids:
                delete_empty_provisional(conn, p)
            db.bump_prints_version(conn)
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
                (config.CLIP_DIR / c).unlink(missing_ok=True)
        return {"deleted": sid}

    # ---------------------------------------------------------------- jobs
    @app.get("/api/jobs")
    def list_jobs(status: str | None = None, limit: int = 50, conn=Depends(get_conn)):
        q = ("SELECT j.*, s.name AS source_name, s.mode AS source_mode FROM jobs j LEFT JOIN sources s ON s.id=j.source_id")
        params: list[Any] = []
        if status:
            q += " WHERE j.status=?"; params.append(status)
        q += " ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, j.id DESC LIMIT ?"
        params.append(limit)
        return {"jobs": db.rows(conn, q, tuple(params))}

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: int, conn=Depends(get_conn)):
        n = conn.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND status='queued'", (jid,)).rowcount
        if n:
            conn.execute("UPDATE sources SET status='failed', error='cancelled' WHERE id=(SELECT source_id FROM jobs WHERE id=?)", (jid,))
        return {"cancelled": bool(n)}

    @app.post("/api/jobs/{jid}/retry")
    def retry_job(jid: int, conn=Depends(get_conn)):
        j = db.row(conn, "SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            raise HTTPException(404)
        with db.tx(conn):
            new = worker.enqueue(conn, int(j["source_id"]), j["type"])
        return {"job_id": new}

    # ---------------------------------------------------------------- persons
    @app.get("/api/persons")
    def list_persons(kind: str | None = None, q: str | None = None, conn=Depends(get_conn)):
        sql = "SELECT * FROM persons WHERE 1=1"
        params: list[Any] = []
        if kind:
            sql += " AND kind=?"; params.append(kind)
        if q:
            sql += " AND name LIKE ?"; params.append(f"%{q}%")
        sql += " ORDER BY kind DESC, name COLLATE NOCASE"
        return {"persons": [person_out(conn, p) for p in db.rows(conn, sql, tuple(params))]}


    @app.post("/api/persons")
    def create_person(body: PersonIn, conn=Depends(get_conn)):
        with db.tx(conn):
            pid = find_or_create_person(conn, body.name)
            if body.notes:
                conn.execute("UPDATE persons SET notes=? WHERE id=?", (body.notes, pid))
        return person_out(conn, db.row(conn, "SELECT * FROM persons WHERE id=?", (pid,)))

    @app.get("/api/persons/{pid}")
    def get_person(pid: int, conn=Depends(get_conn)):
        p = db.row(conn, "SELECT * FROM persons WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404)
        p = person_out(conn, p)
        p["prints"] = db.rows(conn,
            "SELECT pr.id, pr.is_mean, pr.origin, pr.speech_seconds, pr.quality, pr.created_at, pr.source_id, pr.source_speaker_id,"
            " s.name AS source_name, ss.label FROM prints pr LEFT JOIN sources s ON s.id=pr.source_id"
            " LEFT JOIN source_speakers ss ON ss.id=pr.source_speaker_id WHERE pr.person_id=? ORDER BY pr.id DESC", (pid,))
        p["matches"] = db.rows(conn,
            "SELECT m.*, ss.source_id, s.name AS source_name, ss.label, ss.clip_path FROM matches m"
            " JOIN source_speakers ss ON ss.id=m.source_speaker_id JOIN sources s ON s.id=ss.source_id"
            " WHERE m.person_id=? AND m.rank=1 ORDER BY m.id DESC LIMIT 100", (pid,))
        for m in p["matches"]:
            m["clip_url"] = f"/clips/{m['clip_path']}" if m.get("clip_path") else None
        return p


    @app.patch("/api/persons/{pid}")
    def patch_person(pid: int, body: PersonPatch, conn=Depends(get_conn)):
        p = db.row(conn, "SELECT * FROM persons WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404)
        with db.tx(conn):
            if body.name is not None and body.name.strip():
                existing = db.row(conn, "SELECT id FROM persons WHERE name=? COLLATE NOCASE AND kind='named' AND id!=?",
                                  (body.name.strip(), pid))
                if existing:  # naming an unknown voice with an existing name merges into that person
                    merge_persons(conn, pid, int(existing["id"]))
                    pid = int(existing["id"])
                else:
                    conn.execute("UPDATE persons SET name=?, kind='named', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                                 (body.name.strip(), pid))
                    conn.execute("UPDATE source_speakers SET assignment='named' WHERE person_id=? AND assignment='auto'", (pid,))
                    conn.execute("UPDATE matches SET status='confirmed' WHERE person_id=? AND status='auto'", (pid,))
            if body.notes is not None:
                conn.execute("UPDATE persons SET notes=? WHERE id=?", (body.notes, pid))
            db.bump_prints_version(conn)
        return person_out(conn, db.row(conn, "SELECT * FROM persons WHERE id=?", (pid,)))

    def merge_persons(conn, from_id: int, into_id: int) -> None:
        if from_id == into_id:
            return
        conn.execute("UPDATE prints SET person_id=? WHERE person_id=?", (into_id, from_id))
        conn.execute("UPDATE source_speakers SET person_id=? WHERE person_id=?", (into_id, from_id))
        # keep one match row per (speaker, person)
        conn.execute("DELETE FROM matches WHERE person_id=? AND source_speaker_id IN"
                     " (SELECT source_speaker_id FROM matches WHERE person_id=?)", (from_id, into_id))
        conn.execute("UPDATE matches SET person_id=? WHERE person_id=?", (into_id, from_id))
        conn.execute("DELETE FROM persons WHERE id=?", (from_id,))
        gallery.prune_prints(conn, into_id, int(config.Settings(conn)["max_prints_per_person"]))
        db.bump_prints_version(conn)


    @app.post("/api/persons/{pid}/merge")
    def merge(pid: int, body: MergeIn, conn=Depends(get_conn)):
        if not db.row(conn, "SELECT id FROM persons WHERE id=?", (body.into_id,)):
            raise HTTPException(404, "target person not found")
        with db.tx(conn):
            merge_persons(conn, pid, body.into_id)
        return person_out(conn, db.row(conn, "SELECT * FROM persons WHERE id=?", (body.into_id,)))

    @app.delete("/api/persons/{pid}")
    def delete_person(pid: int, conn=Depends(get_conn)):
        with db.tx(conn):
            conn.execute("UPDATE source_speakers SET person_id=NULL, assignment=NULL WHERE person_id=?", (pid,))
            n = conn.execute("DELETE FROM persons WHERE id=?", (pid,)).rowcount
            db.bump_prints_version(conn)
        if not n:
            raise HTTPException(404)
        return {"deleted": pid}

    @app.get("/api/voices/unnamed")
    def unnamed_voices(limit: int = 200, conn=Depends(get_conn)):
        """Build-database view: unnamed voices with preview clips, most-heard first."""
        ps = db.rows(conn,
            "SELECT p.*, (SELECT COUNT(DISTINCT source_id) FROM source_speakers WHERE person_id=p.id) AS n_sources,"
            " (SELECT COALESCE(SUM(speech_seconds),0) FROM source_speakers WHERE person_id=p.id) AS speech_seconds"
            " FROM persons p WHERE p.kind='provisional' ORDER BY n_sources DESC, speech_seconds DESC, p.id DESC LIMIT ?", (limit,))
        for p in ps:
            p["heard_in"] = person_sources(conn, p["id"], limit=10)
        return {"voices": ps}

    # ---------------------------------------------------------------- speakers / matches

    @app.post("/api/speakers/{speaker_id}/name")
    def name_speaker(speaker_id: int, body: NameIn, conn=Depends(get_conn)):
        with db.tx(conn):
            if body.person_id:
                pid = body.person_id
                if not db.row(conn, "SELECT id FROM persons WHERE id=?", (pid,)):
                    raise HTTPException(404, "person not found")
            else:
                pid = find_or_create_person(conn, body.name or "")
            assign_speaker(conn, speaker_id, pid, "named", "named")
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,)))

    @app.post("/api/speakers/{speaker_id}/unassign")
    def unassign(speaker_id: int, conn=Depends(get_conn)):
        with db.tx(conn):
            unassign_speaker(conn, speaker_id)
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,)))

    @app.post("/api/matches/{mid}/confirm")
    def confirm_match(mid: int, conn=Depends(get_conn)):
        m = db.row(conn, "SELECT * FROM matches WHERE id=?", (mid,))
        if not m:
            raise HTTPException(404)
        with db.tx(conn):
            assign_speaker(conn, int(m["source_speaker_id"]), int(m["person_id"]), "confirm", "confirmed")
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (m["source_speaker_id"],)))

    @app.post("/api/matches/{mid}/reject")
    def reject_match(mid: int, conn=Depends(get_conn)):
        m = db.row(conn, "SELECT * FROM matches WHERE id=?", (mid,))
        if not m:
            raise HTTPException(404)
        with db.tx(conn):
            sp = db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (m["source_speaker_id"],))
            if sp and sp["person_id"] == m["person_id"] and sp["assignment"] in ("auto", "confirmed"):
                unassign_speaker(conn, int(sp["id"]))
            conn.execute("UPDATE matches SET status='rejected', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (mid,))
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (m["source_speaker_id"],)))

    @app.get("/api/alerts")
    def alerts(limit: int = 100, conn=Depends(get_conn)):
        """Bulk-scan feed: 'we think X is in this file' + unnamed voices that keep showing up."""
        known = db.rows(conn,
            "SELECT m.id AS match_id, m.probability, m.margin, m.status, ss.id AS speaker_id, ss.quality_score, ss.speech_seconds,"
            " ss.first_start_s, ss.clip_path, s.id AS source_id, s.name AS source_name, s.created_at, p.id AS person_id, p.name AS person_name"
            " FROM matches m JOIN source_speakers ss ON ss.id=m.source_speaker_id JOIN sources s ON s.id=ss.source_id"
            " JOIN persons p ON p.id=m.person_id WHERE m.rank=1 AND m.status IN ('auto','confirmed') AND p.kind='named'"
            " ORDER BY m.id DESC LIMIT ?", (limit,))
        for a in known:
            a["clip_url"] = f"/clips/{a['clip_path']}" if a.get("clip_path") else None
            a["heard_in"] = person_sources(conn, a["person_id"], exclude_source=a["source_id"], limit=5)
        recurring = db.rows(conn,
            "SELECT p.id AS person_id, p.name, COUNT(DISTINCT ss.source_id) AS n_sources, SUM(ss.speech_seconds) AS speech_seconds"
            " FROM persons p JOIN source_speakers ss ON ss.person_id=p.id WHERE p.kind='provisional'"
            " GROUP BY p.id HAVING n_sources>=2 ORDER BY n_sources DESC, speech_seconds DESC LIMIT ?", (limit,))
        for r in recurring:
            r["heard_in"] = person_sources(conn, r["person_id"], limit=10)
        return {"known": known, "recurring_unknown": recurring,
                "confirm_threshold": config.Settings(conn)["confirm_threshold"]}

    # ---------------------------------------------------------------- static
    config.ensure_dirs()
    app.mount("/clips", StaticFiles(directory=str(config.CLIP_DIR)), name="clips")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.exception_handler(sqlite3.Error)
    async def sqlite_err(_, exc):
        return JSONResponse({"detail": f"database error: {exc}"}, status_code=500)

    return app


app = None  # created lazily by cli.serve so `uvicorn voiceflock.api:app` also works via get_app()


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
