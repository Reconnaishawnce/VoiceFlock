import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, gallery
from tests.conftest import rand_unit, speaker_cluster


def test_health_reports_gpu_and_workers(data_dir):
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False, workers=3)) as c:
        h = c.get("/api/health").json()
        assert set(h["gpu"]) == {"nvidia_driver_gpu", "torch_cuda", "torch_device_name", "mps"}
        assert h["workers"] == 0  # no worker in tests


def test_merge_suggestions(data_dir, conn):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    rng = np.random.default_rng(0)
    c = rand_unit(rng)
    u1 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 1', 'provisional')").lastrowid
    u2 = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 2', 'provisional')").lastrowid
    alice = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    bob = conn.execute("INSERT INTO persons(name, kind) VALUES('Bob', 'named')").lastrowid
    other = rand_unit(rng)
    gallery.add_prints(conn, u1, 1, None, c, speaker_cluster(rng, c, 2), "auto-cluster")
    gallery.add_prints(conn, u2, 2, None, speaker_cluster(rng, c, 1)[0], [], "auto-cluster")   # same voice, split
    gallery.add_prints(conn, alice, 3, None, speaker_cluster(rng, c, 1)[0], [], "cli")           # also the same voice
    gallery.add_prints(conn, bob, 4, None, other, [], "cli")
    d = rand_unit(rng)
    gallery.add_prints(conn, alice, 5, None, d, [], "cli")  # noise print on Alice shouldn't matter much
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as cl:
        j = cl.get("/api/voices/merge_suggestions").json()
        pairs = {(p["a"]["name"], p["b"]["name"]) for p in j["pairs"]}
        assert ("Unknown Voice 1", "Unknown Voice 2") in pairs
        assert not any("Bob" in pr for pr in pairs)
        assert all(not (p["a"]["kind"] == "named" and p["b"]["kind"] == "named") for p in j["pairs"])
        assert all(p["probability"] >= j["threshold"] for p in j["pairs"])
        # merge via the suggested action
        r = cl.post(f"/api/persons/{u2}/merge", json={"into_id": u1})
        assert r.status_code == 200 and r.json()["n_prints"] == 4
        assert cl.get(f"/api/persons/{u2}").status_code == 404


def test_worker_threads_start_and_stop(data_dir):
    from voiceflock import worker
    threads, stop = worker.start_background_worker(2)
    assert len(threads) == 2 and all(t.is_alive() for t in threads)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads)
