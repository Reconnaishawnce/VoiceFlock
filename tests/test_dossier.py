import io

import numpy as np
from fastapi.testclient import TestClient

from voiceflock import gallery, mobile
from tests.conftest import rand_unit
from tests.test_api import seed_speaker


def _png(w=120, h=160):
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (w, h), (200, 120, 40)).save(buf, "PNG"); return buf.getvalue()


def test_dossier_fields_photo_timeline_export(data_dir, conn):
    pid = conn.execute("INSERT INTO persons(name, kind, alert) VALUES('Elizabeth Warren', 'named', 1)").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(np.random.default_rng(0)), [], "cli")
    sid, ssid = seed_speaker(conn, "Finance hearing", pid)
    conn.execute("INSERT INTO segments(source_speaker_id, start_s, end_s) VALUES(?, 10, 40)", (ssid,))
    conn.execute("INSERT INTO transcript_segments(source_id, source_speaker_id, start_s, end_s, text) VALUES(?,?,12,20,?)",
                 (sid, ssid, "We should regulate mass surveillance and I will keep asking until we do."))
    conn.execute("INSERT INTO sightings(at_ms, person_id, name, probability, flagged, kind, device) VALUES(1700000000000, ?, 'Elizabeth Warren', 0.91, 1, 'phone', 'phone-1')", (pid,))
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        r = c.patch(f"/api/persons/{pid}", json={"role": "U.S. Senator (D-MA)", "watch_reason": "Finance committee questioning", "links": ["https://warren.senate.gov", " ", "https://en.wikipedia.org/wiki/Elizabeth_Warren"]}).json()
        assert r["role"] == "U.S. Senator (D-MA)" and r["links"] == ["https://warren.senate.gov", "https://en.wikipedia.org/wiki/Elizabeth_Warren"]
        p = c.get(f"/api/persons/{pid}").json()
        assert p["watch_reason"] == "Finance committee questioning" and p["photo_url"] is None
        kinds = [t["kind"] for t in p["timeline"]]
        assert "recording" in kinds and "phone" in kinds and p["timeline"][0]["kind"] == "recording"   # newest first (recording added now)
        assert p["quotes"][0]["text"].startswith("We should regulate") and p["quotes"][0]["source_name"] == "Finance hearing"
        assert p["sightings_total"] == 1 and p["last_heard_ms"]
        # photo upload -> urls, thumbnail, phone library carries it
        r = c.post(f"/api/persons/{pid}/photo", files={"file": ("me.png", _png(), "image/png")}).json()
        assert r["ok"] and r["photo_url"].startswith(f"/photos/{pid}.jpg")
        p = c.get(f"/api/persons/{pid}").json()
        assert p["photo_url"] and p["photo_thumb_url"] and c.get(p["photo_thumb_url"]).status_code == 200
        lib = mobile.build(conn)
        me = next(x for x in lib["people"] if x["id"] == pid)
        assert me["photo"] and len(me["photo"]) > 100 and me["role"] == "U.S. Senator (D-MA)"
        assert c.post(f"/api/persons/{pid}/photo", files={"file": ("x.txt", b"not an image", "text/plain")}).status_code == 400
        # watchlist card
        w = c.get("/api/watchlist").json()["people"]
        assert len(w) == 1 and w[0]["name"] == "Elizabeth Warren" and w[0]["photo_thumb_url"] and w[0]["role"] and w[0]["sightings_total"] == 1
        # printable + markdown dossier
        h = c.get(f"/api/persons/{pid}/dossier")
        assert h.status_code == 200 and "text/html" in h.headers["content-type"]
        assert "Elizabeth Warren" in h.text and "regulate mass surveillance" in h.text and "data:image/jpeg;base64" in h.text and "warren.senate.gov" in h.text
        m = c.get(f"/api/persons/{pid}/dossier", params={"format": "md"})
        assert m.text.startswith("# Elizabeth Warren") and "## Timeline" in m.text and "## In their words" in m.text
        # remove photo
        c.delete(f"/api/persons/{pid}/photo")
        assert c.get(f"/api/persons/{pid}").json()["photo_url"] is None
        # from_still without a local video is a clean 400
        assert c.post(f"/api/persons/{pid}/photo/from_still", json={"source_id": sid, "t": 12}).status_code == 400
        # not alerting -> off the watchlist
        c.patch(f"/api/persons/{pid}", json={"alert": False})
        assert c.get("/api/watchlist").json()["people"] == []
