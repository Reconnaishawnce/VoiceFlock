import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, db, gallery, selfcal
from tests.conftest import rand_unit


def _voice(rng, base, noise=0.5):
    """A recording of `base`'s voice: unit vector perturbed by noise of total norm ≈ `noise` (cosine ≈ 0.9 at 0.5)."""
    return gallery.l2n(base + noise / np.sqrt(base.size) * rng.standard_normal(base.shape).astype(np.float32))


def test_calibration_from_decisions(data_dir, conn):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    rng = np.random.default_rng(11)
    bases = {n: rand_unit(rng) for n in ("Alice", "Bob", "Carol", "Dan")}
    pids = {}
    for name, base in bases.items():
        pids[name] = conn.execute("INSERT INTO persons(name, kind) VALUES(?, 'named')", (name,)).lastrowid
        gallery.add_prints(conn, pids[name], None, None, _voice(rng, base, 0.3), [], "cli")
    # 4 confirmed recordings per person (distinct sources), plus a few rejections of look-alikes
    k = 0
    for name, base in bases.items():
        for _ in range(4):
            k += 1
            sid = conn.execute("INSERT INTO sources(name, mode, status) VALUES(?, 'bulk', 'done')", (f"rec{k}",)).lastrowid
            ssid = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, embedding, person_id, assignment)"
                                " VALUES(?, 'S0', 20, 3, ?, ?, 'confirmed')", (sid, db.emb_to_blob(_voice(rng, base)), pids[name])).lastrowid
            gallery.add_prints(conn, pids[name], sid, ssid, _voice(rng, base), [], "confirm")
            other = [n for n in bases if n != name][0]
            conn.execute("INSERT INTO matches(source_speaker_id, person_id, rank, raw_score, norm_score, probability, margin, score_mode, status)"
                         " VALUES(?,?,2,0.3,0.3,0.2,0,'raw','rejected')", (ssid, pids[other]))
    av = selfcal.available(conn)
    assert av["labelled_speakers"] == 16 and av["labelled_persons"] == 4 and av["rejections"] == 16 and av["enough"]

    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        assert c.get("/api/stats").json()["calibrated"] is False
        st = c.get("/api/calibration").json()
        assert st["fitted"] == {} and st["available"]["enough"] is True
        r = c.post("/api/calibration/decisions").json()
        m = r["modes"]["raw"]
        assert r["n_target"] == 16 and r["n_nontarget"] >= 16 * 3 and m["a"] > 0 and m["eer"] < 0.1
        assert m["target_median"] > m["nontarget_median"]
        assert 0.5 <= r["suggested_match_threshold"] <= 0.97
        assert c.get("/api/stats").json()["calibrated"] is True
        st = c.get("/api/calibration").json()
        assert st["fitted"]["raw"]["n_target"] == 16
        # the fitted model is what the gallery now uses, and a rematch was queued to re-score everything
        g = gallery.load_gallery(conn)
        assert abs(g.calibrations["raw"].a - m["a"]) < 1e-9
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='rematch'").fetchone()[0] >= 1
        # same-voice pairs now score high, different-voice pairs low, under the fitted calibration
        probe = _voice(rng, bases["Alice"])
        sc = g.scores_by_person(probe)
        assert g.calibrations["raw"].prob(sc[pids["Alice"]][0]) > 0.9 > g.calibrations["raw"].prob(sc[pids["Bob"]][0])
        # back to defaults
        st = c.post("/api/calibration/reset").json()
        assert st["fitted"] == {} and c.get("/api/stats").json()["calibrated"] is False


def test_calibration_refuses_without_enough_decisions(data_dir, conn):
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Solo', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(0)), [], "cli")
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        r = c.post("/api/calibration/decisions")
        assert r.status_code == 400 and "Not enough decisions" in r.json()["detail"]
