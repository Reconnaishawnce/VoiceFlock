"""Rematch, feeds, duplicates, auth, export/import, self-test plumbing."""
import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from voiceflock import config, db, feeds, gallery, rematch, transfer, worker
from tests.conftest import rand_unit, speaker_cluster
from tests.test_api import seed_speaker, wav_bytes


@pytest.fixture()
def client(data_dir):
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        yield c


# ------------------------------------------------------------------ rematch
def test_rematch_creates_retroactive_alert(conn):
    """A recording scanned before Alice existed gets flagged once Alice is enrolled."""
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    rng = np.random.default_rng(0)
    alice_c = rand_unit(rng)
    # old recording: speaker with Alice's voice, no gallery at the time -> unassigned, no matches
    sid = conn.execute("INSERT INTO sources(name, mode, status) VALUES('old', 'bulk', 'done')").lastrowid
    ssid = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, first_start_s, embedding)"
                        " VALUES(?, 'S0', 30, 4, 2.0, ?)", (sid, db.emb_to_blob(speaker_cluster(rng, alice_c, 1)[0]))).lastrowid
    # a second, unrelated speaker that should stay unknown
    ssid2 = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, first_start_s, embedding)"
                         " VALUES(?, 'S1', 30, 4, 9.0, ?)", (sid, db.emb_to_blob(rand_unit(rng)))).lastrowid
    # now Alice is enrolled
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, alice_c, speaker_cluster(rng, alice_c, 3), "cli")

    res = rematch.rematch(conn, None)
    assert res["speakers"] == 2 and res["new_alerts"] == 1
    m = db.row(conn, "SELECT * FROM matches WHERE source_speaker_id=? AND rank=1", (ssid,))
    assert m["person_id"] == pid and m["status"] == "auto" and m["probability"] >= settings["match_threshold"]
    sp = db.row(conn, "SELECT * FROM source_speakers WHERE id=?", (ssid,))
    assert sp["person_id"] == pid and sp["assignment"] == "auto"
    m2 = db.row(conn, "SELECT * FROM matches WHERE source_speaker_id=? AND rank=1", (ssid2,))
    assert m2["status"] == "open" and db.row(conn, "SELECT person_id FROM source_speakers WHERE id=?", (ssid2,))["person_id"] is None
    # prints untouched: rematch never adds prints for named people
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (pid,)).fetchone()[0] == 4

    # rejected candidates stay rejected and are excluded next time
    conn.execute("UPDATE matches SET status='rejected' WHERE id=?", (m["id"],))
    conn.execute("UPDATE source_speakers SET person_id=NULL, assignment=NULL WHERE id=?", (ssid,))
    res = rematch.rematch(conn, [sid])
    rows = db.rows(conn, "SELECT * FROM matches WHERE source_speaker_id=? ORDER BY rank", (ssid,))
    assert any(r["status"] == "rejected" and r["person_id"] == pid for r in rows)
    assert not any(r["status"] in ("open", "auto") and r["person_id"] == pid for r in rows)
    assert res["new_alerts"] == 0


def test_rematch_preserves_user_decisions_and_clusters_unknowns(conn):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    rng = np.random.default_rng(1)
    c = rand_unit(rng)
    u = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 1', 'provisional')").lastrowid
    gallery.add_prints(conn, u, 50, None, c, [], "auto-cluster")
    sid = conn.execute("INSERT INTO sources(name, mode, status) VALUES('x', 'bulk', 'done')").lastrowid
    # confirmed speaker: must not be touched
    bob = conn.execute("INSERT INTO persons(name, kind) VALUES('Bob', 'named')").lastrowid
    conf = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, embedding, person_id, assignment)"
                        " VALUES(?, 'S0', 20, 2, ?, ?, 'confirmed')", (sid, db.emb_to_blob(c), bob)).lastrowid
    # unknown short speaker that now matches the provisional cluster -> attached
    short = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, embedding)"
                         " VALUES(?, 'S1', 4, 1, ?)", (sid, db.emb_to_blob(speaker_cluster(rng, c, 1)[0]))).lastrowid
    res = rematch.rematch(conn, None)
    assert res["clustered"] == 1
    assert db.row(conn, "SELECT person_id, assignment FROM source_speakers WHERE id=?", (conf,)) == {"person_id": bob, "assignment": "confirmed"}
    assert conn.execute("SELECT COUNT(*) FROM matches WHERE source_speaker_id=?", (conf,)).fetchone()[0] == 0
    assert db.row(conn, "SELECT person_id, assignment FROM source_speakers WHERE id=?", (short,)) == {"person_id": u, "assignment": "auto"}
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=? AND source_speaker_id=?", (u, short)).fetchone()[0] == 1


