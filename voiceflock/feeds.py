"""RSS / Atom feed watching: podcasts, YouTube channels, anything with enclosures or links.

The worker polls enabled feeds; new items become URL sources processed like any upload."""
from __future__ import annotations

import logging
import sqlite3
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import db, worker

log = logging.getLogger(__name__)
NS = {"atom": "http://www.w3.org/2005/Atom", "media": "http://search.yahoo.com/mrss/", "yt": "http://www.youtube.com/xml/schemas/2015"}


@dataclass
class FeedItem:
    guid: str
    title: str
    url: str | None
    published: str | None


def parse_feed(xml_bytes: bytes) -> tuple[str | None, list[FeedItem]]:
    """Return (feed title, items newest-first as given by the feed). Supports RSS 2.0 and Atom."""
    root = ET.fromstring(xml_bytes)
    items: list[FeedItem] = []
    title = None
    if root.tag.endswith("rss") or root.find("channel") is not None:
        ch = root.find("channel")
        title = (ch.findtext("title") or "").strip() if ch is not None else None
        for it in (ch.findall("item") if ch is not None else []):
            enc = it.find("enclosure")
            url = enc.get("url") if enc is not None else None
            if not url:
                mc = it.find("media:content", NS)
                url = mc.get("url") if mc is not None else None
            if not url:
                url = (it.findtext("link") or "").strip() or None
            guid = (it.findtext("guid") or url or it.findtext("title") or "").strip()
            if guid:
                items.append(FeedItem(guid, (it.findtext("title") or "").strip(), url, (it.findtext("pubDate") or "").strip() or None))
    elif root.tag.endswith("feed"):
        title = (root.findtext("atom:title", default="", namespaces=NS) or "").strip() or None
        for e in root.findall("atom:entry", NS):
            url = None
            for link in e.findall("atom:link", NS):
                rel = link.get("rel", "alternate")
                if rel == "enclosure":
                    url = link.get("href")
                    break
                if rel == "alternate" and url is None:
                    url = link.get("href")
            guid = (e.findtext("atom:id", default="", namespaces=NS) or url or "").strip()
            if guid:
                items.append(FeedItem(guid, (e.findtext("atom:title", default="", namespaces=NS) or "").strip(), url,
                                      (e.findtext("atom:published", default="", namespaces=NS)
                                       or e.findtext("atom:updated", default="", namespaces=NS) or "").strip() or None))
    else:
        raise ValueError("not an RSS or Atom feed")
    return title, items


def fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "VoiceFlock/0.1 (+feed watcher)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (user-supplied feed URL by design)
        return r.read(50 * 1024 * 1024)


def add_feed(conn: sqlite3.Connection, url: str, mode: str = "bulk", incognito: bool = False,
             interval_min: int = 60, backfill: int = 3) -> int:
    cur = conn.execute("INSERT INTO feeds(url, mode, incognito, interval_min, backfill) VALUES(?,?,?,?,?)",
                       (url.strip(), mode, int(incognito), int(interval_min), int(backfill)))
    return int(cur.lastrowid)


def poll_feed(conn: sqlite3.Connection, feed: dict) -> dict:
    """Fetch one feed and queue new items. Returns {new: n, error: str|None}."""
    fid = int(feed["id"])
    try:
        title, items = parse_feed(fetch(feed["url"]))
    except Exception as e:  # noqa: BLE001
        conn.execute("UPDATE feeds SET last_checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), last_error=? WHERE id=?",
                     (f"{type(e).__name__}: {e}"[:500], fid))
        return {"new": 0, "error": str(e)}
    first_poll = conn.execute("SELECT COUNT(*) FROM feed_items WHERE feed_id=?", (fid,)).fetchone()[0] == 0
    new = 0
    with db.tx(conn):
        if title and not feed["title"]:
            conn.execute("UPDATE feeds SET title=? WHERE id=?", (title, fid))
        batch_id = None
        for i, it in enumerate(items):
            if conn.execute("SELECT 1 FROM feed_items WHERE feed_id=? AND guid=?", (fid, it.guid)).fetchone():
                continue
            skip = first_poll and i >= int(feed["backfill"] or 0)
            cur = conn.execute("INSERT INTO feed_items(feed_id, guid, title, url, published, skipped) VALUES(?,?,?,?,?,?)",
                               (fid, it.guid, it.title, it.url, it.published, int(skip)))
            if skip or not it.url:
                continue
            if conn.execute("SELECT 1 FROM sources WHERE url=?", (it.url,)).fetchone():
                continue
            name = it.title or it.url
            if title:
                name = f"{title} – {name}"
            if batch_id is None:
                from .sources import new_batch
                batch_id = new_batch(conn, f"Feed: {title or feed.get('url') or fid} {time.strftime('%Y-%m-%d')}", "feed")
            src = conn.execute("INSERT INTO sources(name, url, mode, incognito, feed_id, status, batch_id) VALUES(?,?,?,?,?,'queued',?)",
                               (name[:200], it.url, feed["mode"], int(feed["incognito"]), fid, batch_id)).lastrowid
            worker.enqueue(conn, int(src), priority=-1)  # uploads first, feed catch-up after
            conn.execute("UPDATE feed_items SET source_id=? WHERE id=?", (src, cur.lastrowid))
            new += 1
        conn.execute("UPDATE feeds SET last_checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'), last_error=NULL,"
                     " n_items=(SELECT COUNT(*) FROM feed_items WHERE feed_id=?) WHERE id=?", (fid, fid))
    return {"new": new, "error": None}


def due_feeds(conn: sqlite3.Connection) -> list[dict]:
    out = []
    now = datetime.now(timezone.utc)
    for f in db.rows(conn, "SELECT * FROM feeds WHERE enabled=1"):
        last = f["last_checked_at"]
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
            except ValueError:
                last_dt = None
            if last_dt and now - last_dt < timedelta(minutes=int(f["interval_min"] or 60)):
                continue
        out.append(f)
    return out


def poll_due(conn: sqlite3.Connection) -> int:
    n = 0
    for f in due_feeds(conn):
        r = poll_feed(conn, f)
        if r["error"]:
            log.warning("feed %s: %s", f["url"], r["error"])
        else:
            n += r["new"]
    return n
