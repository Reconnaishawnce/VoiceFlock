"""Speaker-aware search: "every time Pelosi spoke in the last 30 days, over 5 minutes", plus people,
recordings, transcript lines and live sightings that match a phrase. One query, grouped results."""
from __future__ import annotations

import sqlite3
from typing import Any

from . import db

DATE_EXPR = "COALESCE(s.recorded_on, substr(s.created_at, 1, 10))"


def _people_matching(conn: sqlite3.Connection, q: str, limit: int = 20) -> list[dict]:
    if not q:
        return []
    like = f"%{q}%"
    rows = db.rows(conn, "SELECT p.id, p.name, p.kind, p.alert, p.role, p.watch_reason,"
                         " (SELECT COUNT(DISTINCT source_id) FROM source_speakers WHERE person_id=p.id) AS n_sources,"
                         " (SELECT COALESCE(SUM(speech_seconds),0) FROM source_speakers WHERE person_id=p.id) AS speech_seconds"
                         " FROM persons p WHERE p.name LIKE ? OR p.role LIKE ? OR p.notes LIKE ? OR p.watch_reason LIKE ?"
                         " ORDER BY (p.name LIKE ?) DESC, n_sources DESC LIMIT ?", (like, like, like, like, like, limit))
    return rows


def search(conn: sqlite3.Connection, q: str = "", person_id: int | None = None, date_from: str | None = None, date_to: str | None = None,
           min_seconds: float = 0.0, limit: int = 100) -> dict[str, Any]:
    q = (q or "").strip()
    people = _people_matching(conn, q) if person_id is None else db.rows(conn, "SELECT id, name, kind, alert, role FROM persons WHERE id=?", (person_id,))
    # whose appearances to list: the explicit person, else every person whose name matches the phrase
    pids = [person_id] if person_id is not None else [p["id"] for p in people if q and q.lower() in (p["name"] or "").lower()]

    where, params = ["s.status!='deleted'"], []
    if date_from:
        where.append(f"{DATE_EXPR} >= ?"); params.append(date_from)
    if date_to:
        where.append(f"{DATE_EXPR} <= ?"); params.append(date_to)
    if min_seconds:
        where.append("ss.speech_seconds >= ?"); params.append(float(min_seconds))

    appearances: list[dict] = []
    if pids:
        ph = ",".join("?" * len(pids))
        appearances = db.rows(conn,
            f"SELECT ss.id AS speaker_id, ss.source_id, ss.person_id, ss.speech_seconds, ss.assignment, ss.clip_path, s.name AS source_name, s.url,"
            f" {DATE_EXPR} AS day, s.created_at, p.name AS person_name,"
            " (SELECT start_s FROM segments g WHERE g.source_speaker_id=ss.id ORDER BY (end_s-start_s) DESC LIMIT 1) AS watch_at,"
            " (SELECT COUNT(*) FROM transcript_segments t WHERE t.source_speaker_id=ss.id) AS n_lines"
            f" FROM source_speakers ss JOIN sources s ON s.id=ss.source_id JOIN persons p ON p.id=ss.person_id"
            f" WHERE ss.person_id IN ({ph}) AND ss.ignored=0 AND {' AND '.join(where)} ORDER BY day DESC, s.id DESC, ss.speech_seconds DESC LIMIT ?",
            tuple(pids) + tuple(params) + (limit,))
        for a in appearances:
            a["clip_url"] = f"/clips/{a['clip_path']}" if a.get("clip_path") else None
            a.pop("clip_path", None)

    # recordings whose name/url/notes match
    recordings = []
    if q:
        like = f"%{q}%"
        rw, rp = ["s.status!='deleted'", "(s.name LIKE ? OR s.url LIKE ? OR s.notes LIKE ?)"], [like, like, like]
        if date_from:
            rw.append(f"{DATE_EXPR} >= ?"); rp.append(date_from)
        if date_to:
            rw.append(f"{DATE_EXPR} <= ?"); rp.append(date_to)
        recordings = db.rows(conn, f"SELECT s.id, s.name, s.duration_s, s.n_speakers, {DATE_EXPR} AS day, s.url FROM sources s"
                                   f" WHERE {' AND '.join(rw)} ORDER BY s.id DESC LIMIT 30", tuple(rp))

    # transcript lines: phrase search, optionally restricted to the person(s) and dates
    lines: list[dict] = []
    if q:
        try:
            sql = ("SELECT t.id, t.source_id, t.source_speaker_id, t.start_s, t.end_s, snippet(transcript_fts, 0, '[', ']', '…', 16) AS snippet,"
                   f" s.name AS source_name, {DATE_EXPR} AS day, ss.label, p.name AS person_name, p.id AS person_id"
                   " FROM transcript_fts f JOIN transcript_segments t ON t.id=f.rowid JOIN sources s ON s.id=t.source_id"
                   " LEFT JOIN source_speakers ss ON ss.id=t.source_speaker_id LEFT JOIN persons p ON p.id=ss.person_id"
                   " WHERE transcript_fts MATCH ? AND s.status!='deleted'")
            lp: list[Any] = ['"' + q.replace('"', '""') + '"']
            if person_id is not None:
                sql += " AND ss.person_id=?"; lp.append(person_id)
            if date_from:
                sql += f" AND {DATE_EXPR} >= ?"; lp.append(date_from)
            if date_to:
                sql += f" AND {DATE_EXPR} <= ?"; lp.append(date_to)
            sql += " ORDER BY rank LIMIT 60"
            lines = db.rows(conn, sql, tuple(lp))
        except sqlite3.OperationalError:
            lines = []

    # live / phone sightings of those people
    sightings: list[dict] = []
    if pids:
        ph = ",".join("?" * len(pids))
        sp: list[Any] = list(pids)
        sql = f"SELECT s.*, src.name AS source_name FROM sightings s LEFT JOIN sources src ON src.id=s.source_id WHERE s.person_id IN ({ph}) AND s.kind IN ('live','phone')"
        if date_from:
            sql += " AND date(s.at_ms/1000, 'unixepoch') >= ?"; sp.append(date_from)
        if date_to:
            sql += " AND date(s.at_ms/1000, 'unixepoch') <= ?"; sp.append(date_to)
        sightings = db.rows(conn, sql + " ORDER BY s.at_ms DESC LIMIT 100", tuple(sp))

    total_speech = sum(float(a["speech_seconds"] or 0) for a in appearances)
    return {"q": q, "person_ids": pids, "people": people, "appearances": appearances, "recordings": recordings, "lines": lines, "sightings": sightings,
            "summary": {"n_appearances": len(appearances), "n_recordings": len({a["source_id"] for a in appearances}), "speech_seconds": total_speech,
                        "n_lines": len(lines), "n_sightings": len(sightings)}}
