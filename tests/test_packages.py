import json

import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, db, gallery, mobile, transfer
from tests.conftest import rand_unit


def seed(conn):
    rng = np.random.default_rng(0)
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    ids = {}
    for name in ("Nancy Pelosi", "Chuck Schumer", "Local Guy"):
        pid = conn.execute("INSERT INTO persons(name, kind) VALUES(?, 'named')", (name,)).lastrowid
        gallery.add_prints(conn, pid, None, None, rand_unit(rng), [], "cli")
        ids[name] = pid
    return ids, settings


def test_effective_alert_resolution():
    s = {"alert_default_sound": "siren", "alert_default_message": "Hi [NAME]"}
    person = {"name": "Nancy", "alert": 0, "alert_sound": None, "alert_message": None}
    assert mobile.effective_alert(person, [], s) == {"alert": False, "via": None, "sound": "siren", "message": "Hi [NAME]", "spoken": "Hi Nancy"}
    g = {"name": "Politicians", "alert": 1, "alert_sound": "vault", "alert_message": "[NAME] detected"}
    e = mobile.effective_alert(person, [g], s)
    assert e["alert"] and e["via"] == "package:Politicians" and e["sound"] == "vault" and e["spoken"] == "Nancy detected"
    person2 = {"name": "Nancy", "alert": 1, "alert_sound": "chime", "alert_message": None}
    e2 = mobile.effective_alert(person2, [g], s)
    assert e2["via"] == "person" and e2["sound"] == "chime" and e2["spoken"] == "Nancy detected"


def test_packages_api_and_mobile_payload(data_dir, conn):
    ids, settings = seed(conn)
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        g = c.post("/api/groups", json={"name": "Politicians", "description": "demo"}).json()
        assert g["n"] == 0 and c.post("/api/groups", json={"name": "politicians"}).json()["id"] == g["id"]
        g = c.post(f"/api/groups/{g['id']}/members", json={"person_ids": [ids["Nancy Pelosi"], ids["Chuck Schumer"], 9999]}).json()
        assert g["n"] == 2
        g = c.patch(f"/api/groups/{g['id']}", json={"alert": True, "alert_sound": "vault", "alert_message": "ALERT. [NAME] IS NEARBY."}).json()
        assert g["alert"] == 1 and g["spoken_example"] == "ALERT. Nancy Pelosi IS NEARBY."
        assert c.patch(f"/api/groups/{g['id']}", json={"alert_sound": "cowbell"}).status_code == 400
        p = c.get(f"/api/persons/{ids['Nancy Pelosi']}").json()
        assert p["groups"][0]["name"] == "Politicians" and p["effective"]["alert"] is True and p["effective"]["via"] == "package:Politicians"
        assert p["spoken"] == "ALERT. Nancy Pelosi IS NEARBY." and p["alert"] == 0
        # local guy untouched
        assert c.get(f"/api/persons/{ids['Local Guy']}").json()["effective"]["alert"] is False
        # person-level override wins
        c.patch(f"/api/persons/{ids['Nancy Pelosi']}", json={"alert_sound": "chime"})
        assert c.get(f"/api/persons/{ids['Nancy Pelosi']}").json()["effective"]["sound"] == "chime"
        # live gallery sees the package alert
        gal = gallery.load_gallery(conn, settings)
        assert gal.persons[ids["Chuck Schumer"]]["alert"] is True and gal.persons[ids["Chuck Schumer"]]["alert_sound"] == "vault"
        assert gal.persons[ids["Local Guy"]]["alert"] is False
        # mobile payload
        m = c.get("/api/mobile/gallery.json").json()
        assert m["version"] == 2 and "vault" in m["sounds"] and m["groups"][0]["name"] == "Politicians" and m["groups"][0]["n"] == 2
        chuck = next(x for x in m["people"] if x["name"] == "Chuck Schumer")
        assert chuck["group_ids"] == [g["id"]] and chuck["effective_alert"] is True and chuck["alert"] is False
        # package export -> import into a fresh db
        r = c.get(f"/api/groups/{g['id']}/export")
        assert r.status_code == 200 and "voiceflock-package-Politicians" in r.headers["content-disposition"]
        data = r.json()
        assert data["package"]["alert_sound"] == "vault" and len(data["persons"]) == 2
        conn.execute("DELETE FROM groups"); conn.execute("DELETE FROM persons")
        st = transfer.import_gallery(conn, data)
        assert st["package"] == "Politicians" and st["persons_created"] == 2
        grp = db.row(conn, "SELECT * FROM groups WHERE name='Politicians'")
        assert grp["alert"] == 1 and conn.execute("SELECT COUNT(*) FROM person_groups WHERE group_id=?", (grp["id"],)).fetchone()[0] == 2
        # members list + remove + delete
        gid = grp["id"]
        members = c.get(f"/api/groups/{gid}").json()["members"]
        assert {x["name"] for x in members} == {"Nancy Pelosi", "Chuck Schumer"}
        assert c.delete(f"/api/groups/{gid}/members/{members[0]['id']}").json()["n"] == 1
        assert c.delete(f"/api/groups/{gid}").status_code == 200 and c.get("/api/groups").json()["groups"] == []


