"""Per-person dossier: photo, role, why you're watching, links, a unified timeline, quotes, and a printable export."""
from __future__ import annotations

import base64
import html
import io
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, db


def photo_dir() -> Path:
    d = config.DATA_DIR / "photos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def photo_paths(pid: int) -> tuple[Path, Path]:
    return photo_dir() / f"{pid}.jpg", photo_dir() / f"{pid}.thumb.jpg"


def save_photo(pid: int, data: bytes) -> dict:
    """Store an uploaded or extracted image as a 640px JPEG plus a 96px square thumbnail."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    big, thumb = photo_paths(pid)
    img.thumbnail((640, 640))
    img.save(big, "JPEG", quality=86)
    sq = ImageOps.fit(img, (96, 96), centering=(0.5, 0.35))
    sq.save(thumb, "JPEG", quality=80)
    return {"width": img.width, "height": img.height}


def delete_photo(pid: int) -> None:
    for p in photo_paths(pid):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def photo_urls(pid: int) -> tuple[str | None, str | None]:
    big, thumb = photo_paths(pid)
    if not big.exists():
        return None, None
    v = int(big.stat().st_mtime)
    return f"/photos/{pid}.jpg?v={v}", (f"/photos/{pid}.thumb.jpg?v={v}" if thumb.exists() else f"/photos/{pid}.jpg?v={v}")


def thumb_b64(pid: int) -> str | None:
    _, thumb = photo_paths(pid)
    if not thumb.exists():
        return None
    return base64.b64encode(thumb.read_bytes()).decode()


def links_of(p: dict) -> list[str]:
    try:
        v = json.loads(p.get("links") or "[]")
        return [str(x) for x in v if str(x).strip()]
    except Exception:
        return [x.strip() for x in str(p.get("links") or "").splitlines() if x.strip()]


def timeline(conn: sqlite3.Connection, pid: int, limit: int = 200) -> list[dict]:
    """Recordings (when they were added), laptop Live hits and phone hits, newest first."""
    out: list[dict] = []
    for r in db.rows(conn, "SELECT s.id, s.name, s.created_at, ss.speech_seconds, ss.id AS speaker_id, ss.assignment,"
                           " (SELECT start_s FROM segments g WHERE g.source_speaker_id=ss.id ORDER BY (end_s-start_s) DESC LIMIT 1) AS watch_at"
                           " FROM source_speakers ss JOIN sources s ON s.id=ss.source_id WHERE ss.person_id=? AND s.status!='deleted' ORDER BY s.id DESC LIMIT ?", (pid, limit)):
        try:
            at_ms = int(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            at_ms = 0
        out.append({"at_ms": at_ms, "kind": "recording", "label": r["name"], "source_id": r["id"], "speech_seconds": r["speech_seconds"],
                    "watch_at": r["watch_at"], "assignment": r["assignment"]})
    for r in db.rows(conn, "SELECT s.*, src.name AS source_name FROM sightings s LEFT JOIN sources src ON src.id=s.source_id"
                           " WHERE s.person_id=? AND s.kind IN ('live','phone') ORDER BY s.at_ms DESC LIMIT ?", (pid, limit)):
        out.append({"at_ms": int(r["at_ms"]), "kind": r["kind"], "label": "heard live on the laptop" if r["kind"] == "live" else "heard by the phone",
                    "probability": r["probability"], "flagged": bool(r["flagged"]), "device": r["device"]})
    out.sort(key=lambda x: -x["at_ms"])
    return out[:limit]


def quotes(conn: sqlite3.Connection, pid: int, limit: int = 8) -> list[dict]:
    """Longest transcript lines spoken by this person across recordings, with where to play them."""
    rows = db.rows(conn, "SELECT t.text, t.start_s, t.end_s, t.source_id, s.name AS source_name FROM transcript_segments t"
                         " JOIN source_speakers ss ON ss.id=t.source_speaker_id JOIN sources s ON s.id=t.source_id"
                         " WHERE ss.person_id=? AND length(t.text) > 40 ORDER BY length(t.text) DESC LIMIT ?", (pid, limit * 4))
    seen: set[str] = set()
    out = []
    for r in rows:
        key = r["text"].strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= limit:
            break
    return out


def summary(conn: sqlite3.Connection, pid: int) -> dict:
    last = conn.execute("SELECT MAX(at_ms) FROM sightings WHERE person_id=?", (pid,)).fetchone()[0]
    last_rec = conn.execute("SELECT MAX(s.created_at) FROM source_speakers ss JOIN sources s ON s.id=ss.source_id WHERE ss.person_id=?", (pid,)).fetchone()[0]
    try:
        last_rec_ms = int(datetime.fromisoformat(last_rec.replace("Z", "+00:00")).timestamp() * 1000) if last_rec else None
    except Exception:
        last_rec_ms = None
    week = int((datetime.now(timezone.utc).timestamp() - 7 * 86400) * 1000)
    return {
        "last_heard_ms": max([x for x in (last, last_rec_ms) if x] or [None]) if (last or last_rec_ms) else None,
        "sightings_7d": conn.execute("SELECT COUNT(*) FROM sightings WHERE person_id=? AND at_ms>?", (pid, week)).fetchone()[0],
        "sightings_total": conn.execute("SELECT COUNT(*) FROM sightings WHERE person_id=?", (pid,)).fetchone()[0],
    }


def render_html(p: dict) -> str:
    """Self-contained printable dossier (photo inline), for sharing or printing to PDF."""
    e = html.escape
    big, _ = photo_paths(int(p["id"]))
    photo = f'<img src="data:image/jpeg;base64,{base64.b64encode(big.read_bytes()).decode()}" alt="">' if big.exists() else ""
    def when(ms): return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else ""
    rows = "".join(f"<tr><td>{when(t['at_ms'])}</td><td>{e(t['kind'])}</td><td>{e(t.get('label') or '')}{(' · ' + str(int(t['probability'] * 100)) + '%') if t.get('probability') else ''}</td></tr>" for t in p.get("timeline", []))
    q = "".join(f"<li>“{e(x['text'])}” <span class=m>— {e(x['source_name'])}, {int(x['start_s'] // 60)}:{int(x['start_s'] % 60):02d}</span></li>" for x in p.get("quotes", []))
    links = "".join(f'<li><a href="{e(u)}">{e(u)}</a></li>' for u in p.get("links", []))
    recs = "".join(f"<li>{e(h['name'])} · {int((h.get('speech_seconds') or 0) // 60)} min of speech</li>" for h in p.get("heard_in", []))
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{e(p['name'])} · VoiceFlock dossier</title>
<style>body{{font:15px/1.5 Georgia,serif;max-width:800px;margin:40px auto;padding:0 20px;color:#222}}h1{{font-size:32px;margin:0}}.head{{display:flex;gap:24px;align-items:flex-start}}
img{{width:180px;border-radius:8px}}.m{{color:#777;font-size:13px}}table{{border-collapse:collapse;width:100%;font-size:14px}}td{{padding:4px 8px;border-bottom:1px solid #eee;vertical-align:top}}h2{{font-size:16px;letter-spacing:.08em;text-transform:uppercase;color:#777;margin-top:28px}}
blockquote{{margin:8px 0}}@media print{{body{{margin:0}}}}</style></head><body>
<div class="head">{photo}<div><h1>{e(p['name'])}</h1><div class="m">{e(p.get('role') or '')}</div>
<p>{e(p.get('watch_reason') or '')}</p><p class="m">{p.get('n_sources', 0)} recordings · {int((p.get('speech_seconds') or 0) // 60)} min of speech · {p.get('n_prints', 0)} voiceprints · {'alerting' if (p.get('effective') or {}).get('alert') else 'not alerting'} · generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by VoiceFlock</p></div></div>
{('<h2>Notes</h2><p>' + e(p['notes']) + '</p>') if p.get('notes') else ''}
{('<h2>Links</h2><ul>' + links + '</ul>') if links else ''}
{('<h2>In their words</h2><ul>' + q + '</ul>') if q else ''}
<h2>Timeline</h2><table>{rows or '<tr><td class=m>nothing yet</td></tr>'}</table>
{('<h2>Recordings</h2><ul>' + recs + '</ul>') if recs else ''}
</body></html>"""


