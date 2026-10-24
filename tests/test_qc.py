import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, db, gallery, qc, sources
from tests.conftest import rand_unit


def _src(conn, name, status="done", duration=600, sha=None, url=None, batch=None):
    return conn.execute("INSERT INTO sources(name, mode, status, duration_s, sha256, url, batch_id, error) VALUES(?, 'bulk', ?, ?, ?, ?, ?, ?)",
                        (name, status, duration, sha, url, batch, "boom" if status == "failed" else None)).lastrowid


def _spk(conn, sid, label, speech, snr, quality, clip=0.0, person=None, emb=None):
    rng = np.random.default_rng(1)
    ssid = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, snr_db, quality_score, clip_fraction, embedding, person_id, assignment)"
                        " VALUES(?,?,?,1,?,?,?,?,?,?)", (sid, label, speech, snr, quality, clip, db.emb_to_blob(emb if emb is not None else rand_unit(rng)), person, "auto" if person else None)).lastrowid
    return ssid


def test_quality_report_and_cleanup(data_dir, conn):
    settings = config.Settings(conn); settings.set("cohort_auto", False)
    with db.tx(conn):
        b = sources.new_batch(conn, "YouTube: C-SPAN (6 videos)", "youtube")
    good = _src(conn, "Hearing", batch=b)
    _spk(conn, good, "S0", 900, 25, 80)
    noisy = _src(conn, "Noisy stream", batch=b)
    ns = _spk(conn, noisy, "S0", 4, 5, 12)                       # laughter-like: short, low SNR, low quality
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 9', 'provisional')").lastrowid
    conn.execute("UPDATE source_speakers SET person_id=?, assignment='auto' WHERE id=?", (pid, ns))
    gallery.add_prints(conn, pid, noisy, ns, rand_unit(np.random.default_rng(2)), [], "auto-cluster", 4, 12)
    failed = _src(conn, "Broken link", status="failed", batch=b)
    empty = _src(conn, "Silence", batch=b)
    conn.execute("UPDATE sources SET n_speakers=0 WHERE id=?", (empty,))
    short = _src(conn, "Clip", duration=20, batch=b); _spk(conn, short, "S0", 15, 20, 60)
    dup = _src(conn, "Hearing copy", sha="abc", batch=b); _spk(conn, dup, "S0", 500, 22, 70)
    conn.execute("UPDATE sources SET sha256='abc' WHERE id=?", (good,))
    music = _src(conn, "Concert", duration=3600, batch=b); _spk(conn, music, "S0", 120, 18, 50)
    other_batch = _src(conn, "Elsewhere"); _spk(conn, other_batch, "S0", 3, 4, 10)   # noise, but outside the batch

    rep = qc.report(conn, batch_id=b)
    R = rep["recordings"]
    assert rep["scope"]["n_sources"] == 7 and rep["scope"]["done"] == 6
    assert [x["name"] for x in R["failed"]] == ["Broken link"] and R["failed"][0]["error"] == "boom"
    assert [x["name"] for x in R["empty"]] == ["Silence"]
    assert [x["name"] for x in R["short"]] == ["Clip"]
    assert [x["name"] for x in R["duplicates"]] == ["Hearing copy"] and R["duplicates"][0]["duplicate_of"] == good
    assert {x["name"] for x in R["little_speech"]} == {"Concert", "Noisy stream"}   # 4 s of speech in 10 min counts too
    assert [x["name"] for x in R["low_snr"]] == ["Noisy stream"]
    assert [n["id"] for n in rep["noise_voices"]] == [ns] and set(rep["noise_voices"][0]["reasons"]) >= {"low quality", "under 6 s", "low SNR"}
    assert rep["noise_prints"] == 1 and rep["low_quality_prints"] == 1
    # the whole library sees the extra noise speaker too
    assert len(qc.report(conn)["noise_voices"]) == 2

    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        bl = c.get("/api/batches").json()["batches"]
        assert bl[0]["name"].startswith("YouTube") and bl[0]["n"] == 7 and bl[0]["n_failed"] == 1
        assert c.get("/api/qc", params={"batch_id": b}).json()["scope"]["n_sources"] == 7
        assert c.post("/api/qc/apply", json={"actions": ["nuke"], "batch_id": b}).status_code == 400
        r = c.post("/api/qc/apply", json={"actions": ["ignore_noise", "delete_failed", "delete_empty", "delete_duplicates", "delete_short"], "batch_id": b}).json()["done"]
        assert r == {"ignored_voices": 1, "delete_failed": 1, "delete_empty": 1, "delete_duplicates": 1, "delete_short": 1}
        assert db.row(conn, "SELECT ignored, person_id FROM source_speakers WHERE id=?", (ns,))["ignored"] == 1
        assert conn.execute("SELECT COUNT(*) FROM persons WHERE id=?", (pid,)).fetchone()[0] == 0      # empty unnamed voice gone
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE id IN (?,?,?,?)", (failed, empty, dup, short)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE id=?", (good,)).fetchone()[0] == 1
        rep2 = c.get("/api/qc", params={"batch_id": b}).json()
        assert rep2["noise_voices"] == [] and rep2["recordings"]["failed"] == [] and rep2["recordings"]["duplicates"] == []
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='rematch'").fetchone()[0] >= 1
        # prune low-quality prints library-wide (the one on 'Elsewhere' never got a print; add one and prune)
        p2 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 77', 'provisional')").lastrowid
        es = db.row(conn, "SELECT id FROM source_speakers WHERE source_id=?", (other_batch,))["id"]
        gallery.add_prints(conn, p2, other_batch, es, rand_unit(np.random.default_rng(3)), [], "auto-cluster", 3, 10)
        assert c.get("/api/qc").json()["low_quality_prints"] == 1
        assert c.post("/api/qc/apply", json={"actions": ["prune_low_prints"]}).json()["done"] == {"pruned_prints": 1}
        assert conn.execute("SELECT COUNT(*) FROM persons WHERE id=?", (p2,)).fetchone()[0] == 0


def test_quality_gate_keeps_junk_out_of_the_library(conn):
    """A low-quality speaker is listed and matched but never becomes an unnamed voice or a print."""
    from voiceflock import pipeline
    settings = config.Settings(conn); settings.set("cohort_auto", False)
    sid = conn.execute("INSERT INTO sources(name, mode, status) VALUES('rec', 'bulk', 'processing')").lastrowid
    src = db.row(conn, "SELECT * FROM sources WHERE id=?", (sid,))
    emb = rand_unit(np.random.default_rng(0))
    ssid = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, snr_db, quality_score, embedding) VALUES(?, 'S0', 12, 2, 6, 20, ?)", (sid, db.emb_to_blob(emb))).lastrowid
    r = pipeline.SpeakerResult("S0", [(0, 12)], 12.0, mean_emb=emb, chunk_embs=np.stack([emb]), snr_db=6, quality=20)
    r.source_speaker_id = ssid
    with db.tx(conn):
        pipeline.decide_and_record(conn, src, [r], settings, gallery.GalleryCache())
    assert r.decision == "unknown" and r.person_id is None
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 0 and conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == 0
    # raise the bar to zero and the same speaker becomes an unnamed voice
    settings.set("min_print_quality", 0)
    r2 = pipeline.SpeakerResult("S0", [(0, 12)], 12.0, mean_emb=emb, chunk_embs=np.stack([emb]), snr_db=6, quality=20); r2.source_speaker_id = ssid
    with db.tx(conn):
        pipeline.decide_and_record(conn, src, [r2], settings, gallery.GalleryCache())
    assert r2.decision == "new_unknown" and conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0] >= 1
