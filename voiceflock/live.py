"""Live listening: a rolling window over a microphone stream, embedded every hop and
matched against the gallery. The same maths an always-on phone app would run on-device."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np

from . import audio, config, gallery


@dataclass
class LiveFrame:
    t: float
    level_db: float
    speech: bool
    candidates: list[dict] = field(default_factory=list)
    alert: bool = False

    def as_dict(self) -> dict:
        return {"type": "frame", "t": self.t, "level_db": round(self.level_db, 1), "speech": self.speech,
                "candidates": self.candidates, "alert": self.alert}


class LiveMatcher:
    """Feed int16/float32 16 kHz mono samples; get a LiveFrame back every `hop_s` seconds of audio."""

    def __init__(self, embedder, get_gallery, window_s: float = 3.0, hop_s: float = 1.0,
                 min_level_db: float = -45.0, match_threshold: float = 0.75, k: int = 3, smooth: int = 3):
        self.embedder = embedder
        self.get_gallery = get_gallery
        self.window = int(window_s * audio.SR)
        self.hop = int(hop_s * audio.SR)
        self.min_level_db = min_level_db
        self.match_threshold = match_threshold
        self.k = k
        self.buf = np.zeros(0, dtype=np.float32)
        self.since_hop = 0
        self.t0 = time.time()
        self.samples_seen = 0
        self.history: list[dict[int, float]] = []   # last `smooth` windows: person_id -> probability
        self.smooth = smooth

    @classmethod
    def from_settings(cls, embedder, get_gallery, settings: config.Settings) -> "LiveMatcher":
        return cls(embedder, get_gallery, float(settings["live_window_s"]), float(settings["live_hop_s"]),
                   float(settings["live_min_level_db"]), float(settings["match_threshold"]),
                   k=min(3, int(settings["num_candidates"])))

    def feed(self, samples: np.ndarray) -> list[LiveFrame]:
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        samples = np.asarray(samples, dtype=np.float32).ravel()
        self.buf = np.concatenate([self.buf, samples])[-self.window:]
        self.samples_seen += len(samples)
        self.since_hop += len(samples)
        out = []
        while self.since_hop >= self.hop and len(self.buf) >= min(self.window, audio.SR):
            self.since_hop -= self.hop
            out.append(self._analyse())
        return out

    def _analyse(self) -> LiveFrame:
        t = self.samples_seen / audio.SR
        rms = float(np.sqrt(np.mean(self.buf ** 2) + 1e-12))
        level = 20 * np.log10(rms + 1e-9)
        if level < self.min_level_db:
            self.history = []   # a pause resets smoothing: the next speaker shows up immediately
            return LiveFrame(t, level, False)
        emb = self.embedder.embed_batch([self.buf])[0]
        g = self.get_gallery()
        cands = g.search(emb, k=self.k) if len(g) else []
        self.history.append({c.person_id: c.probability for c in cands})
        self.history = self.history[-self.smooth:]
        # smooth: average over consecutive speech windows (missing = 0) so one noisy window can't flip the name
        merged: dict[int, float] = {}
        for h in self.history:
            for pid, p in h.items():
                merged[pid] = merged.get(pid, 0.0) + p / len(self.history)
        names = {c.person_id: c for c in cands}
        ranked = sorted(merged.items(), key=lambda kv: -kv[1])[: self.k]
        out = []
        for i, (pid, p) in enumerate(ranked):
            c = names.get(pid)
            info = g.persons.get(pid, {"name": f"#{pid}", "kind": "named"})
            nxt = ranked[i + 1][1] if i + 1 < len(ranked) else 0.0
            out.append({"person_id": pid, "name": info["name"], "kind": info["kind"], "probability": round(p, 4),
                        "instant": round(c.probability, 4) if c else 0.0, "margin": round(p - nxt, 4)})
        alert = bool(out) and out[0]["kind"] == "named" and out[0]["probability"] >= self.match_threshold
        return LiveFrame(t, level, True, out, alert)


def glyph(emb: np.ndarray, n: int = 24) -> list[float]:
    """A deterministic 'voiceprint' glyph (n values 0..1) from an embedding, for avatars."""
    emb = gallery.l2n(np.asarray(emb, dtype=np.float32))
    parts = np.array_split(emb, n)
    v = np.array([float(p.sum()) for p in parts])
    v = np.tanh(v * 4.0) * 0.5 + 0.5
    return [round(float(x), 3) for x in v]
