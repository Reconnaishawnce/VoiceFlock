import numpy as np
from fastapi.testclient import TestClient

from voiceflock import audio, config, db, gallery, live
from tests.conftest import rand_unit, speaker_cluster
from tests.test_api import seed_speaker


class FakeEmbedder:
    def __init__(self, sig):
        self.sig = sig

    def embed_batch(self, chunks, batch_size=16):
        return np.stack([self.sig for _ in chunks])


def gallery_with(conn, rng):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    c = rand_unit(rng)
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, c, speaker_cluster(rng, c, 3), "cli")
    other = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 2', 'provisional')").lastrowid
    gallery.add_prints(conn, other, None, None, rand_unit(rng), [], "auto-cluster")
    return pid, c, settings


def test_live_matcher_silence_then_alert(conn):
    rng = np.random.default_rng(0)
    pid, c, settings = gallery_with(conn, rng)
    g = gallery.load_gallery(conn, settings)
    m = live.LiveMatcher(FakeEmbedder(speaker_cluster(rng, c, 1)[0]), lambda: g, window_s=1.0, hop_s=0.5, match_threshold=0.75)
    silence = np.zeros(audio.SR, dtype=np.int16)
    frames = m.feed(silence)
    assert len(frames) == 2 and all(not f.speech and not f.candidates for f in frames)
    loud = (np.random.default_rng(1).normal(size=audio.SR) * 3000).astype(np.int16)
    frames = m.feed(loud)
    assert frames and frames[-1].speech and frames[-1].candidates[0]["name"] == "Alice"
    # smoothing: first speech window after silence averages with empty history -> lower than instant
    assert frames[0].candidates[0]["probability"] <= frames[0].candidates[0]["instant"] + 1e-9
    assert frames[-1].alert is True
    d = frames[-1].as_dict()
    assert d["type"] == "frame" and set(d) >= {"t", "level_db", "speech", "candidates", "alert"}


def test_live_websocket_roundtrip(data_dir, conn, monkeypatch):
    rng = np.random.default_rng(2)
    pid, c, settings = gallery_with(conn, rng)
    import voiceflock.embed as embed_mod
    monkeypatch.setattr(embed_mod.Embedder, "get", staticmethod(lambda *a, **k: FakeEmbedder(speaker_cluster(rng, c, 1)[0])))
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as client, client.websocket_connect("/ws/live") as ws:
        first = ws.receive_json()
        assert first["type"] == "status"
        second = ws.receive_json()
        assert second["type"] == "status" and second["gallery"] == 2
        loud = (np.random.default_rng(1).normal(size=audio.SR * 4) * 3000).astype(np.int16)
        ws.send_bytes(loud.tobytes())
        got = [ws.receive_json() for _ in range(2)]
        assert all(f["type"] == "frame" for f in got)
        assert got[-1]["candidates"][0]["name"] == "Alice" and got[-1]["alert"]


def test_glyph_is_deterministic_and_bounded():
    rng = np.random.default_rng(3)
    e = rand_unit(rng)
    g1, g2 = live.glyph(e), live.glyph(e * 2)
    assert g1 == g2 and len(g1) == 24 and all(0 <= v <= 1 for v in g1)
    assert live.glyph(rand_unit(rng)) != g1


def test_overview_jobs_clear_and_accept(data_dir, conn):
    rng = np.random.default_rng(4)
    pid, c, settings = gallery_with(conn, rng)
    u = db.row(conn, "SELECT id FROM persons WHERE kind='provisional'")["id"]
    s1, _ = seed_speaker(conn, "rec A", pid)
    conn.execute("UPDATE sources SET duration_s=3600 WHERE id=?", (s1,))
    conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, person_id, assignment) VALUES(?, 'S1', 100, 2, ?, 'auto')", (s1, u))
    conn.execute("INSERT INTO jobs(type, source_id, status, error) VALUES('process_source', ?, 'failed', 'boom')", (s1,))
    conn.execute("INSERT INTO jobs(type, status) VALUES('rematch', 'done')")
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as cl:
        running = conn.execute("INSERT INTO jobs(type, status) VALUES('rematch', 'running')").lastrowid  # after startup's orphan reset
        o = cl.get("/api/overview").json()
        assert o["stats"]["recordings"] == 1 and o["stats"]["hours"] == 1.0 and o["stats"]["known"] == 1 and o["stats"]["unknown"] == 1
        assert {v["name"] for v in o["voices"]} == {"Alice", "Unknown Voice 2"}
        assert all(-1 <= v["x"] <= 1 and len(v["glyph"]) == 24 for v in o["voices"])
        assert o["top_voices"][0]["name"] == "Unknown Voice 2"      # 100 s vs 12 s
        assert o["activity"][0]["recordings"] == 1 and o["pairs"][0]["n"] == 1
        # jobs: delete one, clear finished, running survives
        jobs = cl.get("/api/jobs").json()["jobs"]
        failed = [j for j in jobs if j["status"] == "failed"][0]
        assert cl.delete(f"/api/jobs/{failed['id']}").status_code == 200
        assert cl.delete(f"/api/jobs/{running}").status_code == 409
        assert cl.post("/api/jobs/clear").json()["deleted"] == 1
        assert [j["status"] for j in cl.get("/api/jobs").json()["jobs"]] == ["running"]
        # accept the unnamed voice
        p = cl.post(f"/api/persons/{u}/accept").json()
        assert p["kind"] == "named" and p["name"] == "Voice 2" and p["glyph"]
        assert cl.get("/api/voices/unnamed").json()["voices"] == []
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='rematch' AND status='queued'").fetchone()[0] == 1
