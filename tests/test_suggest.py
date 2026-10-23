import json

from fastapi.testclient import TestClient

from voiceflock import config, db, suggest, transcribe
from tests.test_transcribe import HEARING, _words, seed


def test_caption_and_text_name_cleaning():
    assert suggest.clean_caption_name("SEN.ELIZABETH WARREN (D-MA)") == "Elizabeth Warren"
    assert suggest.clean_caption_name("Robert F. Kennedy Jr.") == "Robert F. Kennedy Jr."
    assert suggest.clean_caption_name("REP. JIM JORDAN - OHIO") == "Jim Jordan"
    assert suggest.clean_caption_name("SECRETARY OF STATE") is None
    assert suggest.clean_caption_name("Breaking News") is None
    assert suggest.clean_caption_name("Committee") is None
    names = suggest.names_in_text("RFK Jr. hearing: Senator Elizabeth Warren questions the nominee. Chair Bill Cassidy presides.")
    assert names[:2] == ["Elizabeth Warren", "Bill Cassidy"]
    p = suggest.parse_llm('{"speakers":[{"label":"S1","name":"ELIZABETH WARREN","confidence":"0.9","why":"caption"},{"label":"S0","name":null,"confidence":0.1,"why":""}]}')
    assert p["S1"] == {"name": "Elizabeth Warren", "confidence": 0.9, "why": "caption"} and p["S0"]["name"] is None
    assert suggest.parse_llm("no json here") == {}


def test_suggest_pipeline_with_fake_ocr_and_llm(data_dir, conn, monkeypatch):
    sid, spk = seed(conn)
    conn.execute("UPDATE sources SET url='https://www.youtube.com/watch?v=abcdefghijk', orig_path=NULL WHERE id=?", (sid,))
    # transcript hints first (from the transcript module)
    words = [w for text, s, e in HEARING for w in _words(text, s, e)]
    with db.tx(conn):
        transcribe.attribute(conn, sid, words)
    settings = config.Settings(conn)
    settings.set("llm_provider", "ollama")
    # fake metadata, frames, OCR and LLM
    monkeypatch.setattr(suggest, "fetch_metadata", lambda c, s: {"title": "Kennedy confirmation hearing: Warren vs RFK Jr.", "description": "Senator Elizabeth Warren questions Robert F. Kennedy Jr.", "channel": "C-SPAN"})
    monkeypatch.setattr(suggest, "frames_for", lambda src, times, tmp, progress: [(t, tmp / f"{i}.jpg") for i, t in enumerate(times)])
    def fake_ocr(frame):
        # the frame index maps back to the plan order: S0 (chair), S1 (Warren), S2 (Kennedy), two frames each
        i = int(frame.stem)
        return [[("SEN. BILL CASSIDY (R-LA)", 0.95), ("Chairman", 0.9)], [("SEN.ELIZABETH WARREN", 0.93)], [("ROBERT F. KENNEDY JR.", 0.91), ("Nominee for HHS Secretary", 0.8)]][i // 2]
    monkeypatch.setattr(suggest, "ocr_lower_third", fake_ocr)
    monkeypatch.setattr(suggest, "ocr_available", lambda: True)
    asked = {}
    def fake_llm(settings, payload):
        asked["payload"] = payload
        return {"S0": {"name": "Bill Cassidy", "confidence": 0.8, "why": "caption and chairs the hearing"},
                "S1": {"name": "Elizabeth Warren", "confidence": 0.95, "why": "introduces herself"},
                "S2": {"name": "Robert F. Kennedy Jr.", "confidence": 0.9, "why": "caption, addressed as Mr. Kennedy"}}
    monkeypatch.setattr(suggest, "ask_llm", fake_llm)
    res = suggest.suggest_names(conn, sid)
    assert res["speakers"] == 3 and res["with_suggestions"] == 3 and res["llm"] == "ollama" and res["ocr_frames"] >= 3
    assert "VOICE S1" in asked["payload"] and "on-screen captions" in asked["payload"] and "Kennedy confirmation hearing" in asked["payload"]
    hints = {r["label"]: json.loads(r["name_hints"]) for r in conn.execute("SELECT label, name_hints FROM source_speakers WHERE source_id=?", (sid,))}
    assert hints["S1"][0]["name"] == "Elizabeth Warren" and hints["S2"][0]["name"] == "Robert F. Kennedy Jr." and hints["S0"][0]["name"] == "Bill Cassidy"
    ev = " ".join(hints["S1"][0]["evidence"])
    assert "on-screen caption" in ev and "language model" in ev and "transcript" in ev and "title or description" in ev
    assert hints["S1"][0]["score"] > hints["S1"][-1]["score"] if len(hints["S1"]) > 1 else True
    # a second run (e.g. after a re-transcribe) starts from the transcript evidence, not from its own previous output
    suggest.suggest_names(conn, sid)
    hints2 = {r["label"]: json.loads(r["name_hints"]) for r in conn.execute("SELECT label, name_hints FROM source_speakers WHERE source_id=?", (sid,))}
    assert abs(hints2["S1"][0]["score"] - hints["S1"][0]["score"]) < 0.01

    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        s = c.get(f"/api/sources/{sid}").json()
        sp1 = next(x for x in s["speakers"] if x["label"] == "S1")
        assert sp1["name_hints"][0]["name"] == "Elizabeth Warren" and sp1["name_hints"][0]["evidence"]
        assert "description" not in s
        assert c.post(f"/api/sources/{sid}/suggest").status_code == 200
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='suggest_names' AND status='queued'").fetchone()[0] == 1
        st = c.get("/api/suggest/status").json()
        assert st["provider"] == "ollama" and "reachable" in st
        assert c.post("/api/suggest/backfill").json()["queued"] == 1


def test_llm_status_without_provider(data_dir, conn):
    settings = config.Settings(conn)
    settings.set("llm_provider", "none")
    st = suggest.llm_status(settings)
    assert st["provider"] == "none" and isinstance(st["ocr"], bool)
    settings.set("llm_provider", "anthropic")
    assert suggest.llm_status(settings)["key_set"] is False
