"""Pipeline logic without the ML models: a fake embedder + synthetic diarization turns."""
import numpy as np

from voiceflock import audio, config, db, gallery, pipeline
from voiceflock.diarize import Turn
from tests.conftest import rand_unit


class FakeEmbedder:
    """Embedding = normalised 'speaker signature' chosen from the chunk's dominant frequency."""

    def __init__(self, rng):
        self.sigs = {}
        self.rng = rng

    def embed_batch(self, chunks, batch_size=16):
        out = []
        for c in chunks:
            spec = np.abs(np.fft.rfft(c[: audio.SR]))
            f = int(np.argmax(spec[1:]) + 1) // 10  # bucket by dominant frequency
            if f not in self.sigs:
                self.sigs[f] = rand_unit(self.rng)
            v = self.sigs[f] + 0.05 * self.rng.normal(size=192).astype(np.float32)
            out.append(v / np.linalg.norm(v))
        return np.stack(out).astype(np.float32)


def tone(freq, seconds, sr=audio.SR):
    t = np.arange(int(seconds * sr)) / sr
    return (0.3 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def two_speaker_wav():
    """A: 0-10s @ 200 Hz, B: 10-18s @ 500 Hz, A again 18-24s."""
    return np.concatenate([tone(200, 10), tone(500, 8), tone(200, 6), np.zeros(audio.SR * 2, np.float32)])


TURNS = [Turn(0, 4, "S0"), Turn(4.5, 10, "S0"), Turn(10, 18, "S1"), Turn(18, 24, "S0")]


def test_group_turns_merges_adjacent():
    g = pipeline.group_turns([Turn(0, 1, "a"), Turn(1.02, 2, "a"), Turn(3, 4, "a"), Turn(0.5, 1.5, "b")])
    assert g["a"] == [(0, 2), (3, 4)]
    assert g["b"] == [(0.5, 1.5)]


def test_analyse_produces_speakers_and_quality(conn):
    settings = config.Settings(conn)
    wav = two_speaker_wav()
    res = pipeline.analyse(wav, TURNS, settings, FakeEmbedder(np.random.default_rng(0)), lambda s, f: None)
    assert [r.label for r in res] == ["S0", "S1"]  # ordered by speech
    a, b = res
    assert abs(a.speech_seconds - 15.5) < 1e-6 and abs(b.speech_seconds - 8) < 1e-6
    assert a.mean_emb.shape == (192,) and abs(np.linalg.norm(a.mean_emb) - 1) < 1e-4
    assert a.chunk_embs.shape[0] >= 2
    assert 0 <= a.quality <= 100 and a.snr_db > 10
    assert float(a.mean_emb @ b.mean_emb) < 0.5


def _run(conn, wav, turns, embedder, mode="bulk", incognito=False, enroll_pid=None, name="rec"):
    settings = config.Settings(conn)
    cur = conn.execute("INSERT INTO sources(name, mode, incognito, enroll_person_id, status) VALUES(?,?,?,?,'processing')",
                       (name, mode, int(incognito), enroll_pid))
    sid = cur.lastrowid
    res = pipeline.analyse(wav, turns, settings, embedder, lambda s, f: None)
    src = db.row(conn, "SELECT * FROM sources WHERE id=?", (sid,))
    pipeline.write_speakers(conn, sid, wav, res, settings, incognito)
    pipeline.decide_and_record(conn, src, res, settings, gallery.GalleryCache())
    return sid, res


def test_open_set_flow_bulk_then_match(conn):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    settings.set("min_enroll_seconds", 5)
    emb = FakeEmbedder(np.random.default_rng(1))
    wav = two_speaker_wav()

    # 1) first bulk scan: empty gallery -> both speakers become unnamed voices
    sid1, res1 = _run(conn, wav, TURNS, emb, name="ep1")
    assert [r.decision for r in res1] == ["new_unknown", "new_unknown"]
    assert conn.execute("SELECT COUNT(*) FROM persons WHERE kind='provisional'").fetchone()[0] == 2
    assert all((config.CLIP_DIR / r.clip_path).exists() for r in res1)

    # 2) same voices again -> clustered onto the existing unnamed voices, prints grow
    sid2, res2 = _run(conn, wav, TURNS, emb, name="ep2")
    assert [r.decision for r in res2] == ["clustered", "clustered"]
    assert res2[0].person_id == res1[0].person_id
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(DISTINCT source_id) FROM prints WHERE person_id=?", (res1[0].person_id,)).fetchone()[0] == 2

    # 3) name speaker A -> becomes 'named'; next scan flags a match but does NOT add prints
    conn.execute("UPDATE persons SET name='Alice', kind='named' WHERE id=?", (res1[0].person_id,))
    db.bump_prints_version(conn)
    n_prints_before = conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (res1[0].person_id,)).fetchone()[0]
    sid3, res3 = _run(conn, wav, TURNS, emb, name="ep3")
    assert res3[0].decision == "matched" and res3[0].person_id == res1[0].person_id
    sg = conn.execute("SELECT kind, name, source_id FROM sightings WHERE person_id=?", (res1[0].person_id,)).fetchall()
    assert len(sg) == 1 and sg[0][0] == "recording" and sg[0][1] == "Alice" and sg[0][2] == sid3   # logged on the shared timeline
    assert res3[0].candidates[0].name == "Alice" and res3[0].candidates[0].probability >= settings["match_threshold"]
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (res1[0].person_id,)).fetchone()[0] == n_prints_before
    m = db.row(conn, "SELECT status FROM matches WHERE source_speaker_id=? AND rank=1", (res3[0].source_speaker_id,))
    assert m["status"] == "auto"

    # 4) incognito: nothing new is written to the gallery
    persons_before = conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0]
    prints_before = conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0]
    wav2 = np.concatenate([tone(900, 12), np.zeros(audio.SR, np.float32)])
    sid4, res4 = _run(conn, wav2, [Turn(0, 12, "S0")], emb, mode="match", incognito=True, name="secret")
    assert res4[0].decision == "incognito"
    assert conn.execute("SELECT COUNT(*) FROM persons").fetchone()[0] == persons_before
    assert conn.execute("SELECT COUNT(*) FROM prints").fetchone()[0] == prints_before
    assert res4[0].clip_path is None

    # 5) a brand-new voice below the cluster threshold in a normal scan becomes a new unknown voice
    sid5, res5 = _run(conn, wav2, [Turn(0, 12, "S0")], emb, name="ep5")
    assert res5[0].decision == "new_unknown"
    assert res5[0].candidates[0].probability < settings["unknown_cluster_threshold"]


def test_enroll_mode_assigns_dominant_speaker(conn):
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    emb = FakeEmbedder(np.random.default_rng(2))
    pid = conn.execute("INSERT INTO persons(name, kind) VALUES('Bob', 'named')").lastrowid
    sid, res = _run(conn, two_speaker_wav(), TURNS, emb, mode="enroll", enroll_pid=pid)
    assert res[0].decision == "enrolled" and res[0].person_id == pid
    assert res[1].decision == "new_unknown"
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=? AND origin='enroll'", (pid,)).fetchone()[0] >= 1
    assert db.row(conn, "SELECT assignment FROM source_speakers WHERE id=?", (res[0].source_speaker_id,))["assignment"] == "enrolled"
