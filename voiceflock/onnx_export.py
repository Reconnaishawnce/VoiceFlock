"""Export the ECAPA speaker encoder to ONNX with feature extraction baked in, so phones and
other non-Python clients compute the *same* embeddings as the laptop.

The wrapper re-implements SpeechBrain's STFT with framing + DFT matmuls (ONNX-friendly), reuses
SpeechBrain's own mel filterbank and ECAPA modules, and applies sentence mean-normalisation.
Input: float32 waveform (1, T) at 16 kHz. Output: L2-normalised embedding (1, 192)."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)
MODEL_FILE = "ecapa_voxceleb.onnx"
META_FILE = "ecapa_voxceleb.onnx.json"


def _build_wrapper(sb_model):
    import torch
    from speechbrain.processing.features import spectral_magnitude

    st = sb_model.mods.compute_features.compute_STFT

    class MatmulSTFT(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.n_fft, self.hop, self.win = st.n_fft, st.hop_length, st.win_length
            assert getattr(st, "pad_mode", "constant") == "constant", "unexpected STFT padding mode"
            w = st.window.detach().clone().float()
            if self.win < self.n_fft:
                pad = (self.n_fft - self.win) // 2
                w = torch.nn.functional.pad(w, (pad, self.n_fft - self.win - pad))
            n = torch.arange(self.n_fft, dtype=torch.float64)
            k = torch.arange(self.n_fft // 2 + 1, dtype=torch.float64)
            ang = 2 * np.pi * torch.outer(n, k) / self.n_fft
            self.register_buffer("cos", (torch.cos(ang) * w.double()[:, None]).float())
            self.register_buffer("sin", (-torch.sin(ang) * w.double()[:, None]).float())

        def forward(self, x):
            p = self.n_fft // 2
            # SpeechBrain's STFT uses center=True with zero ("constant") padding
            x = torch.nn.functional.pad(x, (p, p))
            frames = x.unfold(1, self.n_fft, self.hop)
            return torch.stack([frames @ self.cos, frames @ self.sin], dim=-1)

    class Wrapper(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.stft = MatmulSTFT()
            self.fbanks = sb_model.mods.compute_features.compute_fbanks
            self.emb = sb_model.mods.embedding_model

        def forward(self, wav):
            feats = self.fbanks(spectral_magnitude(self.stft(wav)))
            feats = feats - feats.mean(dim=1, keepdim=True)
            e = self.emb(feats, torch.ones(feats.shape[0])).squeeze(1)
            return e / (e.norm(dim=1, keepdim=True) + 1e-9)

    return Wrapper().eval()


def export(model_name: str | None = None, out_dir: Path | None = None, verify: bool = True) -> dict:
    import torch

    from .embed import Embedder
    out_dir = out_dir or config.MODEL_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    model_name = model_name or config.SETTING_SPECS["embedding_model"]["default"]
    emb = Embedder.get(model_name, "cpu", config.MODEL_DIR)
    sb = emb.model
    sb.eval()
    wrapper = _build_wrapper(sb)
    out = out_dir / MODEL_FILE
    sample = torch.from_numpy((np.random.default_rng(0).normal(size=(1, 48000)) * 0.1).astype(np.float32))
    t0 = time.time()
    prog = torch.onnx.export(wrapper, (sample,), dynamo=True, input_names=["wav"], output_names=["embedding"],
                             dynamic_shapes={"wav": {1: torch.export.Dim("T", min=4000, max=16000 * 600)}})
    prog.save(str(out))
    meta = {"model": model_name, "file": MODEL_FILE, "bytes": out.stat().st_size, "sample_rate": 16000,
            "input": "wav float32 (1, T)", "output": "embedding float32 (1, 192), L2-normalised",
            "export_seconds": round(time.time() - t0, 1), "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if verify:
        with torch.no_grad():
            ref = sb.encode_batch(sample).squeeze(1)
            ref = (ref / ref.norm(dim=1, keepdim=True)).numpy()
            wrapped = wrapper(sample).numpy()
        meta["wrapper_vs_speechbrain_cosine"] = float((ref * wrapped).sum())
        try:
            import onnxruntime as ort
            s = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
            o = s.run(None, {"wav": sample.numpy()})[0]
            meta["onnx_vs_speechbrain_cosine"] = float((ref * o).sum())
            x2 = (np.random.default_rng(1).normal(size=(1, 16000 * 3)) * 0.1).astype(np.float32)
            t1 = time.time()
            s.run(None, {"wav": x2})
            meta["onnx_3s_ms_cpu"] = round((time.time() - t1) * 1000)
        except ImportError:
            meta["onnx_vs_speechbrain_cosine"] = None
        if meta["wrapper_vs_speechbrain_cosine"] < 0.999:
            raise RuntimeError(f"ONNX wrapper diverges from SpeechBrain (cos {meta['wrapper_vs_speechbrain_cosine']:.4f})")
    (out_dir / META_FILE).write_text(json.dumps(meta, indent=1))
    return meta


def model_path(out_dir: Path | None = None) -> Path:
    return (out_dir or config.MODEL_DIR) / MODEL_FILE


def meta(out_dir: Path | None = None) -> dict | None:
    p = (out_dir or config.MODEL_DIR) / META_FILE
    if p.exists() and model_path(out_dir).exists():
        try:
            return json.loads(p.read_text())
        except ValueError:
            return None
    return None
