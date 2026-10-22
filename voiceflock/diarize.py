"""pyannote speaker diarization wrapper (lazy-loaded singleton).

Works with pyannote.audio 3.x (returns Annotation) and 4.x (returns an object
with .speaker_diarization). Needs a HuggingFace token with the gated model
terms accepted."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


class DiarizationError(RuntimeError):
    pass


def pick_device(pref: str = "auto") -> str:
    import torch
    pref = (pref or "auto").lower()
    if pref != "auto":
        return pref
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Diarizer:
    _instances: dict[tuple[str, str], "Diarizer"] = {}

    def __init__(self, pipeline_name: str, token: str, device: str = "auto", cache_dir=None):
        import torch
        from pyannote.audio import Pipeline

        if not token:
            raise DiarizationError(
                "No HuggingFace token set. Add it in Settings (or HF_TOKEN env) and accept the model terms at "
                f"https://huggingface.co/{pipeline_name}")
        kw = {"cache_dir": str(cache_dir)} if cache_dir else {}
        try:
            pipe = Pipeline.from_pretrained(pipeline_name, token=token, **kw)
        except TypeError:
            pipe = Pipeline.from_pretrained(pipeline_name, use_auth_token=token, **kw)
        if pipe is None:
            raise DiarizationError(
                f"Could not load {pipeline_name}. Accept the terms at https://huggingface.co/{pipeline_name} "
                "(and https://huggingface.co/pyannote/segmentation-3.0) with the same account as your token.")
        self.device = pick_device(device)
        pipe.to(torch.device(self.device))
        self.pipe = pipe
        self.name = pipeline_name

    @classmethod
    def get(cls, pipeline_name: str, token: str, device: str = "auto", cache_dir=None) -> "Diarizer":
        key = (pipeline_name, device)
        inst = cls._instances.get(key)
        if inst is None:
            inst = cls(pipeline_name, token, device, cache_dir)
            cls._instances[key] = inst
        return inst

    def run(self, wav: np.ndarray, sr: int, max_speakers: int = 0,
            progress: Callable[[str, float], None] | None = None) -> list[Turn]:
        import torch

        waveform = torch.from_numpy(np.ascontiguousarray(wav, dtype=np.float32)).unsqueeze(0)
        audio = {"waveform": waveform, "sample_rate": sr}
        kwargs = {}
        if max_speakers and max_speakers > 0:
            kwargs["max_speakers"] = int(max_speakers)

        def hook(step_name, step_artifact=None, file=None, total=None, completed=None, **_):
            if progress and total:
                try:
                    progress(step_name, float(completed) / float(total))
                except Exception:
                    pass

        try:
            out = self.pipe(audio, hook=hook, **kwargs)
        except TypeError:
            out = self.pipe(audio, **kwargs)
        ann = getattr(out, "speaker_diarization", out)
        turns = [Turn(float(seg.start), float(seg.end), str(spk))
                 for seg, _, spk in ann.itertracks(yield_label=True)]
        turns.sort(key=lambda t: t.start)
        return turns
