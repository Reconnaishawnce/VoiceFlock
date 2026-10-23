"""FastAPI app: JSON API + static single-page UI."""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import __version__, admin, audio, config, db, feeds, gallery, live, mobile, onnx_export, selfcal, stills, transcribe, transfer, worker, youtube

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
    alert: bool | None = None
    alert_sound: str | None = None      # siren | klaxon | chime | none | "" (= default)
    alert_message: str | None = None    # "" = default template


ALERT_SOUNDS = mobile.ALERT_SOUNDS
render_alert_message = mobile.render_alert_message


class GroupIn(BaseModel):
    name: str
    description: str | None = None


class GroupPatch(BaseModel):
    name: str | None = None
    description: str | None = None
    alert: bool | None = None
    alert_sound: str | None = None
    alert_message: str | None = None


class MembersIn(BaseModel):
    person_ids: list[int]


class WipeIn(BaseModel):
    confirm: str
    keep_settings: bool = True


class SourcePatch(BaseModel):
    notes: str | None = None
    review: bool | None = None
    name: str | None = None


class SpeakerPatch(BaseModel):
    notes: str | None = None
    review_status: str | None = None   # pending | done | skipped | "" (clear)


class YtListIn(BaseModel):
    url: str
    limit: int = 300
    title_regex: str | None = None
    min_minutes: float | None = None
    max_minutes: float | None = None
    date_from: str | None = None
    date_to: str | None = None


class YtImportIn(BaseModel):
    videos: list[dict]
    mode: str = "bulk"
    incognito: bool = False
    prefix: str | None = None

class MergeIn(BaseModel):
    into_id: int

class NameIn(BaseModel):
    name: str | None = None
    person_id: int | None = None


class FeedIn(BaseModel):
    url: str
    mode: str = "bulk"
    incognito: bool = False
    interval_min: int = 60
    backfill: int = 3


