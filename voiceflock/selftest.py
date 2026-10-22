"""Model self-test: load both models and run them on a short synthetic signal so the
first real upload doesn't fail with a cryptic error."""
from __future__ import annotations

import time
import traceback

import numpy as np

from . import audio, config
from .diarize import Diarizer
from .embed import Embedder


def synthetic_dialogue(seconds: float = 8.0) -> np.ndarray:
    """Two alternating synthetic 'voices' (harmonic stacks with vibrato), 16 kHz."""
    sr = audio.SR
    t = np.arange(int(seconds * sr)) / sr

    def voice(f0, timbre):
        x = sum((timbre ** k) * np.sin(2 * np.pi * f0 * k * t * (1 + 0.01 * np.sin(2 * np.pi * 5 * t))) for k in range(1, 8))
        return x * (0.5 + 0.5 * np.sin(2 * np.pi * 3 * t) ** 2)

    a, b = voice(110, 0.7), voice(210, 0.5)
    gate_a = ((t // 2) % 2 == 0).astype(np.float32)
    x = a * gate_a + b * (1 - gate_a) + 0.005 * np.random.default_rng(0).normal(size=t.size)
    return (0.3 * x / np.abs(x).max()).astype(np.float32)


def run(conn, progress=None) -> dict:
    progress = progress or (lambda s, f: None)
    settings = config.Settings(conn)
    out: dict = {"device_setting": settings["device"], "diarization_pipeline": settings["diarization_pipeline"],
                 "embedding_model": settings["embedding_model"], "ok": False}
    wav = synthetic_dialogue()
    try:
        progress("loading embedding model", 0.1)
        t0 = time.time()
        emb = Embedder.get(settings["embedding_model"], settings["device"], config.MODEL_DIR)
        out["embedder_load_s"] = round(time.time() - t0, 1)
        out["device"] = emb.device
        t0 = time.time()
        e = emb.embed_batch([wav[: audio.SR * 3], wav[audio.SR * 3: audio.SR * 6]])
        out["embed_s"] = round(time.time() - t0, 2)
        out["embedding_dim"] = int(e.shape[1])
    except Exception as e:  # noqa: BLE001
        out["embedder_error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()[-2000:]
        return out
    try:
        progress("loading diarization pipeline", 0.4)
        t0 = time.time()
        diar = Diarizer.get(settings["diarization_pipeline"], settings["hf_token"], settings["device"], config.MODEL_DIR)
        out["diarizer_load_s"] = round(time.time() - t0, 1)
        out["diarization_pipeline_loaded"] = diar.name
        progress("diarizing test audio", 0.6)
        t0 = time.time()
        turns = diar.run(wav, audio.SR)
        out["diarize_s"] = round(time.time() - t0, 2)
        out["realtime_factor"] = round((len(wav) / audio.SR) / max(1e-6, time.time() - t0), 1)
        out["n_turns"] = len(turns)
        out["n_speakers"] = len({t.speaker for t in turns})
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        out["diarizer_error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()[-2000:]
    progress("done", 1.0)
    return out
