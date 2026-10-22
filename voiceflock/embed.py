"""SpeechBrain ECAPA-TDNN speaker embeddings (lazy-loaded singleton)."""
from __future__ import annotations

import logging

import numpy as np

from . import config
from .diarize import pick_device

log = logging.getLogger(__name__)


class Embedder:
    _instances: dict[tuple[str, str], "Embedder"] = {}

    def __init__(self, model_name: str, device: str = "auto", cache_dir=None):
        try:
            from speechbrain.inference.speaker import EncoderClassifier
        except Exception:  # speechbrain < 1.0
            from speechbrain.pretrained import EncoderClassifier  # type: ignore
        self.device = pick_device(device)
        savedir = (cache_dir or config.MODEL_DIR) / ("speechbrain_" + model_name.replace("/", "_"))
        self.model = EncoderClassifier.from_hparams(source=model_name, savedir=str(savedir),
                                                    run_opts={"device": self.device})
        self.model.eval()
        self.name = model_name

    @classmethod
    def get(cls, model_name: str, device: str = "auto", cache_dir=None) -> "Embedder":
        key = (model_name, device)
        inst = cls._instances.get(key)
        if inst is None:
            inst = cls(model_name, device, cache_dir)
            cls._instances[key] = inst
        return inst

    def embed_batch(self, chunks: list[np.ndarray], batch_size: int = 16) -> np.ndarray:
        """Embed variable-length 16 kHz chunks -> (N, 192) L2-normalised."""
        import torch

        out = []
        order = sorted(range(len(chunks)), key=lambda i: len(chunks[i]))  # similar lengths per batch
        for b in range(0, len(order), batch_size):
            ids = order[b:b + batch_size]
            batch = [chunks[i] for i in ids]
            maxlen = max(len(c) for c in batch)
            x = np.zeros((len(batch), maxlen), dtype=np.float32)
            lens = np.zeros(len(batch), dtype=np.float32)
            for j, c in enumerate(batch):
                x[j, :len(c)] = c
                lens[j] = len(c) / maxlen
            with torch.no_grad():
                e = self.model.encode_batch(torch.from_numpy(x).to(self.device),
                                            torch.from_numpy(lens).to(self.device))
            e = e.squeeze(1).detach().cpu().numpy().astype(np.float32)
            for j, i in enumerate(ids):
                out.append((i, e[j]))
        out.sort(key=lambda t: t[0])
        embs = np.stack([e for _, e in out]) if out else np.zeros((0, config.EMBED_DIM), dtype=np.float32)
        return embs / (np.linalg.norm(embs, axis=1, keepdims=True) + 1e-9)


def make_chunks(wav: np.ndarray, sr: int, segments: list[tuple[float, float]],
                min_seconds: float, max_seconds: float) -> list[tuple[np.ndarray, float, float]]:
    """Cut speaker segments into embedding chunks: skip short ones, split long ones."""
    chunks = []
    for s, e in segments:
        dur = e - s
        if dur < min_seconds:
            continue
        n = max(1, int(np.ceil(dur / max_seconds)))
        step = dur / n
        for i in range(n):
            a, b = s + i * step, s + (i + 1) * step
            if b - a < min_seconds and n > 1:
                continue
            chunks.append((wav[int(a * sr):int(b * sr)], a, b))
    return chunks


def embed_file(embedder: Embedder, wav: np.ndarray, sr: int, max_seconds: float = 8.0,
               min_seconds: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Whole-file embedding (for enrol/calibrate/evaluate on single-speaker clips).
    Returns (mean_embedding, chunk_embeddings)."""
    total = len(wav) / sr
    segs = [(0.0, total)]
    chunks = make_chunks(wav, sr, segs, min(min_seconds, total), max_seconds)
    if not chunks:
        chunks = [(wav, 0.0, total)]
    embs = embedder.embed_batch([c[0] for c in chunks])
    mean = embs.mean(axis=0)
    mean = mean / (np.linalg.norm(mean) + 1e-9)
    return mean, embs
