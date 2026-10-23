import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, gallery
from voiceflock.api import render_alert_message
from tests.conftest import rand_unit


def test_render_alert_message():
    assert render_alert_message("Hi [NAME]. [NAME]!", "Nancy Pelosi") == "Hi Nancy Pelosi. Nancy Pelosi!"
    assert render_alert_message("{name} here", "X") == "X here"
    assert render_alert_message("", "X") == ""


def test_person_alert_settings_flow_to_phone(data_dir, conn):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    rng = np.random.default_rng(0)
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Nancy Pelosi', 'named')").lastrowid
    gallery.add_prints(conn, pid, None, None, rand_unit(rng), [], "cli")
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        p = c.get(f"/api/persons/{pid}").json()
        assert p["alert_sound"] is None and p["alert_message"] is None
        assert p["spoken"].startswith("Attention. Nancy Pelosi, your voice was detected nearby.")
        assert c.patch(f"/api/persons/{pid}", json={"alert_sound": "foghorn"}).status_code == 400
        p = c.patch(f"/api/persons/{pid}", json={"alert": True, "alert_sound": "klaxon",
                                               "alert_message": "IT'S TIME TO REGULATE MASS SURVEILLANCE. [NAME], YOUR VOICE WAS DETECTED NEARBY."}).json()
        assert p["alert_sound"] == "klaxon" and p["spoken"].startswith("IT'S TIME TO REGULATE MASS SURVEILLANCE. Nancy Pelosi,")
        # clearing goes back to the default template
        c.put("/api/settings", json={"alert_default_message": "Hello [NAME]."})
        p = c.patch(f"/api/persons/{pid}", json={"alert_message": ""}).json()
        assert p["alert_message"] is None and p["spoken"] == "Hello Nancy Pelosi."
        m = c.get("/api/mobile/gallery.json").json()
        assert m["alert_defaults"] == {"message": "Hello [NAME].", "sound": "siren"}
        person = m["people"][0]
        assert person["alert"] is True and person["alert_sound"] == "klaxon" and person["spoken"] == "Hello Nancy Pelosi."
        # live gallery carries the per-person sound/message
        g = gallery.load_gallery(conn, settings)
        assert g.persons[pid]["alert_sound"] == "klaxon" and g.persons[pid]["alert"] is True
