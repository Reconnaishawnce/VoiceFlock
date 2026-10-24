"""Quality control for bulk imports: what went wrong in a batch (or a time window), which voices look like
noise, which prints should not be in the library, and one-call clean-ups."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import config, db, sources as srcmod

MIN_DURATION_S = 45.0
LOW_SNR_DB = 10.0
CLIP_FRAC = 0.02
LITTLE_SPEECH_FRAC = 0.15
CROWDED = 12


def _scope(batch_id: int | None, days: int | None) -> tuple[str, list[Any]]:
    w, p = ["s.status!='deleted'"], []
    if batch_id:
        w.append("s.batch_id=?"); p.append(int(batch_id))
    elif days:
        w.append("s.created_at >= datetime('now', ?)"); p.append(f"-{int(days)} days")
    return " AND ".join(w), p


def noise_speaker_sql(settings: config.Settings) -> tuple[str, list[Any]]:
    """Speakers that look like noise: short, noisy or low-quality, judged by the processing stats."""
    q = float(settings["min_print_quality"])
    return ("(ss.quality_score < ? OR ss.speech_seconds < 6 OR ss.snr_db < 8 OR ss.clip_fraction > ?)", [q, CLIP_FRAC])


def report(conn: sqlite3.Connection, batch_id: int | None = None, days: int | None = None) -> dict:
    settings = config.Settings(conn)
    where, params = _scope(batch_id, days)
    srcs = db.rows(conn, f"SELECT s.id, s.name, s.status, s.error, s.duration_s, s.n_speakers, s.sha256, s.url, s.created_at, s.batch_id,"
                         " (SELECT COALESCE(SUM(speech_seconds),0) FROM source_speakers x WHERE x.source_id=s.id AND x.ignored=0) AS speech,"
                         " (SELECT MAX(snr_db) FROM source_speakers x WHERE x.source_id=s.id) AS max_snr,"
                         " (SELECT MAX(clip_fraction) FROM source_speakers x WHERE x.source_id=s.id) AS max_clip,"
                         " (SELECT COUNT(*) FROM source_speakers x WHERE x.source_id=s.id) AS n_spk"
                         f" FROM sources s WHERE {where} ORDER BY s.id DESC", tuple(params))
    out: dict[str, list] = {"failed": [], "empty": [], "short": [], "little_speech": [], "low_snr": [], "clipping": [], "crowded": [], "duplicates": []}
    for s in srcs:
        item = {"id": s["id"], "name": s["name"], "duration_s": s["duration_s"]}
        if s["status"] == "failed":
            out["failed"].append({**item, "error": (s["error"] or "")[:200]}); continue
        if s["status"] != "done":
            continue
        if s["n_spk"] == 0:
            out["empty"].append(item); continue
        if s["duration_s"] and s["duration_s"] < MIN_DURATION_S:
            out["short"].append(item)
        if s["duration_s"] and s["speech"] / max(1.0, s["duration_s"]) < LITTLE_SPEECH_FRAC:
            out["little_speech"].append({**item, "speech_seconds": s["speech"]})
        if s["max_snr"] is not None and s["max_snr"] < LOW_SNR_DB:
            out["low_snr"].append({**item, "snr_db": round(float(s["max_snr"]), 1)})
        if s["max_clip"] is not None and s["max_clip"] > CLIP_FRAC:
            out["clipping"].append({**item, "clip_fraction": round(float(s["max_clip"]), 3)})
        if (s["n_spk"] or 0) > CROWDED:
            out["crowded"].append({**item, "n_speakers": s["n_spk"]})
        dup = None
        if s["sha256"]:
            dup = db.row(conn, "SELECT id, name FROM sources WHERE sha256=? AND id<? AND status!='deleted' ORDER BY id LIMIT 1", (s["sha256"], s["id"]))
        if dup is None and s["url"]:
            dup = db.row(conn, "SELECT id, name FROM sources WHERE url=? AND id<? AND status!='deleted' ORDER BY id LIMIT 1", (s["url"], s["id"]))
        if dup is None:
            dup = db.row(conn, "SELECT id, name FROM sources WHERE name=? AND id<? AND status='done' AND duration_s IS NOT NULL AND ABS(duration_s-?)<2 ORDER BY id LIMIT 1",
                         (s["name"], s["id"], float(s["duration_s"] or 0)))
        if dup:
            out["duplicates"].append({**item, "duplicate_of": dup["id"], "duplicate_name": dup["name"]})
    nz, nzp = noise_speaker_sql(settings)
    noise = db.rows(conn, f"SELECT ss.id, ss.source_id, ss.label, ss.speech_seconds, ss.snr_db, ss.quality_score, ss.clip_fraction, ss.person_id, ss.clip_path,"
                          " s.name AS source_name, p.name AS person_name, p.kind AS person_kind,"
                          " (SELECT COUNT(*) FROM prints pr WHERE pr.source_speaker_id=ss.id) AS n_prints,"
                          " (SELECT COUNT(DISTINCT source_id) FROM source_speakers y WHERE y.person_id=ss.person_id) AS person_sources"
                          f" FROM source_speakers ss JOIN sources s ON s.id=ss.source_id LEFT JOIN persons p ON p.id=ss.person_id"
                          f" WHERE {where} AND ss.ignored=0 AND {nz} AND (ss.assignment IS NULL OR ss.assignment='auto')"
                          " ORDER BY ss.quality_score ASC, ss.speech_seconds ASC LIMIT 300", tuple(params) + tuple(nzp))
    for n in noise:
        n["clip_url"] = f"/clips/{n['clip_path']}" if n.get("clip_path") else None
        n.pop("clip_path", None)
        n["reasons"] = [r for r, ok in (("low quality", (n["quality_score"] or 0) < float(settings["min_print_quality"])), ("under 6 s", (n["speech_seconds"] or 0) < 6),
                                        ("low SNR", (n["snr_db"] if n["snr_db"] is not None else 99) < 8), ("clipping", (n["clip_fraction"] or 0) > CLIP_FRAC)) if ok]
    low_prints = conn.execute(f"SELECT COUNT(*) FROM prints pr JOIN source_speakers ss ON ss.id=pr.source_speaker_id JOIN sources s ON s.id=ss.source_id"
                              f" WHERE {where} AND pr.origin='auto-cluster' AND COALESCE(pr.quality, ss.quality_score, 100) < ?", tuple(params) + (float(settings["min_print_quality"]),)).fetchone()[0]
    return {"scope": {"batch_id": batch_id, "days": days, "n_sources": len(srcs), "done": sum(1 for s in srcs if s["status"] == "done"),
                      "queued": sum(1 for s in srcs if s["status"] in ("queued", "processing")),
                      "speech_hours": round(sum(float(s["speech"] or 0) for s in srcs) / 3600, 2)},
            "recordings": out, "noise_voices": noise, "noise_prints": sum(int(n["n_prints"]) for n in noise), "low_quality_prints": int(low_prints),
            "thresholds": {"min_print_quality": settings["min_print_quality"], "low_snr_db": LOW_SNR_DB, "min_duration_s": MIN_DURATION_S, "little_speech_frac": LITTLE_SPEECH_FRAC}}


def ignore_speaker(conn: sqlite3.Connection, speaker_id: int) -> None:
    sp = db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (speaker_id,))
    if not sp:
        return
    conn.execute("DELETE FROM prints WHERE source_speaker_id=?", (speaker_id,))
    conn.execute("DELETE FROM matches WHERE source_speaker_id=?", (speaker_id,))
    conn.execute("UPDATE source_speakers SET person_id=NULL, assignment=NULL, ignored=1,"
                 " review_status=CASE WHEN review_status='pending' THEN 'done' ELSE review_status END WHERE id=?", (speaker_id,))
    srcmod.delete_empty_provisional(conn, sp["person_id"])


def apply(conn: sqlite3.Connection, actions: list[str], batch_id: int | None = None, days: int | None = None) -> dict:
    rep = report(conn, batch_id, days)
    done: dict[str, int] = {}
    settings = config.Settings(conn)
    if "ignore_noise" in actions:
        with db.tx(conn):
            for n in rep["noise_voices"]:
                ignore_speaker(conn, int(n["id"]))
            db.bump_prints_version(conn)
        done["ignored_voices"] = len(rep["noise_voices"])
    if "prune_low_prints" in actions:
        where, params = _scope(batch_id, days)
        rows = db.rows(conn, f"SELECT pr.id, pr.person_id FROM prints pr JOIN source_speakers ss ON ss.id=pr.source_speaker_id JOIN sources s ON s.id=ss.source_id"
                             f" WHERE {where} AND pr.origin='auto-cluster' AND COALESCE(pr.quality, ss.quality_score, 100) < ?", tuple(params) + (float(settings["min_print_quality"]),))
        with db.tx(conn):
            for r in rows:
                conn.execute("DELETE FROM prints WHERE id=?", (r["id"],))
            for pid in {r["person_id"] for r in rows}:
                srcmod.delete_empty_provisional(conn, pid)
            db.bump_prints_version(conn)
        done["pruned_prints"] = len(rows)
    for key, action in (("failed", "delete_failed"), ("empty", "delete_empty"), ("duplicates", "delete_duplicates"), ("short", "delete_short")):
        if action in actions:
            n = 0
            for it in rep["recordings"][key]:
                n += int(srcmod.delete_source(conn, int(it["id"])))
            done[action] = n
    return done
