import io

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from voiceflock import audio, config, db, gallery
from tests.conftest import rand_unit


@pytest.fixture()
def client(data_dir):
    from voiceflock.api import create_app
    app = create_app(run_worker=False)
    with TestClient(app) as c:
        yield c


def wav_bytes(seconds=2.0):
    buf = io.BytesIO()
    t = np.arange(int(seconds * audio.SR)) / audio.SR
    sf.write(buf, (0.2 * np.sin(2 * np.pi * 300 * t)).astype(np.float32), audio.SR, format="WAV")
    return buf.getvalue()


def seed_speaker(conn, source_name, person_id=None, prob=0.95, kind="named", source_mode="bulk"):
    """Insert a processed source with one speaker and a rank-1 match to person_id."""
    rng = np.random.default_rng(7)
    sid = conn.execute("INSERT INTO sources(name, mode, status) VALUES(?, ?, 'done')", (source_name, source_mode)).lastrowid
    ssid = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, first_start_s, quality_score, embedding)"
                        " VALUES(?, 'S0', 12, 3, 1.5, 70, ?)", (sid, db.emb_to_blob(rand_unit(rng)))).lastrowid
    if person_id:
        conn.execute("INSERT INTO matches(source_speaker_id, person_id, rank, raw_score, norm_score, probability, margin, score_mode, status)"
                     " VALUES(?,?,1,0.6,0.6,?,0.5,'raw','auto')", (ssid, person_id, prob))
        conn.execute("UPDATE source_speakers SET person_id=?, assignment='auto' WHERE id=?", (person_id, ssid))
    return sid, ssid


def test_health_settings_roundtrip(client):
    h = client.get("/api/health").json()
    assert h["ffmpeg"] is True and h["hf_token_set"] is False
    s = client.get("/api/settings").json()
    assert s["values"]["match_threshold"] == 0.75
    r = client.put("/api/settings", json={"match_threshold": "0.6", "save_unknown_voices": False, "hf_token": "hf_abc123XYZ"})
    assert r.status_code == 200
    v = client.get("/api/settings").json()["values"]
    assert v["match_threshold"] == 0.6 and v["save_unknown_voices"] is False
    assert v["hf_token"].startswith("•") and v["hf_token"].endswith("3XYZ")
    # masked token echoed back must not overwrite the stored one
    client.put("/api/settings", json={"hf_token": v["hf_token"]})
    assert config.Settings(db.connect())["hf_token"] == "hf_abc123XYZ"
    assert client.put("/api/settings", json={"nope": 1}).status_code == 400


def test_upload_creates_source_and_job(client):
    r = client.post("/api/upload", data={"mode": "bulk", "incognito": "true"},
                    files=[("files", ("clip one.wav", wav_bytes(), "audio/wav")), ("files", ("notes.txt", b"x", "text/plain"))])
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert created[0]["job_id"] and created[1]["error"] == "unsupported file type"
    src = client.get(f"/api/sources/{created[0]['source_id']}").json()
    assert src["status"] == "queued" and src["incognito"] == 1 and src["job"]["status"] == "queued"
    assert (config.ORIG_DIR / f"{src['id']}_clip one.wav").exists()
    jobs = client.get("/api/jobs").json()["jobs"]
    assert jobs[0]["source_id"] == src["id"]
    # enroll without a name is rejected
    r = client.post("/api/upload", data={"mode": "enroll"}, files=[("files", ("a.wav", wav_bytes(1.0), "audio/wav"))])
    assert r.status_code == 400
    r = client.post("/api/upload", data={"mode": "enroll", "person_name": "Carol"}, files=[("files", ("a.wav", wav_bytes(1.5), "audio/wav"))])
    assert r.status_code == 200
    persons = client.get("/api/persons").json()["persons"]
    assert [p["name"] for p in persons] == ["Carol"]
    assert client.post("/api/upload_url", json={"url": "ftp://x"}).status_code == 400
    r = client.post("/api/upload_url", json={"url": "https://example.com/ep.mp3", "mode": "match"})
    assert r.status_code == 200 and client.get(f"/api/sources/{r.json()['source_id']}").json()["url"] == "https://example.com/ep.mp3"


