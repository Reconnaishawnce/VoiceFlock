import sys
import types

import numpy as np
import pytest
from fastapi.testclient import TestClient

from voiceflock import config, gallery, youtube
from tests.conftest import rand_unit

FLAT = {"title": "City of Example", "entries": [
    {"id": "aaaaaaaaaaa", "title": "City Council Regular Meeting 2026-10-01", "duration": 5400, "upload_date": "20261001", "view_count": 120},
    {"id": "bbbbbbbbbbb", "title": "Parks Board", "duration": 1800, "timestamp": 1758000000},
    {"id": "ccccccccccc", "title": "Council Budget Hearing", "duration": 7200},
    {"id": "ddddddddddd", "title": "Short clip", "duration": 45, "upload_date": "20260101"},
    None,
]}


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    calls = {}

    class YDL:
        def __init__(self, opts):
            calls["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def extract_info(self, url, download=False):
            calls["url"] = url
            return FLAT

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=YDL))
    return calls


def test_list_and_filter_videos(fake_ytdlp):
    title, vids = youtube.list_videos("https://www.youtube.com/@CityOfExample")
    assert fake_ytdlp["url"].endswith("/@CityOfExample/videos") and fake_ytdlp["opts"]["extract_flat"] == "in_playlist"
    assert title == "City of Example" and [v.id for v in vids] == ["aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"]
    assert vids[0].url == "https://www.youtube.com/watch?v=aaaaaaaaaaa" and vids[0].date == "2026-10-01"
    assert vids[1].date == "2025-09-16" and vids[2].date is None
    f = youtube.filter_videos(vids, title_regex="council", min_minutes=30)
    assert [v.id for v in f] == ["aaaaaaaaaaa", "ccccccccccc"]
    assert [v.id for v in youtube.filter_videos(vids, date_from="2026-06-01")] == ["aaaaaaaaaaa", "ccccccccccc"]  # undated kept
    assert len(youtube.filter_videos(vids, max_count=2)) == 2
    assert youtube.normalise_channel_url("https://www.youtube.com/playlist?list=PL123") == "https://www.youtube.com/playlist?list=PL123"


def test_youtube_api_list_and_import(data_dir, conn, fake_ytdlp):
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        j = c.post("/api/youtube/list", json={"url": "https://www.youtube.com/@CityOfExample", "min_minutes": 10}).json()
        assert j["title"] == "City of Example" and len(j["videos"]) == 3 and j["total_minutes"] == 240
        r = c.post("/api/youtube/import", json={"videos": j["videos"][:2], "prefix": j["title"]}).json()
        assert len(r["created"]) == 2 and r["skipped"] == 0
        srcs = c.get("/api/sources").json()["sources"]
        assert srcs[0]["name"].startswith("City of Example – ") and srcs[0]["url"].startswith("https://www.youtube.com/watch?v=")
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND priority=-1").fetchone()[0] == 2
        # re-list marks them, re-import skips them
        j2 = c.post("/api/youtube/list", json={"url": "https://www.youtube.com/@CityOfExample"}).json()
        assert sum(1 for v in j2["videos"] if v["already_imported"]) == 2
        r2 = c.post("/api/youtube/import", json={"videos": j2["videos"]}).json()
        assert len(r2["created"]) == 2 and r2["skipped"] == 2


def test_alert_flag_and_mobile_gallery(data_dir, conn):
    rng = np.random.default_rng(0)
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    a = conn.execute("INSERT INTO persons(name, kind) VALUES('Alice', 'named')").lastrowid
    u = conn.execute("INSERT INTO persons(name, kind) VALUES('Unknown Voice 2', 'provisional')").lastrowid
    conn.execute("INSERT INTO persons(name, kind) VALUES('Nobody', 'named')")
    gallery.add_prints(conn, a, None, None, rand_unit(rng), [rand_unit(rng) for _ in range(6)], "cli")
    gallery.add_prints(conn, u, None, None, rand_unit(rng), [], "auto-cluster")
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        p = c.patch(f"/api/persons/{a}", json={"alert": True}).json()
        assert p["alert"] == 1 and c.get(f"/api/persons/{a}").json()["alert"] == 1
        m = c.get("/api/mobile/gallery.json").json()
        assert m["format"] == "voiceflock-mobile" and m["dim"] == 192 and m["match_threshold"] == 0.75
        assert {x["name"] for x in m["people"]} == {"Alice", "Unknown Voice 2"}       # Nobody has no prints
        alice = next(x for x in m["people"] if x["name"] == "Alice")
        assert alice["alert"] is True and len(alice["prints"]) == 4                     # mean + 3 best chunks
        import base64
        e = np.frombuffer(base64.b64decode(alice["prints"][0]), dtype=np.float32)
        assert e.shape == (192,) and abs(np.linalg.norm(e) - 1) < 1e-4
        assert m["calibration"] == {"a": 14.0, "b": -0.34}
        # model not exported yet -> 202 + a queued export job
        r = c.get("/api/mobile/model.onnx")
        assert r.status_code == 202 and r.json()["job_id"]
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='export_model' AND status='queued'").fetchone()[0] == 1
        # live gallery carries the flag
        g = gallery.load_gallery(conn, settings)
        assert g.persons[a]["alert"] is True and g.persons[u]["alert"] is False


@pytest.mark.slow
def test_onnx_export_matches_speechbrain(data_dir):
    pytest.importorskip("torch")
    pytest.importorskip("onnxscript")
    from voiceflock import onnx_export
    meta = onnx_export.export(out_dir=data_dir / "models")
    assert meta["wrapper_vs_speechbrain_cosine"] > 0.999
    if meta.get("onnx_vs_speechbrain_cosine") is not None:
        assert meta["onnx_vs_speechbrain_cosine"] > 0.999
    assert onnx_export.model_path(data_dir / "models").exists() and onnx_export.meta(data_dir / "models")["bytes"] > 10_000_000