def render_markdown(p: dict) -> str:
    def when(ms): return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else ""
    out = [f"# {p['name']}", ""]
    if p.get("role"): out.append(f"*{p['role']}*  ")
    if p.get("watch_reason"): out += ["", p["watch_reason"]]
    out += ["", f"{p.get('n_sources', 0)} recordings · {int((p.get('speech_seconds') or 0) // 60)} min of speech · {p.get('n_prints', 0)} voiceprints", ""]
    if p.get("notes"): out += ["## Notes", "", p["notes"], ""]
    if p.get("links"): out += ["## Links", ""] + [f"- {u}" for u in p["links"]] + [""]
    if p.get("quotes"): out += ["## In their words", ""] + [f"- “{x['text']}” — {x['source_name']}, {int(x['start_s'] // 60)}:{int(x['start_s'] % 60):02d}" for x in p["quotes"]] + [""]
    out += ["## Timeline", ""] + [f"- {when(t['at_ms'])} · {t['kind']} · {t.get('label') or ''}" + (f" · {int(t['probability'] * 100)}%" if t.get("probability") else "") for t in p.get("timeline", [])]
    if p.get("heard_in"): out += ["", "## Recordings", ""] + [f"- {h['name']} · {int((h.get('speech_seconds') or 0) // 60)} min" for h in p["heard_in"]]
    return "\n".join(out) + "\n"