def test_rematch_job_runs_through_worker(conn):
    config.Settings(conn).set("cohort_auto", False)
    jid = worker.enqueue(conn, None, "rematch", dedupe=True)
    assert worker.enqueue(conn, None, "rematch", dedupe=True) == jid  # deduped
    job = worker.claim_job(conn, "t")
    worker.run_job(conn, job, gallery.GalleryCache())
    j = db.row(conn, "SELECT status, result FROM jobs WHERE id=?", (jid,))
    assert j["status"] == "done" and json.loads(j["result"])["speakers"] == 0


# ------------------------------------------------------------------ feeds
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Council Pod</title>
<item><title>Ep 3</title><guid>g3</guid><pubDate>Mon, 20 Oct 2026 10:00:00 GMT</pubDate><enclosure url="https://x.test/3.mp3" type="audio/mpeg"/></item>
<item><title>Ep 2</title><guid>g2</guid><enclosure url="https://x.test/2.mp3" type="audio/mpeg"/></item>
<item><title>Ep 1</title><guid>g1</guid><enclosure url="https://x.test/1.mp3" type="audio/mpeg"/></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
<title>Some Channel</title>
<entry><id>yt:video:abc</id><title>Meeting 1</title><link rel="alternate" href="https://www.youtube.com/watch?v=abc"/><published>2026-10-01T00:00:00+00:00</published></entry>
</feed>"""


def test_parse_rss_and_atom():
    title, items = feeds.parse_feed(RSS)
    assert title == "Council Pod" and [i.guid for i in items] == ["g3", "g2", "g1"]
    assert items[0].url == "https://x.test/3.mp3" and items[0].published.startswith("Mon")
    title, items = feeds.parse_feed(ATOM)
    assert title == "Some Channel" and items[0].url == "https://www.youtube.com/watch?v=abc" and items[0].guid == "yt:video:abc"
    with pytest.raises(ValueError):
        feeds.parse_feed(b"<html></html>")


def test_poll_feed_backfill_and_dedupe(conn, monkeypatch):
    monkeypatch.setattr(feeds, "fetch", lambda url, timeout=30: RSS)
    fid = feeds.add_feed(conn, "https://x.test/feed.xml", backfill=2)
    f = db.row(conn, "SELECT * FROM feeds WHERE id=?", (fid,))
    r = feeds.poll_feed(conn, f)
    assert r == {"new": 2, "error": None}
    srcs = db.rows(conn, "SELECT * FROM sources ORDER BY id")
    assert [s["url"] for s in srcs] == ["https://x.test/3.mp3", "https://x.test/2.mp3"]
    assert srcs[0]["name"].startswith("Council Pod – Ep 3") and srcs[0]["feed_id"] == fid and srcs[0]["status"] == "queued"
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM feed_items WHERE skipped=1").fetchone()[0] == 1
    f = db.row(conn, "SELECT * FROM feeds WHERE id=?", (fid,))
    assert f["title"] == "Council Pod" and f["n_items"] == 3 and f["last_checked_at"]
    # second poll: nothing new, and it is not due yet
    assert feeds.poll_feed(conn, f) == {"new": 0, "error": None}
    assert feeds.due_feeds(conn) == []
    # new episode appears
    monkeypatch.setattr(feeds, "fetch", lambda url, timeout=30: RSS.replace(b"<item><title>Ep 3", b'<item><title>Ep 4</title><guid>g4</guid><enclosure url="https://x.test/4.mp3"/></item><item><title>Ep 3'))
    assert feeds.poll_feed(conn, f)["new"] == 1
    # network error is recorded, not raised
    def boom(url, timeout=30):
        raise OSError("timeout")
    monkeypatch.setattr(feeds, "fetch", boom)
    assert feeds.poll_feed(conn, f)["error"] == "timeout"
    assert db.row(conn, "SELECT last_error FROM feeds WHERE id=?", (fid,))["last_error"].startswith("OSError")


def test_feeds_api(client, conn, monkeypatch):
    monkeypatch.setattr(feeds, "fetch", lambda url, timeout=30: RSS)
    assert client.post("/api/feeds", json={"url": "nope"}).status_code == 400
    f = client.post("/api/feeds", json={"url": "https://x.test/feed.xml", "backfill": 1, "mode": "match"}).json()
    assert client.post("/api/feeds", json={"url": "https://x.test/feed.xml"}).status_code == 409
    r = client.post(f"/api/feeds/{f['id']}/poll").json()
    assert r["queued"] == 1 and r["feed"]["title"] == "Council Pod" and r["feed"]["recent"][0]["source_id"]
    assert client.patch(f"/api/feeds/{f['id']}", json={"enabled": False}).json()["enabled"] == 0
    assert client.get("/api/feeds").json()["feeds"][0]["mode"] == "match"
    assert client.delete(f"/api/feeds/{f['id']}").status_code == 200
    assert client.get("/api/feeds").json()["feeds"] == []


# ------------------------------------------------------------------ duplicates
def test_duplicate_upload_detected(client):
    files = [("files", ("a.wav", wav_bytes(), "audio/wav"))]
    first = client.post("/api/upload", data={"mode": "bulk"}, files=files).json()["created"][0]
    again = client.post("/api/upload", data={"mode": "bulk"}, files=[("files", ("renamed.wav", wav_bytes(), "audio/wav"))]).json()["created"][0]
    assert again["duplicate_of"] == first["source_id"] and "source_id" not in again
    assert len(client.get("/api/sources").json()["sources"]) == 1
    forced = client.post("/api/upload", data={"mode": "bulk", "force": "true"}, files=files).json()["created"][0]
    assert forced["source_id"] != first["source_id"]
    u = client.post("/api/upload_url", json={"url": "https://x.test/ep.mp3"}).json()
    assert client.post("/api/upload_url", json={"url": "https://x.test/ep.mp3"}).json()["duplicate_of"] == u["source_id"]


def test_source_search(client, conn):
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Zed Zebra', 'named')").lastrowid
    seed_speaker(conn, "Council meeting", pid)
    seed_speaker(conn, "Podcast")
    assert [s["name"] for s in client.get("/api/sources?q=pod").json()["sources"]] == ["Podcast"]
    assert [s["name"] for s in client.get("/api/sources?q=zebra").json()["sources"]] == ["Council meeting"]
    assert len(client.get("/api/sources").json()["sources"]) == 2


# ------------------------------------------------------------------ auth
def test_basic_auth_when_password_set(data_dir, monkeypatch):
    monkeypatch.setenv("VOICEFLOCK_PASSWORD", "s3cret")
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        r = c.get("/api/health")
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")
        assert c.get("/api/health", auth=("anyone", "wrong")).status_code == 401
        assert c.get("/api/health", auth=("anyone", "s3cret")).status_code == 200
        assert c.get("/", auth=("x", "s3cret")).status_code == 200


# ------------------------------------------------------------------ export / import
def test_export_import_roundtrip(client, conn):
    rng = np.random.default_rng(2)
    pid = conn.execute("INSERT INTO persons(name, kind, notes) VALUES('Alice', 'named', 'mayor')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(rng), [rand_unit(rng)], "cli")
    u = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 9', 'provisional')").lastrowid
    gallery.add_prints(conn, u, None, None, rand_unit(rng), [], "auto-cluster")
    conn.execute("INSERT INTO cohort(label, embedding) VALUES('c', ?)", (db.emb_to_blob(rand_unit(rng)),))
    conn.execute("INSERT INTO calibrations(mode, a, b, active) VALUES('raw', 10, -0.3, 1)")
    seed_speaker(conn, "rec1", pid)

    data = client.get("/api/export/gallery.json").json()
    assert data["format"] == "voiceflock-gallery" and len(data["persons"]) == 2
    assert len(client.get("/api/export/gallery.json?include_unnamed=false").json()["persons"]) == 1
    csv_text = client.get("/api/export/matches.csv").text
    assert csv_text.splitlines()[0].startswith("source_id,source") and "rec1" in csv_text

    # import into a second database: Alice merges by name, unknown voice is created
    conn.execute("DELETE FROM persons WHERE id=?", (u,))
    stats = transfer.import_gallery(conn, data)
    assert stats == {"persons_created": 1, "persons_merged": 1, "prints": 3, "cohort": 1, "calibrations": 1, "package": None}
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (pid,)).fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM calibrations WHERE active=1").fetchone()[0] == 1
    # via the API upload path
    buf = io.BytesIO(json.dumps(data).encode())
    r = client.post("/api/import/gallery", files=[("file", ("g.json", buf, "application/json"))])
    assert r.status_code == 200 and r.json()["persons_merged"] == 1
    assert client.post("/api/import/gallery", files=[("file", ("g.json", io.BytesIO(b"{}"), "application/json"))]).status_code == 400
    # a rematch job was queued by the import
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='rematch' AND status='queued'").fetchone()[0] == 1


# ------------------------------------------------------------------ self-test plumbing
def test_selftest_endpoints_and_failure_path(client, conn, monkeypatch):
    assert client.get("/api/selftest").json()["job"] is None
    jid = client.post("/api/selftest").json()["job_id"]
    assert client.post("/api/selftest").json()["job_id"] == jid
    import voiceflock.selftest as st

    def fake(conn_, progress=None):
        return {"ok": False, "embedder_error": "ImportError: no torch"}
    monkeypatch.setattr(st, "run", fake)
    worker.run_job(conn, worker.claim_job(conn, "t"), gallery.GalleryCache())
    j = client.get("/api/selftest").json()["job"]
    assert j["status"] == "failed" and "no torch" in j["error"] and j["result"]["ok"] is False


def test_synthetic_dialogue_shape():
    from voiceflock.selftest import synthetic_dialogue
    w = synthetic_dialogue(4.0)
    assert w.shape == (64000,) and w.dtype == np.float32 and 0.2 < np.abs(w).max() <= 0.3
