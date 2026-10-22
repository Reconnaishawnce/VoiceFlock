import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Point VoiceFlock at a temp data dir and a fresh DB."""
    from voiceflock import config
    d = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", d)
    monkeypatch.setattr(config, "DB_PATH", d / "voiceflock.db")
    monkeypatch.setattr(config, "AUDIO_DIR", d / "audio")
    monkeypatch.setattr(config, "ORIG_DIR", d / "originals")
    monkeypatch.setattr(config, "CLIP_DIR", d / "clips")
    monkeypatch.setattr(config, "MODEL_DIR", d / "models")
    for k in ("HF_TOKEN", "VOICEFLOCK_HF_TOKEN", "HUGGINGFACE_HUB_TOKEN", "VOICEFLOCK_DEVICE"):
        monkeypatch.delenv(k, raising=False)
    from voiceflock import db
    db.init_db()
    return d


@pytest.fixture()
def conn(data_dir):
    from voiceflock import db
    c = db.connect()
    yield c
    c.close()


def rand_unit(rng, d=192):
    v = rng.normal(size=d).astype(np.float32)
    return v / np.linalg.norm(v)


def speaker_cluster(rng, centre, n, noise=0.05):
    out = []
    for _ in range(n):
        v = centre + noise * rng.normal(size=centre.shape).astype(np.float32)
        out.append(v / np.linalg.norm(v))
    return out
