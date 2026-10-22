"""Export / import of the voice gallery (people, prints, calibration, cohort) as JSON,
and a CSV of all rank-1 matches."""
from __future__ import annotations

import base64
import csv
import io
import json
import sqlite3

import numpy as np

from . import db, gallery


def _b64(e: np.ndarray) -> str:
    return base64.b64encode(np.asarray(e, dtype=np.float32).tobytes()).decode()


def _unb64(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=np.float32).copy()


def export_gallery(conn: sqlite3.Connection, include_provisional: bool = True) -> dict:
    persons = db.rows(conn, "SELECT * FROM persons" + ("" if include_provisional else " WHERE kind='named'"))
    out = {"format": "voiceflock-gallery", "version": 1, "persons": []}
    for p in persons:
        prints = db.rows(conn, "SELECT embedding, is_mean, speech_seconds, quality, origin, source_id, created_at FROM prints"
                               " WHERE person_id=?", (p["id"],))
        srcs = db.rows(conn, "SELECT DISTINCT s.name FROM source_speakers ss JOIN sources s ON s.id=ss.source_id"
                             " WHERE ss.person_id=?", (p["id"],))
        out["persons"].append({
            "name": p["name"], "kind": p["kind"], "notes": p["notes"], "created_at": p["created_at"],
            "heard_in": [s["name"] for s in srcs],
            "prints": [{"e": _b64(db.blob_to_emb(pr["embedding"])), "is_mean": pr["is_mean"], "speech_seconds": pr["speech_seconds"],
                        "quality": pr["quality"], "origin": pr["origin"], "created_at": pr["created_at"]} for pr in prints],
        })
    out["cohort"] = [{"label": r["label"], "e": _b64(db.blob_to_emb(r["embedding"]))} for r in conn.execute("SELECT label, embedding FROM cohort")]
    out["calibrations"] = db.rows(conn, "SELECT mode, a, b, n_target, n_nontarget, eer FROM calibrations WHERE active=1")
    return out


def import_gallery(conn: sqlite3.Connection, data: dict, merge_by_name: bool = True) -> dict:
    if data.get("format") != "voiceflock-gallery":
        raise ValueError("not a VoiceFlock gallery export")
    stats = {"persons_created": 0, "persons_merged": 0, "prints": 0, "cohort": 0, "calibrations": 0}
    with db.tx(conn):
        for p in data.get("persons", []):
            pid = None
            if merge_by_name and p.get("kind") == "named":
                r = db.row(conn, "SELECT id FROM persons WHERE name=? COLLATE NOCASE AND kind='named'", (p["name"],))
                if r:
                    pid = int(r["id"])
                    stats["persons_merged"] += 1
            if pid is None:
                pid = int(conn.execute("INSERT INTO persons(name, kind, notes) VALUES(?,?,?)",
                                       (p["name"], p.get("kind", "named"), p.get("notes"))).lastrowid)
                stats["persons_created"] += 1
            for pr in p.get("prints", []):
                conn.execute("INSERT INTO prints(person_id, embedding, is_mean, speech_seconds, quality, origin) VALUES(?,?,?,?,?,?)",
                             (pid, db.emb_to_blob(gallery.l2n(_unb64(pr["e"]))), int(pr.get("is_mean", 0)),
                              pr.get("speech_seconds"), pr.get("quality"), "import"))
                stats["prints"] += 1
        for c in data.get("cohort", []):
            conn.execute("INSERT INTO cohort(label, embedding) VALUES(?, ?)", (c.get("label"), db.emb_to_blob(_unb64(c["e"]))))
            stats["cohort"] += 1
        for cal in data.get("calibrations", []):
            conn.execute("UPDATE calibrations SET active=0 WHERE mode=?", (cal["mode"],))
            conn.execute("INSERT INTO calibrations(mode, a, b, n_target, n_nontarget, eer, active) VALUES(?,?,?,?,?,?,1)",
                         (cal["mode"], cal["a"], cal["b"], cal.get("n_target"), cal.get("n_nontarget"), cal.get("eer")))
            stats["calibrations"] += 1
        db.bump_prints_version(conn)
    return stats


def matches_csv(conn: sqlite3.Connection) -> str:
    rows = db.rows(conn,
        "SELECT s.id AS source_id, s.name AS source, s.url, s.created_at AS uploaded, ss.label AS speaker, ss.first_start_s,"
        " ss.speech_seconds, ss.quality_score, ss.snr_db, p.name AS candidate, p.kind AS candidate_kind, m.probability, m.margin,"
        " m.raw_score, m.norm_score, m.score_mode, m.status, ss.assignment"
        " FROM matches m JOIN source_speakers ss ON ss.id=m.source_speaker_id JOIN sources s ON s.id=ss.source_id"
        " JOIN persons p ON p.id=m.person_id WHERE m.rank=1 ORDER BY s.id, ss.first_start_s")
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return buf.getvalue()


def dumps(data: dict) -> str:
    return json.dumps(data, indent=1)
