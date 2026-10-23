"""The phone library: a compact JSON of people, prints, packages and alert settings.
Built here so the API, the auto-export writer and the CLI produce the same file."""
from __future__ import annotations

import base64
import json
import sqlite3
import time
from pathlib import Path

from . import config, db, gallery, onnx_export

ALERT_SOUNDS = ("siren", "horn", "vault", "radar", "klaxon", "chime", "none")


def render_alert_message(template: str, name: str) -> str:
    return (template or "").replace("[NAME]", name).replace("{name}", name).strip()


def groups_of(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """person_id -> list of group dicts (ordered by group id)."""
    out: dict[int, list[dict]] = {}
    for r in db.rows(conn, "SELECT pg.person_id, g.* FROM person_groups pg JOIN groups g ON g.id=pg.group_id ORDER BY g.id"):
        out.setdefault(int(r["person_id"]), []).append(r)
    return out


def effective_alert(person: dict, groups: list[dict], settings: config.Settings) -> dict:
    """Resolve what happens when this voice is heard: person settings win, then the first package
    that sets a value, then the defaults. Alert is on if the person or any package says so."""
    alert = bool(person.get("alert"))
    via = "person" if alert else None
    for g in groups:
        if g.get("alert"):
            alert = True
            via = via or f"package:{g['name']}"
    sound = person.get("alert_sound") or next((g["alert_sound"] for g in groups if g.get("alert_sound")), None) or settings["alert_default_sound"]
    tpl = person.get("alert_message") or next((g["alert_message"] for g in groups if g.get("alert_message")), None) or settings["alert_default_message"]
    return {"alert": alert, "via": via, "sound": sound, "message": tpl, "spoken": render_alert_message(tpl, person["name"])}


def build(conn: sqlite3.Connection, max_prints: int = 4) -> dict:
    settings = config.Settings(conn)
    cal = gallery.load_calibrations(conn, settings)["raw"]
    gmap = groups_of(conn)
    people = []
    for p in db.rows(conn, "SELECT id, name, kind, alert, alert_sound, alert_message FROM persons"):
        rows = conn.execute("SELECT embedding FROM prints WHERE person_id=? ORDER BY is_mean DESC, COALESCE(quality,0) DESC LIMIT ?",
                            (p["id"], max_prints)).fetchall()
        if not rows:
            continue
        eff = effective_alert(p, gmap.get(p["id"], []), settings)
        prints = [base64.b64encode(db.emb_to_blob(gallery.l2n(db.blob_to_emb(r["embedding"])))).decode() for r in rows]
        people.append({"id": p["id"], "name": p["name"], "kind": p["kind"], "alert": bool(p["alert"]),
                       "alert_sound": p["alert_sound"], "alert_message": p["alert_message"],
                       "group_ids": [g["id"] for g in gmap.get(p["id"], [])],
                       "effective_alert": eff["alert"], "spoken": eff["spoken"], "prints": prints})
    groups = [{"id": g["id"], "name": g["name"], "description": g["description"], "alert": bool(g["alert"]),
               "alert_sound": g["alert_sound"], "alert_message": g["alert_message"],
               "n": conn.execute("SELECT COUNT(*) FROM person_groups WHERE group_id=?", (g["id"],)).fetchone()[0]}
              for g in db.rows(conn, "SELECT * FROM groups ORDER BY name")]
    return {"format": "voiceflock-mobile", "version": 2, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "dim": config.EMBED_DIM, "sample_rate": 16000, "match_threshold": float(settings["match_threshold"]),
            "calibration": {"a": cal.a, "b": cal.b}, "window_s": float(settings["live_window_s"]), "hop_s": float(settings["live_hop_s"]),
            "min_level_db": float(settings["live_min_level_db"]), "model": onnx_export.MODEL_FILE, "model_meta": onnx_export.meta(),
            "alert_defaults": {"message": settings["alert_default_message"], "sound": settings["alert_default_sound"]},
            "sounds": list(ALERT_SOUNDS), "groups": groups, "people": people}


# ---------------------------------------------------------------------------
# Auto-export to a synced folder (Dropbox / Google Drive / OneDrive on the laptop)
# ---------------------------------------------------------------------------

def mark_dirty(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES('library_dirty', '1') ON CONFLICT(key) DO UPDATE SET value='1'")


def export_if_dirty(conn: sqlite3.Connection, force: bool = False) -> Path | None:
    settings = config.Settings(conn)
    path = (settings["library_export_path"] or "").strip()
    if not path:
        return None
    dirty = db.row(conn, "SELECT value FROM meta WHERE key='library_dirty'")
    if not force and not (dirty and dirty["value"] == "1"):
        return None
    out = Path(path).expanduser()
    if out.is_dir() or not out.suffix:
        out = out / "voiceflock-library.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(build(conn)))
    tmp.replace(out)
    conn.execute("INSERT INTO meta(key, value) VALUES('library_dirty', '0') ON CONFLICT(key) DO UPDATE SET value='0'")
    conn.execute("INSERT INTO meta(key, value) VALUES('library_exported_at', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
    return out


# ---------------------------------------------------------------------------
# Edits made on the phone, pasted back into the laptop
# ---------------------------------------------------------------------------

def apply_phone_settings(conn: sqlite3.Connection, data: dict) -> dict:
    if data.get("format") != "voiceflock-phone-settings":
        raise ValueError("not a VoiceFlock phone-settings export")
    n_p = n_g = 0
    with db.tx(conn):
        for pid, v in (data.get("persons") or {}).items():
            if not conn.execute("SELECT 1 FROM persons WHERE id=?", (int(pid),)).fetchone():
                continue
            if "alert" in v:
                conn.execute("UPDATE persons SET alert=? WHERE id=?", (int(bool(v["alert"])), int(pid)))
            if "sound" in v:
                conn.execute("UPDATE persons SET alert_sound=? WHERE id=?", ((v["sound"] or None), int(pid)))
            if "message" in v:
                conn.execute("UPDATE persons SET alert_message=? WHERE id=?", ((v["message"] or None), int(pid)))
            n_p += 1
        for gid, v in (data.get("groups") or {}).items():
            if not conn.execute("SELECT 1 FROM groups WHERE id=?", (int(gid),)).fetchone():
                continue
            if "alert" in v:
                conn.execute("UPDATE groups SET alert=? WHERE id=?", (int(bool(v["alert"])), int(gid)))
            if "sound" in v:
                conn.execute("UPDATE groups SET alert_sound=? WHERE id=?", ((v["sound"] or None), int(gid)))
            if "message" in v:
                conn.execute("UPDATE groups SET alert_message=? WHERE id=?", ((v["message"] or None), int(gid)))
            n_g += 1
        n_e = _apply_enrollments(conn, data.get("enrollments") or [])
        mark_dirty(conn)
        db.bump_prints_version(conn)
    return {"persons": n_p, "groups": n_g, "enrollments": n_e}


def _apply_enrollments(conn: sqlite3.Connection, items: list) -> int:
    """Voiceprints captured on the phone (Identify -> add / enroll). Each item: prints (base64 float32),
    plus person_id (existing person) or name (matched case-insensitively, else created)."""
    import numpy as np
    from . import gallery as gal
    n = 0
    for e in items:
        prints = []
        for b in e.get("prints") or []:
            try:
                v = np.frombuffer(base64.b64decode(b), dtype=np.float32)
            except Exception:
                continue
            if v.size >= 64 and np.isfinite(v).all():
                prints.append(v.copy())
        if not prints:
            continue
        pid = e.get("person_id")
        row = None
        if pid and int(pid) > 0:
            row = conn.execute("SELECT id FROM persons WHERE id=?", (int(pid),)).fetchone()
        name = (e.get("name") or "").strip()
        if row is None and name:
            row = conn.execute("SELECT id FROM persons WHERE lower(name)=lower(?)", (name,)).fetchone()
        if row is None:
            if not name:
                continue
            pid = conn.execute("INSERT INTO persons(name, kind, alert) VALUES(?, 'named', ?)",
                               (name, int(bool(e.get("alert", False))))).lastrowid
        else:
            pid = row[0]
        gal.add_prints(conn, int(pid), None, None, None, prints, "phone")
        n += 1
    return n