def test_auto_export_and_phone_settings(data_dir, conn, tmp_path):
    ids, settings = seed(conn)
    assert mobile.export_if_dirty(conn) is None                       # no path set
    settings.set("library_export_path", str(tmp_path / "Dropbox" / "VoiceFlock"))
    out = mobile.export_if_dirty(conn, force=True)
    assert out == tmp_path / "Dropbox" / "VoiceFlock" / "voiceflock-library.json" and out.exists()
    data = json.loads(out.read_text())
    assert data["format"] == "voiceflock-mobile" and len(data["people"]) == 3
    assert mobile.export_if_dirty(conn) is None                       # clean now
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        c.patch(f"/api/persons/{ids['Local Guy']}", json={"alert": True})   # marks dirty
        assert c.get("/api/mobile/export_status").json()["dirty"] is True
        assert mobile.export_if_dirty(conn) == out
        assert c.get("/api/mobile/export_status").json()["dirty"] is False
        g = c.post("/api/groups", json={"name": "Politicians"}).json()
        r = c.post("/api/import/phone_settings", json={"format": "voiceflock-phone-settings",
                   "persons": {str(ids["Nancy Pelosi"]): {"alert": True, "sound": "horn", "message": "Yo [NAME]"}, "424242": {"alert": True}},
                   "groups": {str(g["id"]): {"alert": True, "sound": "radar"}}}).json()
        assert r == {"persons": 1, "groups": 1, "enrollments": 0}
        p = c.get(f"/api/persons/{ids['Nancy Pelosi']}").json()
        assert p["alert"] == 1 and p["alert_sound"] == "horn" and p["spoken"] == "Yo Nancy Pelosi"
        assert c.get("/api/groups").json()["groups"][0]["alert_sound"] == "radar"
        assert c.post("/api/import/phone_settings", json={"format": "nope"}).status_code == 400
        assert c.post("/api/mobile/export_now").json()["written"] == str(out)
        # voiceprints captured on the phone: add to an existing person, match by name, or create
        import base64
        import numpy as np
        b64 = lambda: base64.b64encode(np.random.default_rng(1).standard_normal(192).astype(np.float32).tobytes()).decode()
        r = c.post("/api/import/phone_settings", json={"format": "voiceflock-phone-settings", "enrollments": [
            {"person_id": ids["Nancy Pelosi"], "prints": [b64()]},
            {"person_id": -3, "name": "nancy pelosi", "prints": [b64(), b64()]},
            {"person_id": -4, "name": "Phone Stranger", "alert": True, "prints": [b64()]},
            {"name": "", "prints": [b64()]}, {"name": "no prints"}]}).json()
        assert r["enrollments"] == 3
        assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=? AND origin='phone'", (ids["Nancy Pelosi"],)).fetchone()[0] == 3
        new = conn.execute("SELECT id, kind, alert FROM persons WHERE name='Phone Stranger'").fetchone()
        assert new and new[1] == "named" and new[2] == 1
        assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (new[0],)).fetchone()[0] == 1
