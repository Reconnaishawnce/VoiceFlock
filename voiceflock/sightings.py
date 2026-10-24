"""One timeline of "this voice was heard": laptop Live mode, the phone, and new recordings. Phones push
their detections here and pull the laptop's, so both devices show the same list."""
from __future__ import annotations

import sqlite3
import time

from . import db

KINDS = ("live", "phone", "recording")


def add(conn: sqlite3.Connection, items: list[dict], default_device: str | None = None) -> int:
    n = 0
    for it in items:
        kind = str(it.get("kind") or "live")
        if kind not in KINDS or not it.get("name"):
            continue
        at_ms = int(it.get("at_ms") or time.time() * 1000)
        cur = conn.execute(
            "INSERT OR IGNORE INTO sightings(at_ms, person_id, name, probability, flagged, kind, source_id, device, session_name) VALUES(?,?,?,?,?,?,?,?,?)",
            (at_ms, it.get("person_id"), str(it["name"])[:200], float(it.get("probability") or 0), int(bool(it.get("flagged"))), kind,
             it.get("source_id"), (it.get("device") or default_device or None), (str(it.get("session") or "")[:200] or None)))
        n += cur.rowcount
    return n


def list_(conn: sqlite3.Connection, since_ms: int | None = None, limit: int = 200, person_id: int | None = None,
          kinds: tuple[str, ...] | None = None, exclude_device: str | None = None) -> list[dict]:
    q = ("SELECT s.*, src.name AS source_name FROM sightings s LEFT JOIN sources src ON src.id=s.source_id WHERE 1=1")
    params: list = []
    if since_ms is not None:
        q += " AND s.at_ms>?"; params.append(int(since_ms))
    if person_id is not None:
        q += " AND s.person_id=?"; params.append(int(person_id))
    if kinds:
        q += f" AND s.kind IN ({','.join('?' * len(kinds))})"; params.extend(kinds)
    if exclude_device:
        q += " AND (s.device IS NULL OR s.device!=?)"; params.append(exclude_device)
    q += " ORDER BY s.at_ms " + ("ASC" if since_ms is not None else "DESC") + " LIMIT ?"
    params.append(int(limit))
    return db.rows(conn, q, tuple(params))


def tailscale_info() -> dict:
    """Is this laptop on a tailnet? -> {dns_name, ips, https_url?} via the local `tailscale` CLI (cached a minute)."""
    import json
    import shutil
    import subprocess
    import sys

    now = time.time()
    if _TS_CACHE and now - _TS_CACHE[0] < 60:
        return _TS_CACHE[1]
    exe = shutil.which("tailscale")
    if exe is None and sys.platform == "win32":
        for p in (r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe"):
            import os
            if os.path.exists(p):
                exe = p
                break
    info: dict = {"installed": exe is not None}
    if exe:
        try:
            out = subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=4)
            if out.returncode == 0:
                st = json.loads(out.stdout)
                me = st.get("Self") or {}
                info["dns_name"] = (me.get("DNSName") or "").rstrip(".")
                info["ips"] = me.get("TailscaleIPs") or []
                info["online"] = bool(me.get("Online", True)) and st.get("BackendState") == "Running"
                srv = subprocess.run([exe, "serve", "status", "--json"], capture_output=True, text=True, timeout=4)
                if srv.returncode == 0 and srv.stdout.strip() and info["dns_name"]:
                    try:
                        sj = json.loads(srv.stdout)
                        if sj.get("Web") or sj.get("TCP"):
                            info["https_url"] = f"https://{info['dns_name']}"
                    except json.JSONDecodeError:
                        pass
            else:
                info["error"] = (out.stderr or out.stdout).strip()[:200]
        except Exception as e:  # noqa: BLE001
            info["error"] = str(e)[:200]
    _TS_CACHE[:] = [now, info]
    return info


_TS_CACHE: list = []
