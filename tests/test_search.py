import numpy as np
from fastapi.testclient import TestClient

from voiceflock import db, gallery
from tests.conftest import rand_unit
from tests.test_api import seed_speaker


def test_speaker_aware_search(data_dir, conn):
    pid = conn.execute("INSERT INTO persons(name, kind, alert, role) VALUES('Nancy Pelosi', 'named', 1, 'Speaker Emerita')").lastrowid
    other = conn.execute("INSERT INTO persons(name, kind) VALUES('Pelosi Staffer', 'provisional')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(0)), [], "cli")
    s1, ss1 = seed_speaker(conn, "Floor speech", pid)
    s2, ss2 = seed_speaker(conn, "Old interview", pid)
    s3, ss3 = seed_speaker(conn, "Presser", other)
    conn.execute("UPDATE source_speakers SET speech_seconds=600 WHERE id=?", (ss1,))
    conn.execute("UPDATE source_speakers SET speech_seconds=90 WHERE id=?", (ss2,))
    conn.execute("UPDATE sources SET recorded_on='2026-10-20' WHERE id=?", (s1,))
    conn.execute("UPDATE sources SET recorded_on='2025-01-05' WHERE id=?", (s2,))
    conn.execute("INSERT INTO segments(source_speaker_id, start_s, end_s) VALUES(?, 100, 160)", (ss1,))
    conn.execute("INSERT INTO transcript_segments(source_id, source_speaker_id, start_s, end_s, text) VALUES(?,?,101,110,'We must regulate mass surveillance now.')", (s1, ss1))
    conn.execute("INSERT INTO sightings(at_ms, person_id, name, probability, flagged, kind, device) VALUES(?, ?, 'Nancy Pelosi', 0.9, 1, 'phone', 'p1')", (1761000000000, pid))
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        r = c.get("/api/search", params={"q": "pelosi"}).json()
        assert [p["name"] for p in r["people"]] == ["Nancy Pelosi", "Pelosi Staffer"]
        assert set(r["person_ids"]) == {pid, other}
        assert [a["source_name"] for a in r["appearances"]] == ["Floor speech", "Presser", "Old interview"] or len(r["appearances"]) == 3
        assert r["summary"]["n_recordings"] == 3 and r["summary"]["speech_seconds"] == 600 + 90 + 12
        fs = next(a for a in r["appearances"] if a["source_name"] == "Floor speech")
        assert fs["watch_at"] == 100 and fs["n_lines"] == 1 and fs["day"] == "2026-10-20"
        assert r["sightings"] and r["sightings"][0]["kind"] == "phone"
        # explicit person + date range + minimum length
        r = c.get("/api/search", params={"person_id": pid, "date_from": "2026-01-01", "min_seconds": 300}).json()
        assert [a["source_name"] for a in r["appearances"]] == ["Floor speech"] and r["summary"]["n_appearances"] == 1
        r = c.get("/api/search", params={"person_id": pid, "date_to": "2025-12-31"}).json()
        assert [a["source_name"] for a in r["appearances"]] == ["Old interview"]
        # phrase search hits transcripts (and the person filter applies to lines too)
        r = c.get("/api/search", params={"q": "mass surveillance"}).json()
        assert r["lines"] and "[mass surveillance]" in r["lines"][0]["snippet"] and r["lines"][0]["person_name"] == "Nancy Pelosi"
        assert c.get("/api/search", params={"q": "mass surveillance", "person_id": other}).json()["lines"] == []
        # recordings by name; bad dates rejected; recorded_on editable
        assert c.get("/api/search", params={"q": "interview"}).json()["recordings"][0]["name"] == "Old interview"
        assert c.get("/api/search", params={"q": "x", "date_from": "yesterday"}).status_code == 400
        assert c.patch(f"/api/sources/{s3}", json={"recorded_on": "2026-10-23"}).status_code == 200
        assert c.patch(f"/api/sources/{s3}", json={"recorded_on": "23/10/2026"}).status_code == 400
        assert db.row(conn, "SELECT recorded_on FROM sources WHERE id=?", (s3,))["recorded_on"] == "2026-10-23"
        assert c.get("/api/search").json()["appearances"] == []
