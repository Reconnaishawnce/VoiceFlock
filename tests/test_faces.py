import io

import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, db, dossier, faces, suggest
from tests.test_transcribe import seed


def _png(color):
    from PIL import Image
    buf = io.BytesIO(); Image.new("RGB", (200, 260), color).save(buf, "PNG"); return buf.getvalue()


def _unit(seed_):
    v = np.random.default_rng(seed_).standard_normal(512).astype(np.float32); return v / np.linalg.norm(v)


def test_reference_faces_and_face_evidence(data_dir, conn, monkeypatch, tmp_path):
    settings = config.Settings(conn); settings.set("face_match", True); settings.set("llm_provider", "none"); settings.set("suggest_ocr", False)
    # pretend the models are present and faces are recognised by photo colour
    monkeypatch.setattr(faces, "available", lambda: True)
    red, blue = _unit(1), _unit(2)
    def fake_from_file(path):
        from PIL import Image
        px = Image.open(path).convert("RGB").resize((1, 1)).getpixel((0, 0))
        return red if px[0] > px[2] else blue
    monkeypatch.setattr(faces, "face_embedding_from_file", fake_from_file)
    warren = conn.execute("INSERT INTO persons(name, kind) VALUES('Elizabeth Warren', 'named')").lastrowid
    kennedy = conn.execute("INSERT INTO persons(name, kind) VALUES('Robert F. Kennedy Jr.', 'named')").lastrowid
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        st = c.get("/api/faces/status").json()
        assert st["enabled"] is True and st["references"] == 0
        # uploading a photo builds the reference face
        c.post(f"/api/persons/{warren}/photo", files={"file": ("w.png", _png((220, 40, 40)), "image/png")})
        c.post(f"/api/persons/{kennedy}/photo", files={"file": ("k.png", _png((40, 40, 220)), "image/png")})
        assert c.get("/api/faces/status").json()["references"] == 2
        R, pids = faces.references(conn)
        assert R.shape == (2, 512) and set(pids) == {warren, kennedy}
        # removing the photo removes the reference
        c.delete(f"/api/persons/{kennedy}/photo")
        assert faces.references(conn)[1] == [warren]
        c.post(f"/api/persons/{kennedy}/photo", files={"file": ("k.png", _png((40, 40, 220)), "image/png")})
        # rebuild from scratch
        r = c.post("/api/faces/rebuild").json()
        assert r["built"] == 2 and faces.status(conn)["references"] == 2

        # a recording whose frames show Warren's face while S1 speaks
        sid, spk = seed(conn)
        conn.execute("UPDATE sources SET orig_path=NULL WHERE id=?", (sid,))
        monkeypatch.setattr(suggest, "fetch_metadata", lambda cn, s_: {"title": "Hearing", "description": "", "channel": ""})
        monkeypatch.setattr(suggest, "frames_for", lambda src, times, tmp, progress: [(t, tmp / f"{i}.jpg") for i, t in enumerate(times)])
        def fake_match(cn, frame, refs=None):
            i = int(frame.stem)      # plan order: S0 has two turns -> frames 0,1; S1 -> 2; S2 -> 3
            return [{"person_id": warren, "cos": 0.61, "size": 120}] if i == 2 else ([{"person_id": kennedy, "cos": 0.36, "size": 80}] if i == 3 else [])
        monkeypatch.setattr(faces, "match_frame", fake_match)
        res = suggest.suggest_names(conn, sid)
        assert res["face_hits"] == 2
        hints = {r["label"]: __import__("json").loads(r["name_hints"] or "[]") for r in conn.execute("SELECT label, name_hints FROM source_speakers WHERE source_id=?", (sid,))}
        assert hints["S1"][0]["name"] == "Elizabeth Warren" and any("face on screen matches" in e for e in hints["S1"][0]["evidence"])
        assert hints["S2"] and hints["S2"][0]["name"] == "Robert F. Kennedy Jr." and "resembles" in hints["S2"][0]["evidence"][0]
        # switched off: no face evidence at all
        settings.set("face_match", False)
        with db.tx(conn):
            conn.execute("UPDATE source_speakers SET name_hints=NULL WHERE source_id=?", (sid,))
        res = suggest.suggest_names(conn, sid)
        assert res["face_hits"] == 0

        # Wikipedia portrait (network mocked)
        monkeypatch.setattr(faces, "wikipedia_portrait", lambda name: {"image": _png((200, 30, 30)), "description": "American politician", "title": name, "url": "https://en.wikipedia.org/wiki/x"} if "Warren" in name else None)
        c.delete(f"/api/persons/{warren}/photo")
        r = c.post(f"/api/persons/{warren}/photo/wikipedia").json()
        assert r["ok"] and r["description"] == "American politician" and dossier.photo_paths(warren)[0].exists()
        assert c.post(f"/api/persons/{kennedy}/photo/wikipedia").status_code == 404
        assert c.post("/api/photos/wikipedia_backfill").json()["named"] == 2
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='wiki_photos'").fetchone()[0] == 1
        assert c.post("/api/faces/download").json()["job_id"]


def test_detector_geometry_helpers():
    # similarity transform maps the template onto itself with identity
    M = faces._similarity_transform(faces.TEMPLATE, faces.TEMPLATE)
    assert np.allclose(M, np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32), atol=1e-4)
    # nms keeps the best of overlapping boxes
    kept = faces._nms([{"bbox": [0, 0, 10, 10], "score": .9}, {"bbox": [1, 1, 11, 11], "score": .8}, {"bbox": [50, 50, 60, 60], "score": .7}], 0.4)
    assert len(kept) == 2 and kept[0]["score"] == .9
