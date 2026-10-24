import struct

import numpy as np
from fastapi.testclient import TestClient

from voiceflock import config, db, gallery
from tests.conftest import rand_unit


def _pcm16(seconds=2.0, sr=16000):
    t = np.arange(int(seconds * sr)) / sr
    x = (0.3 * np.sin(2 * np.pi * 180 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))).astype(np.float32)
    return (x * 32767).astype(np.int16).tobytes()


def _wav(pcm: bytes, sr=16000):
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) + b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm


def test_identify_endpoint_with_fake_embedder(data_dir, conn, monkeypatch):
    settings = config.Settings(conn); settings.set("cohort_auto", False)
    rng = np.random.default_rng(0)
    target = rand_unit(rng)
    alice = conn.execute("INSERT INTO persons(name, kind, alert) VALUES('Alice', 'named', 1)").lastrowid
    bob = conn.execute("INSERT INTO persons(name, kind) VALUES('Bob', 'named')").lastrowid
    gallery.add_prints(conn, alice, None, None, target, [], "cli")
    gallery.add_prints(conn, bob, None, None, rand_unit(rng), [], "cli")

    class FakeEmb:
        device = "cpu"
    from voiceflock import embed
    monkeypatch.setattr(embed.Embedder, "get", staticmethod(lambda *a, **k: FakeEmb()))
    monkeypatch.setattr(embed, "embed_file", lambda e, wav, sr, max_seconds=8.0, min_seconds=1.0: (target + 0.01 * rng.standard_normal(192).astype(np.float32), np.stack([target])))
    from voiceflock.api import create_app
    with TestClient(create_app(run_worker=False)) as c:
        r = c.post("/api/identify", files={"audio": ("clip.pcm", _pcm16(), "application/octet-stream")}).json()
        assert r["candidates"][0]["name"] == "Alice" and r["candidates"][0]["probability"] > 0.9 and r["candidates"][0]["alert"] is True
        assert r["seconds"] == 2.0 and r["did"] is None and len(r["embedding"]) > 100
        # a WAV body works too
        r = c.post("/api/identify", files={"audio": ("clip.wav", _wav(_pcm16()), "audio/wav")}).json()
        assert r["candidates"][0]["name"] == "Alice"
        # add as a print, enroll someone new
        before = conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (alice,)).fetchone()[0]
        r = c.post("/api/identify", data={"action": f"add:{alice}"}, files={"audio": ("clip.pcm", _pcm16(), "application/octet-stream")}).json()
        assert r["did"] == {"added_to": alice, "name": "Alice"}
        assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=? AND origin='phone'", (alice,)).fetchone()[0] >= 1
        assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (alice,)).fetchone()[0] > before
        r = c.post("/api/identify", data={"action": "enroll:Carol New"}, files={"audio": ("clip.pcm", _pcm16(), "application/octet-stream")}).json()
        assert r["did"]["name"] == "Carol New" and db.row(conn, "SELECT kind FROM persons WHERE name='Carol New'")["kind"] == "named"
        assert c.post("/api/identify", data={"action": "bogus"}, files={"audio": ("clip.pcm", _pcm16(), "application/octet-stream")}).status_code == 400
        assert c.post("/api/identify", files={"audio": ("clip.pcm", b"\\x00" * 100, "application/octet-stream")}).status_code == 400
        assert conn.execute("SELECT COUNT(*) FROM jobs WHERE type='rematch'").fetchone()[0] >= 1
