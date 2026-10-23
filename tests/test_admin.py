import numpy as np
from fastapi.testclient import TestClient

from voiceflock import admin, config, gallery
from tests.conftest import rand_unit
from tests.test_api import seed_speaker


def test_check_update_reports_git_state():
    u = admin.check_update()
    assert u["git"] is True and "supervised" in u
    # either a real fetch result or a clear error (offline / blocked network)
    assert ("behind" in u and "local" in u) or u.get("error")


def test_wipe_keeps_settings_and_models(data_dir, conn):
    settings = config.Settings(conn)
    settings.set("hf_token", "hf_keepme")
    settings.set("cohort_auto", False)
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(0)), [], "cli")
    sid, ssid = seed_speaker(conn, "rec", pid)
    conn.execute("INSERT INTO groups(name) VALUES('Politicians')")
    conn.execute("INSERT INTO jobs(type, source_id, status) VALUES('process_source', ?, 'queued')", (sid,))
    config.ensure_dirs()
    (config.CLIP_DIR / "x.wav").write_bytes(b"RIFF"); (config.AUDIO_DIR / "1.wav").write_bytes(b"RIFF")
    (config.MODEL_DIR / "keep.bin").write_bytes(b"model")
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        assert c.post("/api/admin/wipe", json={"confirm": "nope"}).status_code == 400
        r = c.post("/api/admin/wipe", json={"confirm": "WIPE"}).json()["wiped"]
        assert r["persons"] == 1 and r["sources"] == 1 and r["groups"] == 1 and r["prints"] == 1
        assert c.get("/api/stats").json()["persons_named"] == 0
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert config.Settings(conn)["hf_token"] == "hf_keepme"
        assert not (config.CLIP_DIR / "x.wav").exists() and not (config.AUDIO_DIR / "1.wav").exists()
        assert (config.MODEL_DIR / "keep.bin").exists()
        # ids start over
        assert conn.execute("INSERT INTO persons(name, kind) VALUES('B', 'named')").lastrowid == 1
        # restart refuses when not supervised
        assert c.post("/api/admin/restart").status_code == 400
        h = c.get("/api/health").json()
        assert h["supervised"] is False and h["started_at"] and h["version"] == __import__("voiceflock").__version__
        assert c.get("/api/admin/update/status").json()["running"] is False