def test_confirm_reject_name_undo_alerts(client, conn):
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(1)), [], "cli")
    sid, ssid = seed_speaker(conn, "ep7", pid, prob=0.85)

    alerts = client.get("/api/alerts").json()
    assert len(alerts["known"]) == 1 and alerts["known"][0]["person_name"] == "Alice"
    mid = alerts["known"][0]["match_id"]
    assert alerts["confirm_threshold"] == 0.9

    # confirm -> prints added with provenance, speaker assigned
    sp = client.post(f"/api/matches/{mid}/confirm").json()
    assert sp["assignment"] == "confirmed" and sp["person"]["name"] == "Alice"
    p = client.get(f"/api/persons/{pid}").json()
    assert any(pr["origin"] == "confirm" and pr["source_speaker_id"] == ssid for pr in p["prints"])
    assert p["heard_in"][0]["name"] == "ep7"

    # undo
    sp = client.post(f"/api/speakers/{ssid}/unassign").json()
    assert sp["person"] is None
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE source_speaker_id=?", (ssid,)).fetchone()[0] == 0
    assert client.get(f"/api/sources/{sid}").json()["speakers"][0]["candidates"][0]["status"] == "open"

    # reject, then undo the rejection
    sp = client.post(f"/api/matches/{mid}/reject").json()
    assert sp["candidates"][0]["status"] == "rejected"
    sp = client.post(f"/api/matches/{mid}/reopen").json()
    assert sp["candidates"][0]["status"] == "open" and sp["person"] is None
    sp = client.post(f"/api/matches/{mid}/reject").json()

    # name the speaker as somebody new
    sp = client.post(f"/api/speakers/{ssid}/name", json={"name": "Dave"}).json()
    assert sp["person"]["name"] == "Dave" and sp["assignment"] == "named"
    dave = [p for p in client.get("/api/persons").json()["persons"] if p["name"] == "Dave"][0]
    assert dave["n_prints"] >= 1 and dave["n_sources"] == 1

    # forget the source -> Dave keeps existing (named) but loses the prints from this recording
    client.post(f"/api/sources/{sid}/forget")
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE source_id=?", (sid,)).fetchone()[0] == 0
    assert client.get(f"/api/sources/{sid}").json()["incognito"] == 1


def test_unnamed_voice_naming_merge_delete(client, conn):
    rng = np.random.default_rng(3)
    u1 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 1', 'provisional')").lastrowid
    u2 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 2', 'provisional')").lastrowid
    s1, ss1 = seed_speaker(conn, "a", u1)
    s2, ss2 = seed_speaker(conn, "b", u1)
    s3, ss3 = seed_speaker(conn, "c", u2)
    for pid, ss, s in ((u1, ss1, s1), (u1, ss2, s2), (u2, ss3, s3)):
        gallery.add_prints(conn, pid, s, ss, rand_unit(rng), [], "auto-cluster")

    v = client.get("/api/voices/unnamed").json()["voices"]
    assert [x["id"] for x in v] == [u1, u2] and v[0]["n_sources"] == 2
    rec = client.get("/api/alerts").json()["recurring_unknown"]
    assert len(rec) == 1 and rec[0]["person_id"] == u1

    # naming an unnamed voice makes it known and confirms its auto matches
    p = client.patch(f"/api/persons/{u1}", json={"name": "Erin"}).json()
    assert p["kind"] == "named"
    assert conn.execute("SELECT COUNT(*) FROM matches WHERE person_id=? AND status='confirmed'", (u1,)).fetchone()[0] == 2

    # naming u2 with the same name merges into Erin
    p = client.patch(f"/api/persons/{u2}", json={"name": "erin"}).json()
    assert p["id"] == u1 and p["n_sources"] == 3
    assert client.get(f"/api/persons/{u2}").status_code == 404

    # merge endpoint + delete
    u3 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 3', 'provisional')").lastrowid
    gallery.add_prints(conn, u3, None, None, rand_unit(rng), [], "auto-cluster")
    assert client.post(f"/api/persons/{u3}/merge", json={"into_id": u1}).json()["n_prints"] == 4
    assert client.delete(f"/api/persons/{u1}").status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM source_speakers WHERE person_id IS NOT NULL").fetchone()[0] == 0


def test_delete_source_cleans_up(client, conn):
    u1 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 1', 'provisional')").lastrowid
    sid, ssid = seed_speaker(conn, "x", u1)
    gallery.add_prints(conn, u1, sid, ssid, rand_unit(np.random.default_rng(0)), [], "auto-cluster")
    clip = config.CLIP_DIR / f"{sid}_{ssid}.wav"
    config.ensure_dirs(); clip.write_bytes(b"RIFF")
    conn.execute("UPDATE source_speakers SET clip_path=? WHERE id=?", (clip.name, ssid))
    assert client.get(f"/api/sources/{sid}").json()["speakers"][0]["clip_url"] == f"/clips/{clip.name}"
    assert client.delete(f"/api/sources/{sid}").status_code == 200
    assert not clip.exists()
    assert client.get(f"/api/sources/{sid}").status_code == 404
    # the unnamed voice had no other prints -> removed too
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0
    assert client.get("/api/stats").json()["persons_unknown"] == 0


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "VoiceFlock" in r.text