class FeedPatch(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    incognito: bool | None = None
    interval_min: int | None = None
    title: str | None = None


class RematchIn(BaseModel):
    source_id: int | None = None


def _safe_name(name: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", name or "upload").strip() or "upload"
    return name[:150]


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def lan_ips() -> list[str]:
    """Addresses other devices on the network can use to reach this machine."""
    import socket
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def gpu_info() -> dict:
    """What Windows/driver sees (nvidia-smi) vs what PyTorch can use."""
    info: dict[str, Any] = {"nvidia_driver_gpu": None, "torch_cuda": False, "torch_device_name": None, "mps": False}
    smi = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe" if os.name == "nt" else None)
    if smi and Path(smi).exists():
        try:
            import subprocess
            r = subprocess.run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader"], capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                info["nvidia_driver_gpu"] = r.stdout.strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            pass
    try:
        import torch
        info["torch_cuda"] = bool(torch.cuda.is_available())
        if info["torch_cuda"]:
            info["torch_device_name"] = torch.cuda.get_device_name(0)
        info["mps"] = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
    except Exception:  # noqa: BLE001
        pass
    return info


def create_app(run_worker: bool = True, workers: int = 1) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db()
        stop = None
        if run_worker:
            _, stop = worker.start_background_worker(workers)
        yield
        if stop:
            stop.set()

    app = FastAPI(title="VoiceFlock", version=__version__, lifespan=lifespan)
    app.state.gcache = gallery.GalleryCache()
    app.state.started_at = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())

    password = os.environ.get("VOICEFLOCK_PASSWORD", "")
    if password:
        @app.middleware("http")
        async def basic_auth(request: Request, call_next):
            """Optional HTTP basic auth (any username) when VOICEFLOCK_PASSWORD is set: handy when the
            laptop is reachable over LAN/Tailscale."""
            hdr = request.headers.get("authorization", "")
            ok = False
            if hdr.lower().startswith("basic "):
                try:
                    _, _, pw = base64.b64decode(hdr[6:]).decode("utf-8", "replace").partition(":")
                    ok = secrets.compare_digest(pw, password)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok:
                return Response("VoiceFlock: password required", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="VoiceFlock"'})
            return await call_next(request)

    # ---------------------------------------------------------------- helpers
    VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v"}

    def youtube_id(url: str | None) -> str | None:
        if not url:
            return None
        m = re.search(r"(?:youtube\.com/(?:watch\?(?:.*&)?v=|embed/|live/|shorts/)|youtu\.be/)([\w-]{11})", url)
        return m.group(1) if m else None

    def source_out(conn, s: dict) -> dict:
        s = dict(s)
        s["has_audio"] = bool(s.get("wav_path") and Path(s["wav_path"]).exists())
        s["youtube_id"] = youtube_id(s.get("url"))
        orig = Path(s["orig_path"]) if s.get("orig_path") else None
        s["media_kind"] = "youtube" if s["youtube_id"] else ("video" if orig and orig.suffix.lower() in VIDEO_EXTS and orig.exists() else "audio")
        job = db.row(conn, "SELECT id, status, stage, progress, error FROM jobs WHERE source_id=? ORDER BY id DESC LIMIT 1",
                     (s["id"],))
        s["job"] = job
        return s

    def speaker_out(conn, sp: dict) -> dict:
        sp = dict(sp)
        sp.pop("embedding", None)
        sp["clip_url"] = f"/clips/{sp['clip_path']}" if sp.get("clip_path") else None
        sp["stills"] = [{"t": x["t"], "url": f"/stills/{x['file']}"} for x in json.loads(sp.get("stills") or "[]")]
        sp["name_hints"] = json.loads(sp.get("name_hints") or "[]")
        sp["segments"] = db.rows(conn, "SELECT start_s, end_s FROM segments WHERE source_speaker_id=? ORDER BY start_s",
                                 (sp["id"],))
        if sp["segments"]:
            lg = max(sp["segments"], key=lambda g: g["end_s"] - g["start_s"])
            sp["longest"] = {"start_s": lg["start_s"], "seconds": round(lg["end_s"] - lg["start_s"], 1)}
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
        q = ("SELECT DISTINCT s.id, s.name, s.created_at, s.url, s.orig_path, ss.first_start_s, ss.id AS speaker_id, ss.assignment, ss.clip_path,"
             " ss.speech_seconds, ss.stills,"
             " (SELECT start_s FROM segments g WHERE g.source_speaker_id=ss.id ORDER BY (end_s-start_s) DESC LIMIT 1) AS longest_start_s"
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
            o["youtube_id"] = youtube_id(o.get("url"))
            op = o.pop("orig_path", None)
            o["media_kind"] = "youtube" if o["youtube_id"] else ("video" if op and Path(op).suffix.lower() in VIDEO_EXTS else "audio")
            o["watch_at"] = o["longest_start_s"] if o.get("longest_start_s") is not None else o.get("first_start_s")
            st = json.loads(o.pop("stills", None) or "[]")
            o["still_url"] = f"/stills/{st[0]['file']}" if st else None
        return out

    def person_out(conn, p: dict) -> dict:
        p = dict(p)
        p["n_prints"] = conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (p["id"],)).fetchone()[0]
        p["n_sources"] = conn.execute("SELECT COUNT(DISTINCT source_id) FROM source_speakers WHERE person_id=?",
                                      (p["id"],)).fetchone()[0]
        p["speech_seconds"] = conn.execute("SELECT COALESCE(SUM(speech_seconds),0) FROM source_speakers WHERE person_id=?",
                                           (p["id"],)).fetchone()[0]
        p["heard_in"] = person_sources(conn, p["id"], limit=50)
        p["glyph"] = person_glyph(conn, p["id"])
        p["groups"] = db.rows(conn, "SELECT g.id, g.name, g.alert FROM person_groups pg JOIN groups g ON g.id=pg.group_id WHERE pg.person_id=? ORDER BY g.name", (p["id"],))
        p["effective"] = mobile.effective_alert(p, p["groups"] and db.rows(conn,
            "SELECT g.* FROM person_groups pg JOIN groups g ON g.id=pg.group_id WHERE pg.person_id=? ORDER BY g.id", (p["id"],)) or [],
            config.Settings(conn))
        p["spoken"] = p["effective"]["spoken"]
        return p

    def person_mean(conn, person_id: int):
        import numpy as np
        rows = conn.execute("SELECT embedding FROM prints WHERE person_id=? AND is_mean=1 LIMIT 50", (person_id,)).fetchall()
        if not rows:
            rows = conn.execute("SELECT embedding FROM prints WHERE person_id=? LIMIT 50", (person_id,)).fetchall()
        if not rows:
            return None
        return gallery.l2n(np.mean(np.stack([db.blob_to_emb(r["embedding"]) for r in rows]), axis=0))

    def person_glyph(conn, person_id: int):
        m = person_mean(conn, person_id)
        return live.glyph(m) if m is not None else None

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
        conn.execute("UPDATE source_speakers SET person_id=?, assignment=?, review_status=CASE WHEN review_status IS NULL THEN NULL ELSE 'done' END WHERE id=?",
                     (person_id, assignment, speaker_id))
        conn.execute("UPDATE matches SET status='confirmed', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                     " WHERE source_speaker_id=? AND person_id=?", (speaker_id, person_id))
        # any other candidate for this speaker (including an earlier confirmation) contradicts the new assignment
        conn.execute("UPDATE matches SET status='rejected', decided_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
                     " WHERE source_speaker_id=? AND person_id!=? AND status IN ('open','auto','confirmed')",
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

    def queue_rematch(conn, source_id: int | None = None) -> int:
        return worker.enqueue(conn, source_id, "rematch", priority=-5, dedupe=True)

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
        info["gpu"] = gpu_info()
        info["workers"] = workers if run_worker else 0
        info["password_required"] = bool(password)
        info["lan_ips"] = lan_ips()
        info["supervised"] = admin.supervised()
        info["started_at"] = app.state.started_at
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
            "stills_mb": round(sum(p.stat().st_size for p in stills.still_dir().glob("*.jpg")) / 1e6, 1),
            "review_pending": conn.execute("SELECT COUNT(*) FROM source_speakers WHERE review_status='pending' AND ignored=0").fetchone()[0],
            "transcribed": conn.execute("SELECT COUNT(*) FROM sources WHERE transcript_status='done'").fetchone()[0],
            "untranscribed": conn.execute("SELECT COUNT(*) FROM sources WHERE status='done' AND incognito=0 AND (transcript_status IS NULL OR transcript_status='failed')").fetchone()[0],
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
        mobile.mark_dirty(conn)
        return {"values": s.all()}

    # ---------------------------------------------------------------- uploads
    def _create_source(conn, name: str, filename: str | None, url: str | None, orig_path: str | None,
                       mode: str, incognito: bool, person_name: str | None) -> dict:
        review = mode == "review"
        if review:
            mode = "bulk"
        if mode not in ("bulk", "match", "enroll"):
            raise HTTPException(400, "mode must be bulk|review|match|enroll")
        pid = None
        if mode == "enroll":
            if not person_name:
                raise HTTPException(400, "enroll mode needs a person name")
            pid = find_or_create_person(conn, person_name)
            incognito = False
        cur = conn.execute(
            "INSERT INTO sources(name, original_filename, url, orig_path, mode, incognito, enroll_person_id, status, review)"
            " VALUES(?,?,?,?,?,?,?, 'queued', ?)", (name, filename, url, orig_path, mode, int(incognito), pid, int(review)))
        sid = int(cur.lastrowid)
        jid = worker.enqueue(conn, sid)
        return {"source_id": sid, "job_id": jid, "name": name}

    @app.post("/api/upload")
    async def upload(files: list[UploadFile] = File(...), mode: str = Form("bulk"), incognito: bool = Form(False),
                     person_name: str | None = Form(None), force: bool = Form(False), conn=Depends(get_conn)):
        config.ensure_dirs()
        review = mode == "review"
        if review:
            mode = "bulk"
        if mode not in ("bulk", "match", "enroll"):
            raise HTTPException(400, "mode must be bulk|review|match|enroll")
        if mode == "enroll" and not (person_name or "").strip():
            raise HTTPException(400, "enroll mode needs a person name")
        out = []
        for f in files:
            fname = _safe_name(f.filename or "upload")
            if Path(fname).suffix.lower() not in audio.AUDIO_EXTS:
                out.append({"name": fname, "error": "unsupported file type"})
                continue
            with db.tx(conn):
                cur = conn.execute("INSERT INTO sources(name, original_filename, mode, incognito, status, review) VALUES(?,?,?,?, 'queued', ?)",
                                   (Path(fname).stem, fname, mode, int(incognito), int(review)))
                sid = int(cur.lastrowid)
            dest = config.ORIG_DIR / f"{sid}_{fname}"
            with open(dest, "wb") as fh:
                while True:
                    chunk = await f.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
            sha = audio.sha256_file(dest)
            dup = db.row(conn, "SELECT id, name, status FROM sources WHERE sha256=? AND id!=? AND status!='failed'", (sha, sid))
            if dup and not force:
                dest.unlink(missing_ok=True)
                conn.execute("DELETE FROM sources WHERE id=?", (sid,))
                out.append({"name": fname, "duplicate_of": dup["id"], "duplicate_name": dup["name"]})
                continue
            with db.tx(conn):
                conn.execute("UPDATE sources SET orig_path=?, sha256=? WHERE id=?", (str(dest), sha, sid))
                if mode == "enroll":
                    pid = find_or_create_person(conn, person_name)
                    conn.execute("UPDATE sources SET enroll_person_id=?, incognito=0 WHERE id=?", (pid, sid))
                jid = worker.enqueue(conn, sid)
            out.append({"source_id": sid, "job_id": jid, "name": fname})
        return {"created": out}


    @app.post("/api/upload_url")
    def upload_url(body: UrlIn, conn=Depends(get_conn)):
        if not re.match(r"^https?://", body.url.strip()):
            raise HTTPException(400, "url must start with http(s)://")
        dup = db.row(conn, "SELECT id, name FROM sources WHERE url=? AND status!='failed'", (body.url.strip(),))
        if dup:
            return {"duplicate_of": dup["id"], "duplicate_name": dup["name"], "name": body.url.strip()}
        with db.tx(conn):
            res = _create_source(conn, body.url.strip(), None, body.url.strip(), None, body.mode, body.incognito,
                                 body.person_name)
        return res

    # ---------------------------------------------------------------- sources
    @app.get("/api/sources")
    def list_sources(mode: str | None = None, status: str | None = None, q: str | None = None, limit: int = 100,
                     offset: int = 0, conn=Depends(get_conn)):
        q_text = q
        q = "SELECT * FROM sources WHERE status!='deleted'"
        params: list[Any] = []
        if q_text:
            q += " AND (name LIKE ? OR url LIKE ? OR id IN (SELECT ss.source_id FROM source_speakers ss JOIN persons p ON p.id=ss.person_id WHERE p.name LIKE ?))"
            params += [f"%{q_text}%", f"%{q_text}%", f"%{q_text}%"]
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
        s["settings"] = {k: config.Settings(conn)[k] for k in ("match_threshold", "confirm_threshold", "candidate_min_prob")}
        s["stills_info"] = json.loads(s["stills_info"]) if s.get("stills_info") else None
        s["review_pending"] = sum(1 for sp in s["speakers"] if sp.get("review_status") == "pending")
        s["transcript_info"] = json.loads(s["transcript_info"]) if s.get("transcript_info") else None
        s["transcript_job"] = db.row(conn, "SELECT id, status, stage, progress, error FROM jobs WHERE source_id=? AND type='transcribe' ORDER BY id DESC LIMIT 1", (sid,))
        s["transcript_lines"] = conn.execute("SELECT COUNT(*) FROM transcript_segments WHERE source_id=?", (sid,)).fetchone()[0]
        return s

    @app.get("/api/sources/{sid}/transcript")
    def get_transcript(sid: int, speaker_id: int | None = None, conn=Depends(get_conn)):
        q = "SELECT id, source_speaker_id AS speaker_id, start_s, end_s, text FROM transcript_segments WHERE source_id=?"
        params: list[Any] = [sid]
        if speaker_id is not None:
            q += " AND source_speaker_id=?"; params.append(speaker_id)
        src = db.row(conn, "SELECT transcript_status, transcript_language, transcript_info FROM sources WHERE id=?", (sid,))
        if not src:
            raise HTTPException(404)
        return {"status": src["transcript_status"], "language": src["transcript_language"],
                "info": json.loads(src["transcript_info"]) if src["transcript_info"] else None,
                "lines": db.rows(conn, q + " ORDER BY start_s", tuple(params))}

    @app.post("/api/sources/{sid}/transcribe")
    def transcribe_now(sid: int, force: bool = False, conn=Depends(get_conn)):
        """Queue Whisper for this recording (force = run it again even if words are cached)."""
        if not db.row(conn, "SELECT id FROM sources WHERE id=? AND status='done'", (sid,)):
            raise HTTPException(400, "the recording must be processed first")
        if force:
            try:
                transcribe.raw_path(sid).unlink(missing_ok=True)
            except OSError:
                pass
        with db.tx(conn):
            conn.execute("UPDATE sources SET transcript_status='queued' WHERE id=?", (sid,))
            jid = worker.enqueue(conn, sid, "transcribe", priority=-2, dedupe=True)
        return {"job_id": jid}

    @app.post("/api/transcribe/backfill")
    def transcribe_backfill(conn=Depends(get_conn)):
        """Queue every processed recording that has no transcript yet."""
        ids = [r["id"] for r in conn.execute("SELECT id FROM sources WHERE status='done' AND incognito=0 AND (transcript_status IS NULL OR transcript_status='failed')")]
        with db.tx(conn):
            for sid in ids:
                conn.execute("UPDATE sources SET transcript_status='queued' WHERE id=?", (sid,))
                worker.enqueue(conn, sid, "transcribe", priority=-3, dedupe=True)
        return {"queued": len(ids)}

    @app.get("/api/transcripts/search")
    def transcripts_search(q: str, limit: int = 50, conn=Depends(get_conn)):
        return {"hits": transcribe.search(conn, q, limit)}

    @app.get("/api/sources/{sid}/audio")
    def source_audio(sid: int, conn=Depends(get_conn)):
        s = db.row(conn, "SELECT wav_path, orig_path, original_filename FROM sources WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        p = s["wav_path"] if s["wav_path"] and Path(s["wav_path"]).exists() else s["orig_path"]
        if not p or not Path(p).exists():
            raise HTTPException(404, "audio file missing")
        return FileResponse(p)

    @app.get("/api/sources/{sid}/media")
    def source_media(sid: int, conn=Depends(get_conn)):
        """The original upload when it is a browser-playable video, else the wav (for the synced player)."""
        s = db.row(conn, "SELECT wav_path, orig_path FROM sources WHERE id=?", (sid,))
        if not s:
            raise HTTPException(404)
        orig = Path(s["orig_path"]) if s["orig_path"] else None
        if orig and orig.exists() and orig.suffix.lower() in VIDEO_EXTS:
            return FileResponse(str(orig))
        p = s["wav_path"] if s["wav_path"] and Path(s["wav_path"]).exists() else (str(orig) if orig and orig.exists() else None)
        if not p:
            raise HTTPException(404, "media file missing")
        return FileResponse(p)

    @app.patch("/api/sources/{sid}")
    def patch_source(sid: int, body: SourcePatch, conn=Depends(get_conn)):
        if not db.row(conn, "SELECT id FROM sources WHERE id=?", (sid,)):
            raise HTTPException(404)
        with db.tx(conn):
            if body.notes is not None:
                conn.execute("UPDATE sources SET notes=? WHERE id=?", (body.notes.strip() or None, sid))
            if body.name is not None and body.name.strip():
                conn.execute("UPDATE sources SET name=? WHERE id=?", (body.name.strip(), sid))
            if body.review is not None:
                conn.execute("UPDATE sources SET review=? WHERE id=?", (int(body.review), sid))
        return source_out(conn, db.row(conn, "SELECT * FROM sources WHERE id=?", (sid,)))

    @app.post("/api/sources/{sid}/review")
    def queue_source_for_review(sid: int, conn=Depends(get_conn)):
        """Put every voice of this recording that isn't a confirmed/named/enrolled person into the review queue."""
        if not db.row(conn, "SELECT id FROM sources WHERE id=?", (sid,)):
            raise HTTPException(404)
        with db.tx(conn):
            conn.execute("UPDATE sources SET review=1 WHERE id=?", (sid,))
            n = conn.execute("UPDATE source_speakers SET review_status='pending' WHERE source_id=? AND ignored=0 AND (assignment IS NULL OR assignment='auto')"
                             " AND (review_status IS NULL OR review_status='skipped')", (sid,)).rowcount
        return {"queued": n}

    @app.patch("/api/speakers/{speaker_id}")
    def patch_speaker(speaker_id: int, body: SpeakerPatch, conn=Depends(get_conn)):
        if not db.row(conn, "SELECT id FROM source_speakers WHERE id=?", (speaker_id,)):
            raise HTTPException(404)
        with db.tx(conn):
            if body.notes is not None:
                conn.execute("UPDATE source_speakers SET notes=? WHERE id=?", (body.notes.strip() or None, speaker_id))
            if body.review_status is not None:
                st = body.review_status.strip() or None
                if st not in (None, "pending", "done", "skipped"):
                    raise HTTPException(400, "review_status must be pending|done|skipped")
                conn.execute("UPDATE source_speakers SET review_status=? WHERE id=?", (st, speaker_id))
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,)))

    @app.get("/api/review")
    def review_queue(limit: int = 50, offset: int = 0, conn=Depends(get_conn)):
        rows = db.rows(conn,
            "SELECT ss.id FROM source_speakers ss JOIN sources s ON s.id=ss.source_id WHERE ss.review_status='pending' AND ss.ignored=0"
            " ORDER BY s.id DESC, ss.speech_seconds DESC LIMIT ? OFFSET ?", (limit, offset))
        total = conn.execute("SELECT COUNT(*) FROM source_speakers WHERE review_status='pending' AND ignored=0").fetchone()[0]
        items = []
        for r in rows:
            sp = speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (r["id"],)))
            src = db.row(conn, "SELECT id, name, url, notes, duration_s, orig_path FROM sources WHERE id=?", (sp["source_id"],))
            sp["source"] = {"id": src["id"], "name": src["name"], "notes": src["notes"], "youtube_id": youtube_id(src["url"]),
                            "media_kind": "youtube" if youtube_id(src["url"]) else ("video" if src["orig_path"] and Path(src["orig_path"]).suffix.lower() in VIDEO_EXTS else "audio")}
            items.append(sp)
        return {"items": items, "total": total, "candidate_min_prob": float(config.Settings(conn)["candidate_min_prob"])}

    @app.post("/api/sources/{sid}/stills")
    def make_stills(sid: int, conn=Depends(get_conn)):
        if not db.row(conn, "SELECT id FROM sources WHERE id=? AND status='done'", (sid,)):
            raise HTTPException(404, "recording not found or not processed yet")
        with db.tx(conn):
            jid = worker.enqueue(conn, sid, "stills", priority=2, dedupe=True)
        return {"job_id": jid}

    @app.post("/api/stills/backfill")
    def stills_backfill(conn=Depends(get_conn)):
        """Queue stills for every processed recording that has none yet (videos and YouTube only)."""
        ids = [r["id"] for r in conn.execute(
            "SELECT s.id FROM sources s WHERE s.status='done' AND s.incognito=0 AND (s.url LIKE '%youtu%' OR lower(s.orig_path) LIKE '%.mp4'"
            " OR lower(s.orig_path) LIKE '%.webm' OR lower(s.orig_path) LIKE '%.mov' OR lower(s.orig_path) LIKE '%.mkv' OR lower(s.orig_path) LIKE '%.m4v')"
            " AND EXISTS (SELECT 1 FROM source_speakers ss WHERE ss.source_id=s.id AND (ss.stills IS NULL OR ss.stills='[]'))")]
        with db.tx(conn):
            for sid in ids:
                worker.enqueue(conn, sid, "stills", priority=-2, dedupe=True)
        n_sp = conn.execute("SELECT COUNT(*) FROM source_speakers WHERE source_id IN (%s)" % (",".join(map(str, ids)) or "NULL")).fetchone()[0]
        per = int(config.Settings(conn)["stills_per_speaker"])
        return {"queued": len(ids), "estimated_mb": round(n_sp * per * stills.STILL_BYTES_ESTIMATE / 1e6, 1)}

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
            conn.execute("DELETE FROM transcript_segments WHERE source_id=?", (sid,))
            conn.execute("DELETE FROM source_speakers WHERE source_id=?", (sid,))
            conn.execute("UPDATE jobs SET status='cancelled' WHERE source_id=? AND status='queued'", (sid,))
            conn.execute("DELETE FROM sources WHERE id=?", (sid,))
            for p in pids:
                delete_empty_provisional(conn, p)
            db.bump_prints_version(conn)
        try:
            transcribe.raw_path(sid).unlink(missing_ok=True)
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
                (config.CLIP_DIR / c).unlink(missing_ok=True)
        stills.delete_stills(sid)
        return {"deleted": sid}

    # ---------------------------------------------------------------- jobs
    @app.get("/api/jobs")
    def list_jobs(status: str | None = None, limit: int = 50, conn=Depends(get_conn)):
        q = ("SELECT j.*, s.name AS source_name, s.mode AS source_mode, s.enroll_person_id FROM jobs j LEFT JOIN sources s ON s.id=j.source_id")
        params: list[Any] = []
        if status:
            q += " WHERE j.status=?"; params.append(status)
        q += " ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, j.id DESC LIMIT ?"
        params.append(limit)
        return {"jobs": db.rows(conn, q, tuple(params))}

    @app.post("/api/jobs/{jid}/cancel")
    def cancel_job(jid: int, conn=Depends(get_conn)):
        """Queued jobs are dropped immediately; a running job stops at its next progress tick (a few seconds)."""
        j = db.row(conn, "SELECT * FROM jobs WHERE id=?", (jid,))
        if not j:
            raise HTTPException(404)
        if j["status"] not in ("queued", "running"):
            return {"cancelled": False, "status": j["status"]}
        conn.execute("UPDATE jobs SET status='cancelled', stage='cancelling' WHERE id=?", (jid,))
        if j["status"] == "queued" and j["source_id"] is not None:
            conn.execute("UPDATE sources SET status='failed', error='cancelled' WHERE id=?", (j["source_id"],))
        return {"cancelled": True, "status": j["status"]}

    @app.delete("/api/jobs/{jid}")
    def delete_job(jid: int, conn=Depends(get_conn)):
        n = conn.execute("DELETE FROM jobs WHERE id=? AND status!='running'", (jid,)).rowcount
        if not n:
            raise HTTPException(409, "job is running or does not exist")
        return {"deleted": jid}

    @app.post("/api/jobs/clear")
    def clear_jobs(conn=Depends(get_conn)):
        n = conn.execute("DELETE FROM jobs WHERE status IN ('done','failed','cancelled')").rowcount
        return {"deleted": n}

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
                    queue_rematch(conn)
            if body.notes is not None:
                conn.execute("UPDATE persons SET notes=? WHERE id=?", (body.notes, pid))
            if body.alert is not None:
                conn.execute("UPDATE persons SET alert=? WHERE id=?", (int(body.alert), pid))
            if body.alert_sound is not None:
                snd = body.alert_sound.strip().lower()
                if snd and snd not in ALERT_SOUNDS:
                    raise HTTPException(400, f"alert_sound must be one of {ALERT_SOUNDS}")
                conn.execute("UPDATE persons SET alert_sound=? WHERE id=?", (snd or None, pid))
            if body.alert_message is not None:
                conn.execute("UPDATE persons SET alert_message=? WHERE id=?", (body.alert_message.strip() or None, pid))
            db.bump_prints_version(conn)
            mobile.mark_dirty(conn)
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

    @app.post("/api/persons/{pid}/accept")
    def accept_person(pid: int, conn=Depends(get_conn)):
        """Keep an unnamed voice as a known voice without naming it: it then raises alerts like a named person."""
        p = db.row(conn, "SELECT * FROM persons WHERE id=?", (pid,))
        if not p:
            raise HTTPException(404)
        with db.tx(conn):
            name = p["name"].replace("Unknown Voice", "Voice") if p["name"].startswith("Unknown Voice") else p["name"]
            conn.execute("UPDATE persons SET kind='named', name=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (name, pid))
            conn.execute("UPDATE source_speakers SET assignment='named' WHERE person_id=? AND assignment='auto'", (pid,))
            conn.execute("UPDATE matches SET status='confirmed' WHERE person_id=? AND status='auto'", (pid,))
            db.bump_prints_version(conn)
            mobile.mark_dirty(conn)
            queue_rematch(conn)
        return person_out(conn, db.row(conn, "SELECT * FROM persons WHERE id=?", (pid,)))

    # ---------------------------------------------------------------- packages (groups)
    def group_out(conn, g: dict) -> dict:
        g = dict(g)
        g["n"] = conn.execute("SELECT COUNT(*) FROM person_groups WHERE group_id=?", (g["id"],)).fetchone()[0]
        settings = config.Settings(conn)
        g["spoken_example"] = render_alert_message(g["alert_message"] or settings["alert_default_message"], "Nancy Pelosi")
        return g

    @app.get("/api/groups")
    def list_groups(conn=Depends(get_conn)):
        return {"groups": [group_out(conn, g) for g in db.rows(conn, "SELECT * FROM groups ORDER BY name")], "sounds": list(ALERT_SOUNDS)}

    @app.post("/api/groups")
    def create_group(body: GroupIn, conn=Depends(get_conn)):
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "name required")
        with db.tx(conn):
            r = db.row(conn, "SELECT id FROM groups WHERE name=? COLLATE NOCASE", (name,))
            gid = int(r["id"]) if r else int(conn.execute("INSERT INTO groups(name, description) VALUES(?, ?)", (name, body.description)).lastrowid)
        return group_out(conn, db.row(conn, "SELECT * FROM groups WHERE id=?", (gid,)))

    @app.get("/api/groups/{gid}")
    def get_group(gid: int, conn=Depends(get_conn)):
        g = db.row(conn, "SELECT * FROM groups WHERE id=?", (gid,))
        if not g:
            raise HTTPException(404)
        g = group_out(conn, g)
        g["members"] = [person_out(conn, p) for p in db.rows(conn,
            "SELECT p.* FROM person_groups pg JOIN persons p ON p.id=pg.person_id WHERE pg.group_id=? ORDER BY p.name COLLATE NOCASE", (gid,))]
        return g

    @app.patch("/api/groups/{gid}")
    def patch_group(gid: int, body: GroupPatch, conn=Depends(get_conn)):
        if not db.row(conn, "SELECT id FROM groups WHERE id=?", (gid,)):
            raise HTTPException(404)
        with db.tx(conn):
            if body.name is not None and body.name.strip():
                conn.execute("UPDATE groups SET name=? WHERE id=?", (body.name.strip(), gid))
            if body.description is not None:
                conn.execute("UPDATE groups SET description=? WHERE id=?", (body.description.strip() or None, gid))
            if body.alert is not None:
                conn.execute("UPDATE groups SET alert=? WHERE id=?", (int(body.alert), gid))
            if body.alert_sound is not None:
                snd = body.alert_sound.strip().lower()
                if snd and snd not in ALERT_SOUNDS:
                    raise HTTPException(400, f"alert_sound must be one of {ALERT_SOUNDS}")
                conn.execute("UPDATE groups SET alert_sound=? WHERE id=?", (snd or None, gid))
            if body.alert_message is not None:
                conn.execute("UPDATE groups SET alert_message=? WHERE id=?", (body.alert_message.strip() or None, gid))
            db.bump_prints_version(conn)
            mobile.mark_dirty(conn)
        return group_out(conn, db.row(conn, "SELECT * FROM groups WHERE id=?", (gid,)))

    @app.delete("/api/groups/{gid}")
    def delete_group(gid: int, conn=Depends(get_conn)):
        with db.tx(conn):
            n = conn.execute("DELETE FROM groups WHERE id=?", (gid,)).rowcount
            db.bump_prints_version(conn)
            mobile.mark_dirty(conn)
        if not n:
            raise HTTPException(404)
        return {"deleted": gid}

    @app.post("/api/groups/{gid}/members")
    def add_members(gid: int, body: MembersIn, conn=Depends(get_conn)):
        if not db.row(conn, "SELECT id FROM groups WHERE id=?", (gid,)):
            raise HTTPException(404)
        with db.tx(conn):
            for pid in body.person_ids:
                if db.row(conn, "SELECT 1 AS x FROM persons WHERE id=?", (pid,)):
                    conn.execute("INSERT OR IGNORE INTO person_groups(person_id, group_id) VALUES(?, ?)", (pid, gid))
            db.bump_prints_version(conn)
            mobile.mark_dirty(conn)
        return group_out(conn, db.row(conn, "SELECT * FROM groups WHERE id=?", (gid,)))

    @app.delete("/api/groups/{gid}/members/{pid}")
    def remove_member(gid: int, pid: int, conn=Depends(get_conn)):
        with db.tx(conn):
            conn.execute("DELETE FROM person_groups WHERE group_id=? AND person_id=?", (gid, pid))
            db.bump_prints_version(conn)
            mobile.mark_dirty(conn)
        return group_out(conn, db.row(conn, "SELECT * FROM groups WHERE id=?", (gid,)))

    @app.get("/api/groups/{gid}/export")
    def export_group(gid: int, conn=Depends(get_conn)):
        """A shareable package: the members' prints plus the package's alert settings."""
        g = db.row(conn, "SELECT * FROM groups WHERE id=?", (gid,))
        if not g:
            raise HTTPException(404)
        ids = [r["person_id"] for r in conn.execute("SELECT person_id FROM person_groups WHERE group_id=?", (gid,))]
        data = transfer.export_gallery(conn, include_provisional=True, person_ids=ids)
        data["package"] = {"name": g["name"], "description": g["description"], "alert": bool(g["alert"]),
                           "alert_sound": g["alert_sound"], "alert_message": g["alert_message"]}
        safe = re.sub(r"[^\w.-]+", "_", g["name"]).strip("_") or "package"
        return Response(transfer.dumps(data), media_type="application/json",
                        headers={"Content-Disposition": f"attachment; filename=voiceflock-package-{safe}.json"})

    @app.post("/api/import/phone_settings")
    def import_phone_settings(body: dict[str, Any], conn=Depends(get_conn)):
        try:
            return mobile.apply_phone_settings(conn, body)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, str(e))

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

    @app.get("/api/voices/merge_suggestions")
    def merge_suggestions(limit: int = 50, conn=Depends(get_conn)):
        """Pairs of voices that are probably the same person (incremental clustering can split one person
        across several unnamed voices when the mic or room changes)."""
        import numpy as np
        g = app.state.gcache.get(conn)
        settings = config.Settings(conn)
        thr = float(settings["unknown_cluster_threshold"])
        if len(g) == 0:
            return {"pairs": []}
        means: dict[int, list] = {}
        for i, pid in enumerate(g.person_ids):
            means.setdefault(int(pid), []).append(g.emb[i])
        pids = [p for p in means if p in g.persons]
        M = gallery.l2n(np.stack([np.mean(np.stack(means[p]), axis=0) for p in pids]))
        pairs = []
        for a in range(len(pids)):
            for b in range(a + 1, len(pids)):
                ka, kb = g.persons[pids[a]]["kind"], g.persons[pids[b]]["kind"]
                if ka == "named" and kb == "named":
                    continue  # two known people are never auto-suggested
                raw, norm, prob = g.score_pair(M[a], M[b])
                if prob >= thr:
                    pairs.append({"a": {"id": pids[a], "name": g.persons[pids[a]]["name"], "kind": ka},
                                  "b": {"id": pids[b], "name": g.persons[pids[b]]["name"], "kind": kb},
                                  "probability": prob, "raw_score": raw})
        pairs.sort(key=lambda x: -x["probability"])
        for pr in pairs[:limit]:
            for side in ("a", "b"):
                pr[side]["heard_in"] = person_sources(conn, pr[side]["id"], limit=3)
        return {"pairs": pairs[:limit], "threshold": thr}

    # ---------------------------------------------------------------- speakers / matches

    @app.post("/api/speakers/{speaker_id}/name")
    def name_speaker(speaker_id: int, body: NameIn, conn=Depends(get_conn)):
        """Name the voice behind a recording-speaker. If that speaker currently belongs to an unnamed
        (provisional) voice, the whole voice is named: its other recordings come along instead of being
        left behind as a rejected look-alike. A name that already exists merges the unnamed voice into it."""
        sp = db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,))
        if sp is None:
            raise HTTPException(404, "speaker not found")
        cur = db.row(conn, "SELECT * FROM persons WHERE id=?", (sp["person_id"],)) if sp["person_id"] else None
        with db.tx(conn):
            if body.person_id:
                pid = body.person_id
                if not db.row(conn, "SELECT id FROM persons WHERE id=?", (pid,)):
                    raise HTTPException(404, "person not found")
            else:
                name = (body.name or "").strip()
                if not name:
                    raise HTTPException(400, "name required")
                existing = db.row(conn, "SELECT id FROM persons WHERE name=? COLLATE NOCASE AND kind='named'", (name,))
                if cur and cur["kind"] == "provisional":
                    if existing and int(existing["id"]) != int(cur["id"]):
                        merge_persons(conn, int(cur["id"]), int(existing["id"]))
                        pid = int(existing["id"])
                    else:
                        conn.execute("UPDATE persons SET name=?, kind='named', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
                                     (name, int(cur["id"])))
                        pid = int(cur["id"])
                else:
                    pid = int(existing["id"]) if existing else find_or_create_person(conn, name)
            assign_speaker(conn, speaker_id, pid, "named", "named")
            queue_rematch(conn)
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,)))

    @app.post("/api/speakers/{speaker_id}/ignore")
    def ignore_speaker(speaker_id: int, conn=Depends(get_conn)):
        """Not a voice (noise, laughter, crosstalk): drop its prints and candidates and keep it out of matching,
        the review queue and calibration. The segments stay on the timeline, greyed out. Undo with /unignore."""
        with db.tx(conn):
            unassign_speaker(conn, speaker_id)
            conn.execute("DELETE FROM matches WHERE source_speaker_id=?", (speaker_id,))
            conn.execute("UPDATE source_speakers SET ignored=1, review_status=CASE WHEN review_status='pending' THEN 'done' ELSE review_status END"
                         " WHERE id=?", (speaker_id,))
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,)))

    @app.post("/api/speakers/{speaker_id}/unignore")
    def unignore_speaker(speaker_id: int, conn=Depends(get_conn)):
        sp = db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,))
        if sp is None:
            raise HTTPException(404, "speaker not found")
        with db.tx(conn):
            conn.execute("UPDATE source_speakers SET ignored=0 WHERE id=?", (speaker_id,))
            queue_rematch(conn, int(sp["source_id"]))
        return speaker_out(conn, db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,)))

    @app.get("/api/calibration")
    def calibration_status(conn=Depends(get_conn)):
        return selfcal.status(conn)

    @app.post("/api/calibration/decisions")
    def calibrate_from_decisions(conn=Depends(get_conn)):
        """Fit the probability model from every Confirm / Name / Enroll / Reject made so far, then re-score the library."""
        try:
            rep = selfcal.run(conn)
        except ValueError as e:
            raise HTTPException(400, str(e))
        with db.tx(conn):
            queue_rematch(conn)
        return rep

    @app.post("/api/calibration/reset")
    def calibration_reset(conn=Depends(get_conn)):
        selfcal.reset(conn)
        with db.tx(conn):
            queue_rematch(conn)
        return selfcal.status(conn)

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
            queue_rematch(conn)
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

    @app.post("/api/matches/{mid}/reopen")
    def reopen_match(mid: int, conn=Depends(get_conn)):
        """Undo a rejection: the candidate is back on the table (nothing is assigned)."""
        m = db.row(conn, "SELECT * FROM matches WHERE id=?", (mid,))
        if not m:
            raise HTTPException(404)
        with db.tx(conn):
            conn.execute("UPDATE matches SET status='open', decided_at=NULL WHERE id=? AND status='rejected'", (mid,))
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

    # ---------------------------------------------------------------- maintenance jobs
    @app.post("/api/rematch")
    def api_rematch(body: RematchIn | None = None, conn=Depends(get_conn)):
        """Re-score every processed recording against the current gallery (fast, no ML)."""
        with db.tx(conn):
            jid = queue_rematch(conn, body.source_id if body else None)
        return {"job_id": jid}

    @app.post("/api/selftest")
    def api_selftest(conn=Depends(get_conn)):
        with db.tx(conn):
            jid = worker.enqueue(conn, None, "selftest", priority=10, dedupe=True)
        return {"job_id": jid}

    @app.get("/api/selftest")
    def api_selftest_latest(conn=Depends(get_conn)):
        j = db.row(conn, "SELECT * FROM jobs WHERE type='selftest' ORDER BY id DESC LIMIT 1")
        if j and j.get("result"):
            try:
                j["result"] = json.loads(j["result"])
            except ValueError:
                pass
        return {"job": j}

    # ---------------------------------------------------------------- feeds
    def feed_out(conn, f: dict) -> dict:
        f = dict(f)
        f["recent"] = db.rows(conn, "SELECT fi.*, s.status AS source_status FROM feed_items fi LEFT JOIN sources s ON s.id=fi.source_id"
                                    " WHERE fi.feed_id=? ORDER BY fi.source_id IS NULL, fi.id DESC LIMIT 5", (f["id"],))
        return f

    @app.get("/api/feeds")
    def list_feeds(conn=Depends(get_conn)):
        return {"feeds": [feed_out(conn, f) for f in db.rows(conn, "SELECT * FROM feeds ORDER BY id DESC")]}

    @app.post("/api/feeds")
    def create_feed(body: FeedIn, conn=Depends(get_conn)):
        if not re.match(r"^https?://", body.url.strip()):
            raise HTTPException(400, "url must start with http(s)://")
        if body.mode not in ("bulk", "match"):
            raise HTTPException(400, "feed mode must be bulk or match")
        if db.row(conn, "SELECT id FROM feeds WHERE url=?", (body.url.strip(),)):
            raise HTTPException(409, "feed already added")
        with db.tx(conn):
            fid = feeds.add_feed(conn, body.url, body.mode, body.incognito, body.interval_min, body.backfill)
        return feed_out(conn, db.row(conn, "SELECT * FROM feeds WHERE id=?", (fid,)))

    @app.patch("/api/feeds/{fid}")
    def patch_feed(fid: int, body: FeedPatch, conn=Depends(get_conn)):
        f = db.row(conn, "SELECT * FROM feeds WHERE id=?", (fid,))
        if not f:
            raise HTTPException(404)
        with db.tx(conn):
            for k, v in body.model_dump(exclude_none=True).items():
                conn.execute(f"UPDATE feeds SET {k}=? WHERE id=?", (int(v) if isinstance(v, bool) else v, fid))
        return feed_out(conn, db.row(conn, "SELECT * FROM feeds WHERE id=?", (fid,)))

    @app.delete("/api/feeds/{fid}")
    def delete_feed(fid: int, conn=Depends(get_conn)):
        with db.tx(conn):
            n = conn.execute("DELETE FROM feeds WHERE id=?", (fid,)).rowcount
        if not n:
            raise HTTPException(404)
        return {"deleted": fid}

    @app.post("/api/feeds/{fid}/poll")
    def poll_feed_now(fid: int, conn=Depends(get_conn)):
        f = db.row(conn, "SELECT * FROM feeds WHERE id=?", (fid,))
        if not f:
            raise HTTPException(404)
        r = feeds.poll_feed(conn, f)
        if r["error"]:
            raise HTTPException(502, f"feed fetch failed: {r['error']}")
        return {"queued": r["new"], "feed": feed_out(conn, db.row(conn, "SELECT * FROM feeds WHERE id=?", (fid,)))}

    # ---------------------------------------------------------------- export / import
    @app.get("/api/export/gallery.json")
    def export_gallery(include_unnamed: bool = True, conn=Depends(get_conn)):
        data = transfer.export_gallery(conn, include_provisional=include_unnamed)
        return Response(transfer.dumps(data), media_type="application/json",
                        headers={"Content-Disposition": "attachment; filename=voiceflock-gallery.json"})

    @app.get("/api/export/matches.csv")
    def export_matches(conn=Depends(get_conn)):
        return PlainTextResponse(transfer.matches_csv(conn), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=voiceflock-matches.csv"})

    @app.post("/api/import/gallery")
    async def import_gallery(file: UploadFile = File(...), conn=Depends(get_conn)):
        try:
            data = json.loads(await file.read())
            stats = transfer.import_gallery(conn, data)
            mobile.mark_dirty(conn)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, f"bad gallery file: {e}")
        with db.tx(conn):
            queue_rematch(conn)
        return stats

    # ---------------------------------------------------------------- youtube bulk import
    @app.post("/api/youtube/list")
    def yt_list(body: YtListIn, conn=Depends(get_conn)):
        try:
            title, videos = youtube.list_videos(body.url, limit=body.limit)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, f"could not list videos: {str(e)[:300]}")
        videos = youtube.filter_videos(videos, body.title_regex, body.min_minutes, body.max_minutes, body.date_from, body.date_to)
        youtube.mark_imported(conn, videos)
        return {"title": title, "videos": youtube.video_dicts(videos),
                "total_minutes": round(sum((v.duration_s or 0) for v in videos) / 60.0)}

    @app.post("/api/youtube/import")
    def yt_import(body: YtImportIn, conn=Depends(get_conn)):
        vids = [youtube.Video(v.get("id", ""), v["url"], v.get("title") or v["url"], v.get("duration_s"), v.get("date"), v.get("view_count"))
                for v in body.videos if v.get("url")]
        return youtube.import_videos(conn, vids, body.mode, body.incognito, body.prefix)

    # ---------------------------------------------------------------- phone sync
    @app.get("/api/mobile/gallery.json")
    def mobile_gallery(max_prints: int = 4, download: bool = False, conn=Depends(get_conn)):
        """Compact library for the phone: people with a few prints each, packages, alert settings, calibration."""
        data = mobile.build(conn, max_prints)
        if download:
            return Response(json.dumps(data), media_type="application/json",
                            headers={"Content-Disposition": f"attachment; filename=voiceflock-library-{__import__('time').strftime('%Y%m%d')}.json"})
        return data

    @app.get("/api/mobile/export_status")
    def mobile_export_status(conn=Depends(get_conn)):
        return {"path": config.Settings(conn)["library_export_path"],
                "dirty": (db.row(conn, "SELECT value FROM meta WHERE key='library_dirty'") or {}).get("value") == "1",
                "exported_at": (db.row(conn, "SELECT value FROM meta WHERE key='library_exported_at'") or {}).get("value")}

    @app.post("/api/mobile/export_now")
    def mobile_export_now(conn=Depends(get_conn)):
        try:
            out = mobile.export_if_dirty(conn, force=True)
        except OSError as e:
            raise HTTPException(400, f"could not write: {e}")
        if not out:
            raise HTTPException(400, "set 'Auto-export phone library to' in Settings first")
        return {"written": str(out)}

    @app.api_route("/api/mobile/model.onnx", methods=["GET", "HEAD"])
    def mobile_model(conn=Depends(get_conn)):
        p = onnx_export.model_path()
        if not p.exists():
            with db.tx(conn):
                jid = worker.enqueue(conn, None, "export_model", priority=5, dedupe=True)
            return JSONResponse({"detail": "model export queued; try again in a minute", "job_id": jid}, status_code=202)
        return FileResponse(str(p), media_type="application/octet-stream", filename=onnx_export.MODEL_FILE)

    @app.post("/api/mobile/export_model")
    def mobile_export_model(conn=Depends(get_conn)):
        with db.tx(conn):
            jid = worker.enqueue(conn, None, "export_model", priority=5, dedupe=True)
        return {"job_id": jid, "meta": onnx_export.meta()}

    # ---------------------------------------------------------------- overview (charts)
    @app.get("/api/overview")
    def overview(conn=Depends(get_conn)):
        import numpy as np
        persons = db.rows(conn,
            "SELECT p.id, p.name, p.kind, (SELECT COUNT(DISTINCT source_id) FROM source_speakers WHERE person_id=p.id) AS n_sources,"
            " (SELECT COALESCE(SUM(speech_seconds),0) FROM source_speakers WHERE person_id=p.id) AS speech_seconds FROM persons p")
        means, keep = [], []
        for p in persons:
            m = person_mean(conn, p["id"])
            if m is not None:
                means.append(m)
                keep.append(p)
        voices = []
        if len(means) >= 1:
            X = np.stack(means)
            Xc = X - X.mean(axis=0)
            if len(means) >= 3:
                _, _, vt = np.linalg.svd(Xc, full_matrices=False)
                xy = Xc @ vt[:2].T
            else:
                xy = np.zeros((len(means), 2))
                xy[:, 0] = np.arange(len(means))
            span = max(1e-6, float(np.abs(xy).max()))
            xy = xy / span
            for p, m, (x, y) in zip(keep, means, xy):
                voices.append({**p, "x": round(float(x), 4), "y": round(float(y), 4), "glyph": live.glyph(m)})
        top = sorted(voices, key=lambda v: -v["speech_seconds"])[:12]
        activity = db.rows(conn,
            "SELECT substr(created_at,1,10) AS day, COUNT(*) AS recordings, COALESCE(SUM(duration_s),0)/3600.0 AS hours"
            " FROM sources WHERE status!='deleted' GROUP BY day ORDER BY day")
        pairs = db.rows(conn,
            "SELECT a.person_id AS a_id, pa.name AS a_name, b.person_id AS b_id, pb.name AS b_name, COUNT(DISTINCT a.source_id) AS n"
            " FROM source_speakers a JOIN source_speakers b ON a.source_id=b.source_id AND a.person_id<b.person_id"
            " JOIN persons pa ON pa.id=a.person_id JOIN persons pb ON pb.id=b.person_id"
            " GROUP BY a.person_id, b.person_id ORDER BY n DESC, a.person_id LIMIT 15")
        st = db.row(conn, "SELECT COUNT(*) AS recordings, COALESCE(SUM(duration_s),0)/3600.0 AS hours FROM sources WHERE status='done'")
        return {
            "stats": {"recordings": st["recordings"], "hours": round(st["hours"], 1),
                      "known": sum(1 for p in persons if p["kind"] == "named"),
                      "unknown": sum(1 for p in persons if p["kind"] == "provisional"),
                      "prints": conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0],
                      "speech_hours": round(sum(p["speech_seconds"] for p in persons) / 3600.0, 1),
                      "open_alerts": conn.execute("SELECT COUNT(*) FROM matches m JOIN persons p ON p.id=m.person_id"
                                                  " WHERE m.status='auto' AND p.kind='named'").fetchone()[0]},
            "voices": voices, "top_voices": top, "activity": activity, "pairs": pairs,
        }

    # ---------------------------------------------------------------- live listening
    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        """Binary frames: int16 PCM, 16 kHz mono. Server replies with JSON status/frame messages."""
        import asyncio

        import numpy as np
        await ws.accept()
        conn = db.connect()
        try:
            settings = config.Settings(conn)
            await ws.send_json({"type": "status", "text": "loading embedding model…"})
            from .embed import Embedder
            try:
                emb = await asyncio.to_thread(Embedder.get, settings["embedding_model"], settings["device"], config.MODEL_DIR)
            except Exception as e:  # noqa: BLE001
                await ws.send_json({"type": "error", "text": f"could not load embedding model: {e}"})
                return
            matcher = live.LiveMatcher.from_settings(emb, lambda: app.state.gcache.get(conn, settings), settings)
            g = app.state.gcache.get(conn, settings)
            await ws.send_json({"type": "status", "text": f"listening · {len(g.persons)} voices in gallery",
                                "threshold": float(settings["match_threshold"]), "gallery": len(g.persons)})
            received = 0
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                data = msg.get("bytes")
                if not data:
                    continue
                samples = np.frombuffer(data, dtype=np.int16)
                received += len(samples)
                if received - len(samples) < 16000 <= received:
                    await ws.send_json({"type": "status", "text": f"audio flowing · {len(g.persons)} voices in gallery · device {getattr(emb, 'device', '?')}"})
                frames = await asyncio.to_thread(matcher.feed, samples)
                for f in frames:
                    await ws.send_json(f.as_dict())
        except WebSocketDisconnect:
            pass
        finally:
            conn.close()

    # ---------------------------------------------------------------- admin: update / restart / wipe
    @app.get("/api/admin/update")
    def update_check():
        return admin.check_update()

    @app.post("/api/admin/update")
    def update_apply():
        admin.apply_update(restart=admin.supervised())
        return {"started": True, "will_restart": admin.supervised()}

    @app.get("/api/admin/update/status")
    def update_progress():
        return admin.update_status()

    @app.post("/api/admin/restart")
    def restart():
        if not admin.supervised():
            raise HTTPException(400, "the server was not started by VoiceFlock.bat / voiceflock.sh, so it cannot restart itself; restart it by hand")
        admin.restart_server()
        return {"restarting": True}

    @app.post("/api/admin/wipe")
    def wipe(body: WipeIn, conn=Depends(get_conn)):
        if body.confirm != "WIPE":
            raise HTTPException(400, "type WIPE to confirm")
        counts = admin.wipe_library(conn, keep_settings=body.keep_settings)
        app.state.gcache.invalidate()
        return {"wiped": counts}

    # ---------------------------------------------------------------- static
    config.ensure_dirs()
    app.mount("/clips", StaticFiles(directory=str(config.CLIP_DIR)), name="clips")
    app.mount("/stills", StaticFiles(directory=str(stills.still_dir())), name="stills")
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
