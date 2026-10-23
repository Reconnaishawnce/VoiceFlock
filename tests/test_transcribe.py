import json

import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, db, transcribe
from tests.conftest import rand_unit


def _words(text, s, e):
    toks = text.split(); step = (e - s) / len(toks)
    return [{"s": round(s + i * step, 2), "e": round(s + (i + 1) * step, 2), "w": " " + t, "p": 0.9} for i, t in enumerate(toks)]


HEARING = (
    ("Thank you. The chair now recognizes Senator Warren for five minutes.", 0, 10),
    ("Thank you Mr. Chairman. My name is Elizabeth Warren and I have questions for Mr. Kennedy about vaccines.", 10.5, 30),
    ("Thank you, Senator Warren. Mr. Kennedy, you may respond.", 31, 40),
    ("Thank you Senator. I am Robert Kennedy and I never said that.", 41, 60),
)


def seed(conn):
    rng = np.random.default_rng(0)
    sid = conn.execute("INSERT INTO sources(name, mode, status, duration_s, wav_path) VALUES('hearing', 'bulk', 'done', 60, ?)",
                       (str(config.AUDIO_DIR / "nonexistent.wav"),)).lastrowid
    spk = {}
    for label in ("S0", "S1", "S2"):
        spk[label] = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, embedding) VALUES(?,?,10,2,?)",
                                  (sid, label, db.emb_to_blob(rand_unit(rng)))).lastrowid
    turns = [(0, 10, "S0"), (10.5, 30, "S1"), (31, 40, "S0"), (41, 60, "S2")]
    for s, e, lab in turns:
        conn.execute("INSERT INTO segments(source_speaker_id, start_s, end_s) VALUES(?,?,?)", (spk[lab], s, e))
    return sid, spk


def test_attribution_hints_and_search(data_dir, conn, monkeypatch):
    sid, spk = seed(conn)
    words = [w for text, s, e in HEARING for w in _words(text, s, e)]
    monkeypatch.setattr(transcribe, "run_whisper", lambda path, settings, progress, dur: ("en", words))
    # the fake audio file must exist for the job to start
    config.ensure_dirs(); (config.AUDIO_DIR / "nonexistent.wav").write_bytes(b"RIFF")
    res = transcribe.transcribe_source(conn, sid)
    assert res["language"] == "en" and res["segments"] == 4
    assert transcribe.raw_path(sid).exists()
    lines = db.rows(conn, "SELECT * FROM transcript_segments WHERE source_id=? ORDER BY start_s", (sid,))
    assert [l["source_speaker_id"] for l in lines] == [spk["S0"], spk["S1"], spk["S0"], spk["S2"]]
    assert lines[1]["text"].startswith("Thank you Mr. Chairman. My name is Elizabeth Warren")
    hints = {r["id"]: json.loads(r["name_hints"] or "[]") for r in conn.execute("SELECT id, name_hints FROM source_speakers")}
    assert hints[spk["S1"]][0]["name"] == "Elizabeth Warren"
    assert hints[spk["S2"]][0]["name"] == "Robert Kennedy"
    assert all(h["name"] not in ("Elizabeth Warren", "Robert Kennedy") for h in hints[spk["S0"]])
    # full-text search
    hits = transcribe.search(conn, "vaccines")
    assert len(hits) == 1 and hits[0]["source_id"] == sid and "[vaccines]" in hits[0]["snippet"]
    assert transcribe.search(conn, "never said") and not transcribe.search(conn, "pelosi")

    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        s = c.get(f"/api/sources/{sid}").json()
        assert s["transcript_status"] == "done" and s["transcript_lines"] == 4 and s["transcript_language"] == "en"
        sp1 = next(x for x in s["speakers"] if x["id"] == spk["S1"])
        assert sp1["name_hints"][0]["name"] == "Elizabeth Warren"
        t = c.get(f"/api/sources/{sid}/transcript").json()
        assert len(t["lines"]) == 4 and t["lines"][3]["speaker_id"] == spk["S2"]
        assert len(c.get(f"/api/sources/{sid}/transcript", params={"speaker_id": spk["S0"]}).json()["lines"]) == 2
        h = c.get("/api/transcripts/search", params={"q": "Kennedy"}).json()["hits"]
        assert len(h) == 3 and all(x["source_name"] == "hearing" for x in h)
        assert c.get("/api/stats").json()["transcribed"] == 1
        # re-queue (cached words -> attribution only) and force
        assert c.post(f"/api/sources/{sid}/transcribe").status_code == 200
        assert db.row(conn, "SELECT transcript_status FROM sources WHERE id=?", (sid,))["transcript_status"] == "queued"
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='transcribe' AND status='queued'").fetchone()[0] == 1
        c.post(f"/api/sources/{sid}/transcribe", params={"force": "true"})
        assert not transcribe.raw_path(sid).exists()
        # after a reprocess the speakers change: attribution from cached words follows the new turns
        transcribe.raw_path(sid).write_text(json.dumps({"language": "en", "words": words}))
        new = conn.execute("INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments) VALUES(?, 'S9', 60, 1)", (sid,)).lastrowid
        conn.execute("DELETE FROM segments"); conn.execute("INSERT INTO segments(source_speaker_id, start_s, end_s) VALUES(?, 0, 60)", (new,))
        monkeypatch.setattr(transcribe, "run_whisper", lambda *a: (_ for _ in ()).throw(AssertionError("whisper must not run")))
        transcribe.transcribe_source(conn, sid)
        assert {r[0] for r in conn.execute("SELECT DISTINCT source_speaker_id FROM transcript_segments WHERE source_id=?", (sid,))} == {new}
        # deleting the recording removes transcript rows and the cached words
        c.delete(f"/api/sources/{sid}")
        assert conn.execute("SELECT COUNT(*) FROM transcript_segments").fetchone()[0] == 0 and not transcribe.raw_path(sid).exists()
        assert transcribe.search(conn, "vaccines") == []


def test_pipeline_queues_transcription_when_wanted(data_dir, conn):
    settings = config.Settings(conn)
    settings.set("transcribe_auto", "always")
    assert transcribe.wanted(settings) is True
    settings.set("transcribe_auto", "never")
    assert transcribe.wanted(settings) is False
    name, device, compute = transcribe.pick_model(settings)
    assert device in ("cpu", "cuda") and compute in ("int8", "float16") and name