def test_naming_a_clustered_speaker_names_the_whole_unnamed_voice(client, conn):
    """Two recordings auto-clustered into 'Unknown Voice 1'. Naming the speaker in one of them must rename
    the voice (both recordings follow) rather than spawn a new person and reject the 98% look-alike."""
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 1', 'provisional')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(3)), [], "auto")
    sid1, ss1 = seed_speaker(conn, "hearing A", pid, prob=0.987)
    sid2, ss2 = seed_speaker(conn, "hearing B", pid, prob=0.95)
    sp = client.post(f"/api/speakers/{ss1}/name", json={"name": "Sen. Elizabeth Warren"}).json()
    assert sp["person"]["id"] == pid and sp["person"]["name"] == "Sen. Elizabeth Warren" and sp["person"]["kind"] == "named"
    assert sp["candidates"][0]["status"] == "confirmed"                      # not rejected
    assert client.get(f"/api/sources/{sid2}").json()["speakers"][0]["person"]["name"] == "Sen. Elizabeth Warren"
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
    # naming another unnamed voice with the same name merges it into Warren
    pid2 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 2', 'provisional')").lastrowid
    gallery.add_prints(conn, pid2, None, None, rand_unit(np.random.default_rng(4)), [], "auto")
    sid3, ss3 = seed_speaker(conn, "hearing C", pid2, prob=0.9)
    sp = client.post(f"/api/speakers/{ss3}/name", json={"name": "sen. elizabeth warren"}).json()
    assert sp["person"]["id"] == pid
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM source_speakers WHERE person_id=?", (pid,)).fetchone()[0] == 3
    # a speaker already assigned to a *named* person keeps the old behaviour: reassign just this recording
    other = conn.execute("INSERT INTO persons(name, kind) VALUES('Bob', 'named')").lastrowid
    gallery.add_prints(conn, other, None, None, rand_unit(np.random.default_rng(5)), [], "cli")
    sid4, ss4 = seed_speaker(conn, "hearing D", other, prob=0.8)
    sp = client.post(f"/api/speakers/{ss4}/name", json={"name": "Carol"}).json()
    assert sp["person"]["name"] == "Carol" and client.get(f"/api/persons/{other}").json()["name"] == "Bob"


def test_ignore_noise_speaker(client, conn):
    """A 'speaker' that is laughter or ambient noise: Not a voice removes it from matching for good, undo brings it back."""
    from voiceflock import rematch
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(1)), [], "cli")
    sid, ssid = seed_speaker(conn, "noisy", pid, prob=0.8)
    client.post(f"/api/matches/{client.get('/api/alerts').json()['known'][0]['match_id']}/confirm")
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE source_speaker_id=?", (ssid,)).fetchone()[0] == 1
    client.post(f"/api/sources/{sid}/review")
    sp = client.post(f"/api/speakers/{ssid}/ignore").json()
    assert sp["ignored"] == 1 and sp["person"] is None and sp["candidates"] == []
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE source_speaker_id=?", (ssid,)).fetchone()[0] == 0
    assert client.get("/api/review").json()["total"] == 0 and client.get("/api/stats").json()["review_pending"] == 0
    assert client.get(f"/api/persons/{pid}").json()["name"] == "Alice"          # the person itself is untouched
    rematch.rematch(conn)                                                         # re-matching leaves it alone
    assert conn.execute("SELECT COUNT(*) FROM matches WHERE source_speaker_id=?", (ssid,)).fetchone()[0] == 0
    assert client.get(f"/api/sources/{sid}").json()["speakers"][0]["ignored"] == 1
    sp = client.post(f"/api/speakers/{ssid}/unignore").json()
    assert sp["ignored"] == 0
    assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='rematch' AND status='queued'").fetchone()[0] >= 1
    rematch.rematch(conn)
    assert conn.execute("SELECT COUNT(*) FROM matches WHERE source_speaker_id=?", (ssid,)).fetchone()[0] >= 1


def test_sightings_timeline(client, conn):
    """Laptop Live mode, the phone and new recordings share one timeline; phones poll with since_ms and skip their own echoes."""
    pid = conn.execute("INSERT INTO persons(name, kind, alert) VALUES('Alice', 'named', 1)").lastrowid
    r = client.post("/api/sightings", json={"items": [{"kind": "live", "person_id": pid, "name": "Alice", "probability": 0.93, "flagged": True}]}).json()
    assert r["added"] == 1
    r = client.post("/api/sightings", json={"device": "phone-1", "items": [
        {"kind": "phone", "person_id": pid, "name": "Alice", "probability": 0.88, "flagged": True, "at_ms": 1700000000000},
        {"kind": "phone", "person_id": pid, "name": "Alice", "probability": 0.88, "flagged": True, "at_ms": 1700000000000},   # duplicate upload
        {"kind": "bogus", "name": "x"}]}).json()
    assert r["added"] == 1
    allv = client.get("/api/sightings").json()["sightings"]
    assert [x["kind"] for x in allv] == ["live", "phone"] and allv[1]["device"] == "phone-1"
    # the phone asks for everything after its watermark, excluding itself
    mine = client.get("/api/sightings", params={"since_ms": 1700000000000, "exclude_device": "phone-1"}).json()["sightings"]
    assert len(mine) == 1 and mine[0]["kind"] == "live"
    assert client.get("/api/sightings", params={"kinds": "phone"}).json()["sightings"][0]["kind"] == "phone"
    p = client.get(f"/api/persons/{pid}").json()
    assert len(p["sightings"]) == 2
    h = client.get("/api/health").json()
    assert "tailscale" in h and "installed" in h["tailscale"] and isinstance(h["port"], int)
