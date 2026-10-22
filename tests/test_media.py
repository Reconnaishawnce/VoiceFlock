"""Video input, yt-dlp environment, conversion progress, job cancellation."""
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from voiceflock import audio, db, gallery, worker


def make_mp4(path: Path, seconds: float = 3.0) -> Path:
    cmd = [audio.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=160x120:rate=10:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"ffmpeg cannot build a test mp4 here: {r.stderr[-200:]}")
    return path


def test_mp4_is_converted_to_wav_with_progress(tmp_path):
    mp4 = make_mp4(tmp_path / "clip.mp4")
    ticks = []
    wav = audio.to_wav16k(mp4, tmp_path / "out.wav", progress=ticks.append)
    assert abs(audio.duration_seconds(wav) - 3.0) < 0.2
    data = audio.load_wav(wav)
    assert data.ndim == 1 and np.abs(data).max() > 0.1          # mono, has the sine
    if audio.ffprobe_duration(mp4):
        assert ticks and 0.0 < max(ticks) <= 1.0


def test_video_without_audio_fails_cleanly(tmp_path):
    cmd = [audio.ffmpeg_bin(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp_path / "silent.mp4")]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        pytest.skip("ffmpeg cannot build a test mp4 here")
    with pytest.raises(audio.AudioError):
        audio.to_wav16k(tmp_path / "silent.mp4", tmp_path / "out.wav")


def test_ytdlp_runs_in_process_with_progress(tmp_path, monkeypatch):
    import types
    seen = {}

    class FakeYDL:
        def __init__(self, opts):
            seen["opts"] = opts

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def download(self, urls):
            seen["urls"] = urls
            for h in seen["opts"]["progress_hooks"]:
                h({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 200})
                h({"status": "finished"})
            (tmp_path / "Episode_12 [abc].webm").write_bytes(b"x")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FakeYDL))
    ticks = []
    out = audio.download_url("https://youtube.com/watch?v=abc", tmp_path, progress=ticks.append)
    assert out.name == "Episode_12 [abc].webm" and ticks == [0.25]
    assert seen["urls"] == ["https://youtube.com/watch?v=abc"]
    assert seen["opts"]["format"] == "bestaudio/best" and "postprocessors" not in seen["opts"]
    assert Path(seen["opts"]["ffmpeg_location"]).exists()

    class FailingYDL(FakeYDL):
        def download(self, urls):
            raise RuntimeError("ERROR: Sign in to confirm you're not a bot")

    monkeypatch.setitem(sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=FailingYDL))
    with pytest.raises(audio.AudioError, match="not a bot.*Hint"):
        audio.download_url("https://youtube.com/watch?v=abc", tmp_path)


def test_ytdlp_subprocess_fallback_never_uses_system_python(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # import fails -> subprocess path
    cmd_head = None
    try:
        cmd_head = audio.ytdlp_cmd()
    except audio.AudioError:
        pytest.skip("no yt-dlp executable available for the fallback here")
    assert cmd_head[0] != "python"

    class FakeProc:
        returncode = 0
        stdout = iter(["[download]  12.5% of 10MiB\n", "[download] 100% of 10MiB\n"])

        def wait(self):
            return 0

    calls = []

    def fake_popen(c, **kw):
        calls.append(c)
        (tmp_path / "Episode [abc].m4a").write_bytes(b"x")
        return FakeProc()

    monkeypatch.setattr(audio.subprocess, "Popen", fake_popen)
    ticks = []
    out = audio.download_url("https://example.com/ep", tmp_path, progress=ticks.append)
    assert out.name == "Episode [abc].m4a" and ticks == [0.125, 1.0]
    assert calls[0][-1] == "https://example.com/ep" and "--ffmpeg-location" in calls[0]


def test_cancel_running_job(conn, monkeypatch):
    from voiceflock import pipeline
    sid = conn.execute("INSERT INTO sources(name, mode) VALUES('x', 'bulk')").lastrowid
    jid = worker.enqueue(conn, sid)
    job = worker.claim_job(conn, "t")

    def slow_pipeline(conn_, source_id, progress, gcache):
        progress("diarizing", 0.1)
        conn.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (jid,))   # user presses Stop
        import time
        time.sleep(2.1)
        progress("diarizing", 0.2)                                            # next tick raises
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(pipeline, "process_source", slow_pipeline)
    import voiceflock.worker as w
    monkeypatch.setattr(w, "process_source", slow_pipeline, raising=False)
    worker.run_job(conn, job, gallery.GalleryCache())
    j = db.row(conn, "SELECT * FROM jobs WHERE id=?", (jid,))
    assert j["status"] == "cancelled" and "diarizing" in (j["log"] or "")
    assert db.row(conn, "SELECT status, error FROM sources WHERE id=?", (sid,)) == {"status": "failed", "error": "cancelled"}
