"""Self-update, restart and wipe. The launcher (VoiceFlock.bat / voiceflock.sh) runs the server in a
loop and restarts it when it exits with code 3; VOICEFLOCK_SUPERVISED=1 tells us that loop is there."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[1]
RESTART_EXIT_CODE = 3
_update_log: list[str] = []
_update_state = {"running": False, "error": None}


def supervised() -> bool:
    return os.environ.get("VOICEFLOCK_SUPERVISED") == "1"


def _git(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True, timeout=timeout)


def check_update() -> dict:
    out: dict = {"repo": str(REPO), "supervised": supervised(), "git": shutil.which("git") is not None}
    if not out["git"] or not (REPO / ".git").exists():
        out["error"] = "not a git checkout (or git not installed); update by re-downloading"
        return out
    try:
        _git("config", "--global", "--add", "safe.directory", str(REPO).replace("\\", "/"), timeout=10)
        f = _git("fetch", "origin", "main", timeout=45)
        if f.returncode != 0:
            out["error"] = f"git fetch failed: {f.stderr.strip()[:300]}"
            return out
        out["local"] = _git("rev-parse", "--short", "HEAD").stdout.strip()
        out["remote"] = _git("rev-parse", "--short", "origin/main").stdout.strip()
        out["behind"] = int(_git("rev-list", "--count", "HEAD..origin/main").stdout.strip() or 0)
        out["ahead"] = int(_git("rev-list", "--count", "origin/main..HEAD").stdout.strip() or 0)
        out["log"] = [l for l in _git("log", "--pretty=%h %s", "HEAD..origin/main").stdout.strip().splitlines()][:30]
        out["branch"] = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    except subprocess.TimeoutExpired:
        out["error"] = "git timed out (no network?)"
    return out


def apply_update(restart: bool = True) -> None:
    """git pull + pip install, then exit so the launcher restarts us. Runs in a thread; progress in update_status()."""
    if _update_state["running"]:
        return

    def run():
        _update_state.update(running=True, error=None)
        _update_log.clear()
        try:
            _update_log.append("$ git pull --ff-only origin main")
            p = _git("pull", "--ff-only", "origin", "main", timeout=120)
            _update_log.extend((p.stdout + p.stderr).strip().splitlines()[-15:])
            if p.returncode != 0:
                raise RuntimeError("git pull failed")
            _update_log.append("$ pip install -e . (quiet)")
            q = subprocess.run([sys.executable, "-m", "pip", "install", "-e", ".", "--quiet", "--upgrade-strategy", "only-if-needed"],
                               cwd=str(REPO), capture_output=True, text=True, timeout=900)
            _update_log.extend((q.stdout + q.stderr).strip().splitlines()[-10:])
            if q.returncode != 0:
                raise RuntimeError("pip install failed")
            subprocess.run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp", "--quiet"], cwd=str(REPO), capture_output=True, text=True, timeout=300)
            _update_log.append("done" + (" · restarting" if restart else ""))
            if restart:
                time.sleep(1.0)
                restart_server()
        except Exception as e:  # noqa: BLE001
            _update_state["error"] = f"{type(e).__name__}: {e}"
            _update_log.append(f"ERROR {e}")
        finally:
            _update_state["running"] = False

    threading.Thread(target=run, name="voiceflock-update", daemon=True).start()


def update_status() -> dict:
    return {"running": _update_state["running"], "error": _update_state["error"], "log": list(_update_log)}


def restart_server() -> None:
    """Exit with the code the launcher loop restarts on. Without a launcher this just stops the server."""
    log.warning("restart requested (exit %s)", RESTART_EXIT_CODE)
    threading.Timer(0.5, lambda: os._exit(RESTART_EXIT_CODE)).start()


def wipe_library(conn, keep_settings: bool = True) -> dict:
    """Delete every recording, voice, print, package, feed item and the derived files. Settings, calibration
    and downloaded models stay unless keep_settings is False."""
    counts = {}
    with db.tx(conn):
        conn.execute("UPDATE jobs SET status='cancelled' WHERE status IN ('queued','running')")
        for t in ("distinct_pairs", "sightings", "transcript_segments", "matches", "segments", "prints", "source_speakers", "person_groups", "groups", "persons", "feed_items", "sources", "jobs", "cohort"):
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            conn.execute(f"DELETE FROM {t}")
        conn.execute("DELETE FROM feeds")
        if not keep_settings:
            conn.execute("DELETE FROM settings")
            conn.execute("DELETE FROM calibrations")
        conn.execute("DELETE FROM sqlite_sequence")
        db.bump_prints_version(conn)
    for d in (config.AUDIO_DIR, config.ORIG_DIR, config.CLIP_DIR, config.DATA_DIR / "stills", config.DATA_DIR / "transcripts", config.DATA_DIR / "photos"):
        if d.exists():
            for p in d.iterdir():
                try:
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
                except OSError:
                    pass
    conn.execute("VACUUM")
    return counts
