"""End-to-end through the real worker and the real ECAPA model, with diarization faked
(pyannote needs a gated token). Skipped when speechbrain/torch aren't installed."""
import numpy as np
import pytest
import soundfile as sf

pytest.importorskip("torch")
pytest.importorskip("speechbrain")

from voiceflock import audio, config, db, gallery, worker  # noqa: E402
from voiceflock.diarize import Turn  # noqa: E402


def voice(f0, secs, seed, timbre):
    sr = audio.SR
    t = np.arange(int(secs * sr)) / sr
    r = np.random.default_rng(seed)
    x = sum((timbre ** k) * np.sin(2 * np.pi * f0 * k * t * (1 + 0.01 * np.sin(2 * np.pi * (4 + seed % 3) * t))) for k in range(1, 9))
    x = x * (0.5 + 0.5 * np.sin(2 * np.pi * (2.5 + 0.3 * seed) * t) ** 2) + 0.01 * r.normal(size=t.size)
    return (0.3 * x / np.abs(x).max()).astype(np.float32)


def noise_voice(seed, lo, hi, secs=8):
    """Band-limited noise 'voice': clearly distinct from the harmonic ones for ECAPA."""
    r = np.random.default_rng(seed)
    sr = audio.SR
    n = int(secs * sr)
    spec = np.fft.rfft(r.normal(size=n))
    f = np.fft.rfftfreq(n, 1 / sr)
    spec[(f < lo) | (f > hi)] = 0
    x = np.fft.irfft(spec, n)
    t = np.arange(n) / sr
    x *= 0.5 + 0.5 * np.sin(2 * np.pi * (3 + seed % 2) * t) ** 2
    return (0.3 * x / np.abs(x).max()).astype(np.float32)


class FakeDiarizer:
    """Alternating 8 s turns: speaker A, speaker B, A, B ..."""
    name = "fake"

    def run(self, wav, sr, max_speakers=0, progress=None):
        total = len(wav) / sr
        turns, t, i = [], 0.0, 0
        while t < total - 0.5:
            turns.append(Turn(t, min(total, t + 8.0), f"SPEAKER_0{i % 2}"))
            t += 8.0
            i += 1
        return turns


@pytest.fixture()
def fake_diarizer(monkeypatch):
    from voiceflock import pipeline
    monkeypatch.setattr(pipeline.Diarizer, "get", staticmethod(lambda *a, **k: FakeDiarizer()))


def make_file(path, a, b):
    sf.write(str(path), np.concatenate([a, b, a * 0.9, b * 0.9]), audio.SR)


@pytest.mark.slow
def test_full_pipeline_bulk_match_rematch(conn, fake_diarizer, tmp_path):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    settings.set("min_enroll_seconds", 5)
    settings.set("device", "cpu")
    f1, f2, f3 = tmp_path / "ep1.wav", tmp_path / "ep2.wav", tmp_path / "ep3.wav"
    make_file(f1, voice(110, 8, 1, 0.7), voice(230, 8, 2, 0.45))      # speakers A and B
    make_file(f2, voice(110, 8, 7, 0.7), voice(230, 8, 8, 0.45))      # same two voices, different take
    make_file(f3, noise_voice(5, 300, 1200), noise_voice(6, 1500, 4000))  # two new voices

    def ingest(path, mode="bulk", incognito=False):
        sid = conn.execute("INSERT INTO sources(name, original_filename, orig_path, mode, incognito) VALUES(?,?,?,?,?)",
                           (path.stem, path.name, str(path), mode, int(incognito))).lastrowid
        worker.enqueue(conn, sid)
        job = worker.claim_job(conn, "test")
        worker.run_job(conn, job, gcache)
        j = db.row(conn, "SELECT status, error, log FROM jobs WHERE id=?", (job["id"],))
        assert j["status"] == "done", (j["error"], j["log"])
        return sid

    gcache = gallery.GalleryCache()
    s1 = ingest(f1)
    sp1 = db.rows(conn, "SELECT * FROM source_speakers WHERE source_id=? ORDER BY label", (s1,))
    assert len(sp1) == 2 and all(sp["assignment"] == "auto" for sp in sp1)
    assert conn.execute("SELECT COUNT(*) FROM persons WHERE kind='provisional'").fetchone()[0] == 2
    assert all((config.CLIP_DIR / sp["clip_path"]).exists() for sp in sp1)
    assert all(sp["quality_score"] > 20 and sp["snr_db"] >= 0 for sp in sp1)  # synthetic audio has no real pauses
    src = db.row(conn, "SELECT * FROM sources WHERE id=?", (s1,))
    assert src["status"] == "done" and abs(src["duration_s"] - 32) < 0.1 and src["wav_path"]

    # same voices again -> attach to the same unnamed voices
    s2 = ingest(f2)
    sp2 = db.rows(conn, "SELECT * FROM source_speakers WHERE source_id=? ORDER BY label", (s2,))
    assert {sp["person_id"] for sp in sp2} == {sp["person_id"] for sp in sp1}
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 2

    # new voices -> two more unknowns, best candidate well below threshold
    s3 = ingest(f3)
    sp3 = db.rows(conn, "SELECT * FROM source_speakers WHERE source_id=?", (s3,))
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 4
    for sp in sp3:
        top = db.row(conn, "SELECT probability FROM matches WHERE source_speaker_id=? AND rank=1", (sp["id"],))
        assert top["probability"] < settings["unknown_cluster_threshold"]

    # name speaker A -> rematch flags nothing new (already attached) but keeps the alert visible
    a_pid = sp1[0]["person_id"]
    conn.execute("UPDATE persons SET name='Alice', kind='named' WHERE id=?", (a_pid,))
    db.bump_prints_version(conn)
    from voiceflock.rematch import rematch
    rematch(conn, None, gcache)
    alerts = db.rows(conn, "SELECT m.* FROM matches m JOIN persons p ON p.id=m.person_id JOIN source_speakers ss ON ss.id=m.source_speaker_id"
                           " WHERE m.rank=1 AND m.status IN ('auto','confirmed') AND p.kind='named'")
    assert {a["source_speaker_id"] for a in alerts} >= {sp1[0]["id"], sp2[0]["id"]}

    # incognito match of A's voice: alert but no prints / persons added
    before = (conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0], conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0])
    s4 = ingest(f2, mode="match", incognito=True)
    sp4 = db.rows(conn, "SELECT * FROM source_speakers WHERE source_id=? ORDER BY label", (s4,))
    top = db.row(conn, "SELECT m.*, p.name FROM matches m JOIN persons p ON p.id=m.person_id WHERE m.source_speaker_id=? AND m.rank=1", (sp4[0]["id"],))
    assert top["name"] == "Alice" and top["probability"] >= settings["match_threshold"] and top["status"] == "auto"
    assert sp4[0]["person_id"] is None and sp4[0]["clip_path"] is None
    after = (conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0], conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0])
    assert before == after
