import json

import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, db, gallery
from tests.conftest import rand_unit
from tests.test_api import seed_speaker, wav_bytes


def test_review_queue_flow(data_dir, conn):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(0)), [], "cli")
    sid, ssid = seed_speaker(conn, "hearing", None)
    _, ssid2 = seed_speaker(conn, "other", pid)        # auto-matched to Alice: still reviewable
    conn.execute("UPDATE source_speakers SET source_id=? WHERE id=?", (sid, ssid2))
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        # upload with the 'review' intent sets the flag
        r = c.post("/api/upload", data={"mode": "review"}, files=[("files", ("a.wav", wav_bytes(1.2), "audio/wav"))]).json()["created"][0]
        src = c.get(f"/api/sources/{r['source_id']}").json()
        assert src["review"] == 1 and src["mode"] == "bulk" and src["incognito"] == 0
        assert c.post("/api/upload_url", json={"url": "https://x.test/v.mp3", "mode": "review"}).status_code == 200
        # queue every voice of an existing recording
        assert c.post(f"/api/sources/{sid}/review").json()["queued"] == 2
        q = c.get("/api/review").json()
        assert q["total"] == 2 and q["candidate_min_prob"] == 0.5 and q["items"][0]["source"]["name"] == "hearing"
        assert c.get("/api/stats").json()["review_pending"] == 2
        assert c.get(f"/api/sources/{sid}").json()["review_pending"] == 2
        # naming a voice completes its review item
        c.post(f"/api/speakers/{ssid}/name", json={"name": "Bob"})
        assert db.row(conn, "SELECT review_status FROM source_speakers WHERE id=?", (ssid,))["review_status"] == "done"
        # skip the other; notes on the voice and the recording
        sp = c.patch(f"/api/speakers/{ssid2}", json={"review_status": "skipped", "notes": "sounds like the chair"}).json()
        assert sp["review_status"] == "skipped" and sp["notes"] == "sounds like the chair"
        assert c.get("/api/review").json()["total"] == 0
        assert c.patch(f"/api/speakers/{ssid2}", json={"review_status": "bogus"}).status_code == 400
        s2 = c.patch(f"/api/sources/{sid}", json={"notes": "Senate Armed Services, Jan 2025", "name": "Hegseth hearing"}).json()
        assert s2["notes"].startswith("Senate") and s2["name"] == "Hegseth hearing"
        # re-queue picks the skipped one up again
        assert c.post(f"/api/sources/{sid}/review").json()["queued"] == 1
        # stills_info surfaces on the recording
        conn.execute("UPDATE sources SET stills_info=? WHERE id=?", (json.dumps({"kind": "error", "error": "no storyboard"}), sid))
        assert c.get(f"/api/sources/{sid}").json()["stills_info"]["error"] == "no storyboard"
