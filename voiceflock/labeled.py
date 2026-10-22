"""Read a labelled folder of single-speaker clips and embed them.

Layout:  folder/<speaker name>/<clip>.(wav|mp3|m4a|...)
Optional: folder/pairs.csv with columns file1,file2,label (1 = same speaker) for
explicit verification pairs; otherwise all cross pairs are generated."""
from __future__ import annotations

import csv
import itertools
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio
from .embed import Embedder, embed_file

log = logging.getLogger(__name__)


@dataclass
class Clip:
    speaker: str
    path: Path
    mean: np.ndarray
    chunks: np.ndarray
    seconds: float


def scan_folder(folder: Path) -> list[tuple[str, Path]]:
    items = []
    for spk_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        for f in sorted(spk_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in audio.AUDIO_EXTS:
                items.append((spk_dir.name, f))
    return items


def embed_folder(folder: Path, embedder: Embedder, max_chunk_s: float = 8.0,
                 progress: Callable[[int, int], None] | None = None) -> list[Clip]:
    items = scan_folder(folder)
    if not items:
        raise ValueError(f"no audio found under {folder} (expected <folder>/<speaker>/<clip>)")
    clips = []
    with tempfile.TemporaryDirectory() as td:
        for i, (spk, f) in enumerate(items):
            wav_path = Path(td) / f"{i}.wav"
            audio.to_wav16k(f, wav_path)
            wav = audio.load_wav(wav_path)
            if len(wav) < audio.SR // 2:
                log.warning("skipping %s: shorter than 0.5 s", f)
                continue
            mean, chunks = embed_file(embedder, wav, audio.SR, max_seconds=max_chunk_s)
            clips.append(Clip(spk, f, mean, chunks, len(wav) / audio.SR))
            if progress:
                progress(i + 1, len(items))
    return clips


def pairs_from_clips(clips: list[Clip], folder: Path | None = None,
                     max_nontarget: int = 200_000, seed: int = 0) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(target_pairs, nontarget_pairs) as index pairs into clips. Uses pairs.csv when present."""
    if folder is not None and (folder / "pairs.csv").exists():
        by_path = {str(c.path.resolve()): i for i, c in enumerate(clips)}
        by_rel = {str(c.path.relative_to(folder)): i for i, c in enumerate(clips)}
        tgt, non = [], []
        with open(folder / "pairs.csv", newline="") as fh:
            for row in csv.DictReader(fh):
                a = by_rel.get(row["file1"]) or by_path.get(str((folder / row["file1"]).resolve()))
                b = by_rel.get(row["file2"]) or by_path.get(str((folder / row["file2"]).resolve()))
                if a is None or b is None:
                    continue
                (tgt if str(row["label"]).strip() in ("1", "true", "same") else non).append((a, b))
        return tgt, non
    tgt, non = [], []
    for i, j in itertools.combinations(range(len(clips)), 2):
        (tgt if clips[i].speaker == clips[j].speaker else non).append((i, j))
    if len(non) > max_nontarget:
        rng = np.random.default_rng(seed)
        idx = rng.choice(len(non), size=max_nontarget, replace=False)
        non = [non[k] for k in idx]
    return tgt, non
