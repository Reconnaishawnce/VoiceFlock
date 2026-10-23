"""Calibrate probabilities from the user's own decisions.

Every Confirm, Name, Enroll and Reject is a labelled verification trial: this speaker is (or is not) that
person. Scoring each labelled speaker against the library, with prints from its own recording excluded,
gives same-voice and different-voice score distributions on *this user's* microphones and rooms, and a
logistic fit of those replaces the default calibration constants. No labelled folder needed.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import numpy as np

from . import config, db, gallery, metrics

HUMAN_ASSIGNMENTS = ("confirmed", "named", "enrolled")
MIN_TARGET = 8
MIN_TARGET_PERSONS = 2
MIN_NONTARGET = 30


def _labelled_speakers(conn: sqlite3.Connection) -> list[dict]:
    return db.rows(conn,
        "SELECT ss.id, ss.source_id, ss.person_id, ss.embedding FROM source_speakers ss"
        " WHERE ss.embedding IS NOT NULL AND ss.person_id IS NOT NULL AND ss.ignored=0"
        f" AND ss.assignment IN ({','.join('?' * len(HUMAN_ASSIGNMENTS))})", HUMAN_ASSIGNMENTS)


def _rejected(conn: sqlite3.Connection) -> list[dict]:
    return db.rows(conn,
        "SELECT ss.id, ss.source_id, ss.embedding, m.person_id FROM matches m JOIN source_speakers ss ON ss.id=m.source_speaker_id"
        " WHERE m.status='rejected' AND ss.embedding IS NOT NULL AND ss.ignored=0")


def available(conn: sqlite3.Connection) -> dict:
    """How much labelled material there is, without scoring anything (cheap, for the UI)."""
    sp = _labelled_speakers(conn)
    persons = {int(r["person_id"]) for r in sp}
    named = conn.execute("SELECT COUNT(*) FROM persons WHERE kind='named'").fetchone()[0]
    rej = conn.execute("SELECT COUNT(*) FROM matches m JOIN source_speakers ss ON ss.id=m.source_speaker_id"
                       " WHERE m.status='rejected' AND ss.embedding IS NOT NULL AND ss.ignored=0").fetchone()[0]
    return {"labelled_speakers": len(sp), "labelled_persons": len(persons), "rejections": int(rej), "named_persons": int(named),
            "enough": len(sp) >= MIN_TARGET and len(persons) >= MIN_TARGET_PERSONS}


def collect_scores(conn: sqlite3.Connection, settings: config.Settings, g: gallery.Gallery) -> dict:
    """Target and non-target scores, per scoring mode ('raw' always; 'snorm' when the cohort allows it)."""
    modes = ["raw"] + (["snorm"] if g.score_mode == "snorm" else [])
    tgt: dict[str, list[float]] = {m: [] for m in modes}
    non: dict[str, list[float]] = {m: [] for m in modes}
    tgt_persons: set[int] = set()
    seen_non: set[tuple[int, int]] = set()
    named = {pid for pid, info in g.persons.items() if info["kind"] == "named"}
    for r in _labelled_speakers(conn):
        emb = db.blob_to_emb(r["embedding"])
        pid = int(r["person_id"])
        scores = g.scores_by_person(emb, exclude_source=int(r["source_id"]))
        own = scores.get(pid)
        if own is None:                       # the person only exists through this recording: nothing to verify against
            continue
        tgt_persons.add(pid)
        tgt["raw"].append(own[0])
        if "snorm" in modes:
            tgt["snorm"].append(own[1])
        for other, (raw, norm) in scores.items():
            if other == pid or other not in named:
                continue
            seen_non.add((int(r["id"]), other))
            non["raw"].append(raw)
            if "snorm" in modes:
                non["snorm"].append(norm)
    for r in _rejected(conn):                 # explicit "not them" decisions are the hardest negatives of all
        key = (int(r["id"]), int(r["person_id"]))
        if key in seen_non:
            continue
        scores = g.scores_by_person(db.blob_to_emb(r["embedding"]), exclude_source=int(r["source_id"]))
        sc = scores.get(int(r["person_id"]))
        if sc is None:
            continue
        seen_non.add(key)
        non["raw"].append(sc[0])
        if "snorm" in modes:
            non["snorm"].append(sc[1])
    return {"modes": modes, "target": {m: np.asarray(v) for m, v in tgt.items()}, "nontarget": {m: np.asarray(v) for m, v in non.items()},
            "n_target_persons": len(tgt_persons)}


def run(conn: sqlite3.Connection) -> dict:
    settings = config.Settings(conn)
    g = gallery.load_gallery(conn, settings)
    data = collect_scores(conn, settings, g)
    n_t, n_n = len(data["target"]["raw"]), len(data["nontarget"]["raw"])
    if n_t < MIN_TARGET or data["n_target_persons"] < MIN_TARGET_PERSONS or n_n < MIN_NONTARGET:
        raise ValueError(
            f"Not enough decisions yet: {n_t} same-voice trials from {data['n_target_persons']} people and {n_n} different-voice trials. "
            f"Need at least {MIN_TARGET} confirmed/named speakers across {MIN_TARGET_PERSONS} people and {MIN_NONTARGET} different-voice trials "
            "(every confirmed speaker counts against each other known person, so a few more known people fixes this quickly).")
    report: dict = {"n_target": n_t, "n_nontarget": n_n, "n_target_persons": data["n_target_persons"], "modes": {}}
    with db.tx(conn):
        for mode in data["modes"]:
            t, n = data["target"][mode], data["nontarget"][mode]
            cal = gallery.Calibration.fit(t, n, mode)
            if not np.isfinite(cal.a) or cal.a <= 0:
                raise ValueError(f"calibration fit degenerate for {mode} scores (a={cal.a:.3g}); decisions may be contradictory")
            e, thr = metrics.eer(t, n)
            m1, thr1 = metrics.miss_at_fa(t, n, 0.01)
            report["modes"][mode] = {
                "a": cal.a, "b": cal.b, "eer": float(e), "miss_at_1pct_fa": float(m1),
                "prob_at_eer": float(cal.prob(thr)), "prob_at_1pct_fa": float(cal.prob(thr1)) if np.isfinite(thr1) else None,
                "target_median": float(np.median(t)), "nontarget_median": float(np.median(n)),
            }
            conn.execute("UPDATE calibrations SET active=0 WHERE mode=?", (mode,))
            conn.execute("INSERT INTO calibrations(mode, a, b, n_target, n_nontarget, eer, active) VALUES(?,?,?,?,?,?,1)",
                         (mode, cal.a, cal.b, n_t, n_n, float(e)))
        db.bump_prints_version(conn)
    active = report["modes"][g.score_mode if g.score_mode in report["modes"] else "raw"]
    # alerts want few false alarms: suggest the 1%-FA point when it exists, else the equal-error point, kept sane
    sug = active["prob_at_1pct_fa"] if active["prob_at_1pct_fa"] is not None else active["prob_at_eer"]
    report["active_mode"] = g.score_mode
    report["suggested_match_threshold"] = round(float(min(0.97, max(0.5, sug))), 2)
    report["fitted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return report


def status(conn: sqlite3.Connection) -> dict:
    rows = db.rows(conn, "SELECT mode, a, b, n_target, n_nontarget, eer, created_at FROM calibrations WHERE active=1 ORDER BY mode")
    return {"fitted": {r["mode"]: r for r in rows}, "available": available(conn)}


def reset(conn: sqlite3.Connection) -> None:
    with db.tx(conn):
        conn.execute("UPDATE calibrations SET active=0")
        db.bump_prints_version(conn)
