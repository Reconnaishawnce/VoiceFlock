import json

from fastapi.testclient import TestClient

from voiceflock import config, db, stills
from tests.test_media import make_mp4


def test_storyboard_tile_math():
    fmt = {"fragments": [{"url": "u0", "duration": 50.0}, {"url": "u1", "duration": 50.0}], "rows": 5, "columns": 5, "width": 800, "height": 450}
    assert stills.storyboard_tile(fmt, 0.0) == (0, 0, 0, 160, 90)
    assert stills.storyboard_tile(fmt, 2.0) == (0, 1, 0, 160, 90)        # 2 s per tile -> second tile
    assert stills.storyboard_tile(fmt, 49.9) == (0, 4, 4, 160, 90)       # last tile of fragment 0
    assert stills.storyboard_tile(fmt, 75.0) == (1, 2, 2, 160, 90)       # 25 s into fragment 1 -> tile 12 -> col 2 row 2
    assert stills.storyboard_tile(fmt, 999.0)[0] == 1                    # clamped to the last fragment
    assert stills.storyboard_tile({"fragments": [{"url": "u"}], "rows": 1, "columns": 1}, 1.0) is None


def test_video_stills_for_source(data_dir, conn, tmp_path):
    mp4 = make_mp4(tmp_path / "hearing.mp4", seconds=6.0)
    sid = conn.execute("INSERT INTO sources(name, orig_path, mode, status, duration_s) VALUES('hearing', ?, 'bulk', 'done', 6)", (str(mp4),)).lastrowid
    a = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments) VALUES(?, 'S0', 3, 2)", (sid,)).lastrowid
    b = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments) VALUES(?, 'S1', 2, 1)", (sid,)).lastrowid
    conn.executemany("INSERT INTO segments(source_speaker_id, start_s, end_s) VALUES(?,?,?)", [(a, 0.0, 2.0), (a, 3.0, 4.0), (b, 4.0, 6.0)])
    res = stills.make_stills_for_source(conn, sid)
    assert res["kind"] == "video" and res["speakers"] == 2 and res["stills"] == 3   # a: 2 turns, b: 1 (max 3 per speaker)
    st = json.loads(db.row(conn, "SELECT stills FROM source_speakers WHERE id=?", (a,))["stills"])
    assert [x["t"] for x in st] == [1.0, 3.5] and all((stills.still_dir() / x["file"]).stat().st_size > 500 for x in st)
    assert max((stills.still_dir() / x["file"]).stat().st_size for x in st) < 60_000   # low-res
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        src = c.get(f"/api/sources/{sid}").json()
        sp = next(x for x in src["speakers"] if x["label"] == "S0")
        assert sp["stills"][0]["url"].startswith("/stills/") and c.get(sp["stills"][0]["url"]).status_code == 200
        # person_sources carries the first still
        pid = conn.execute("INSERT INTO persons(name, kind) VALUES('X', 'named')").lastrowid
        conn.execute("UPDATE source_speakers SET person_id=?, assignment='named' WHERE id=?", (pid, a))
        h = c.get(f"/api/persons/{pid}").json()["heard_in"][0]
        assert h["still_url"] == sp["stills"][0]["url"] and h["media_kind"] == "video" and h["watch_at"] == 0.0
        # backfill finds nothing left to do for this one; stats reports storage
        conn.execute("UPDATE source_speakers SET stills=NULL WHERE id=?", (b,))
        r = c.post("/api/stills/backfill").json()
        assert r["queued"] == 1 and r["estimated_mb"] >= 0
        assert c.get("/api/stats").json()["stills_mb"] >= 0
        # disabled setting -> nothing made
        config.Settings(conn).set("stills_per_speaker", 0)
        assert stills.make_stills_for_source(conn, sid)["kind"] == "disabled"
        # deleting the source removes the files
        assert c.delete(f"/api/sources/{sid}").status_code == 200
        assert not list(stills.still_dir().glob(f"{sid}_*.jpg"))


def test_storyboard_crop_path(tmp_path):
    """Exercise the sprite-sheet crop with a local sheet (YouTube itself blocks datacenter IPs)."""
    import subprocess
    from voiceflock import audio
    sheet = tmp_path / "sheet.jpg"
    r = subprocess.run([audio.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=800x450:rate=1", "-frames:v", "1", str(sheet)], capture_output=True)
    if r.returncode != 0:
        import pytest
        pytest.skip("ffmpeg cannot synthesise a sheet here")
    fmt = {"fragments": [{"url": sheet.as_uri(), "duration": 50.0}], "rows": 5, "columns": 5, "width": 800, "height": 450}
    out = tmp_path / "tile.jpg"
    cache = {}
    assert stills.storyboard_frame(fmt, 27.0, out, cache, tmp_path) is True
    assert out.exists() and 300 < out.stat().st_size < 30_000 and 0 in cache
    # second call reuses the cached sheet
    assert stills.storyboard_frame(fmt, 48.0, tmp_path / "tile2.jpg", cache, tmp_path) is True
    probe = subprocess.run([str(__import__("pathlib").Path(audio.ffmpeg_bin()).with_name("ffprobe")), "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height", "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    if probe.returncode == 0:
        assert probe.stdout.strip() == "160,90"
