"""Live listening: score the microphone against the gallery every second.

Instead of a fixed time window (which is diluted by pauses), we keep only *speech* frames:
100 ms frames whose level is above an adaptive floor (the quiet part of the last 10 s plus a
margin). The last `window_s` seconds of speech are embedded every `hop_s`; a pause longer than
2 s clears the buffer so the next speaker starts fresh. Same maths as the phone app."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import audio, config, gallery

FRAME_S = 0.1
MIN_SPEECH_S = 1.0
PAUSE_RESET_S = 2.0
FLOOR_MARGIN_DB = 8.0


@dataclass
class LiveFrame:
    t: float
    level_db: float
    speech: bool
    speech_s: float = 0.0
    candidates: list[dict] = field(default_factory=list)
    alert: bool = False

    def as_dict(self) -> dict:
        return {"type": "frame", "t": round(self.t, 2), "level_db": round(self.level_db, 1), "speech": self.speech,
                "speech_s": round(self.speech_s, 1), "candidates": self.candidates, "alert": self.alert}


class LiveMatcher:
    """Feed int16/float32 16 kHz mono samples; get a LiveFrame back every `hop_s` seconds of audio."""

    def __init__(self, embedder, get_gallery, window_s: float = 4.0, hop_s: float = 1.0,
                 min_level_db: float = -45.0, match_threshold: float = 0.75, k: int = 3, smooth: int = 3):
        self.embedder = embedder
        self.get_gallery = get_gallery
        self.window_s = window_s
        self.hop = int(hop_s * audio.SR)
        self.frame = int(FRAME_S * audio.SR)
        self.min_level_db = min_level_db
        self.match_threshold = match_threshold
        self.k = k
        self.smooth = smooth
        self.pending = np.zeros(0, dtype=np.float32)
        self.levels: deque[float] = deque(maxlen=int(10 / FRAME_S))
        self.speech: deque[np.ndarray] = deque()
        self.since_hop = 0
        self.since_speech = 0.0
        self.samples_seen = 0
        self.last_level = -100.0
        self.history: list[dict[int, float]] = []
        self.t0 = time.time()

    @classmethod
    def from_settings(cls, embedder, get_gallery, settings: config.Settings) -> "LiveMatcher":
        return cls(embedder, get_gallery, float(settings["live_window_s"]), float(settings["live_hop_s"]),
                   float(settings["live_min_level_db"]), float(settings["match_threshold"]),
                   k=min(3, int(settings["num_candidates"])))

    def threshold_db(self) -> float:
        if len(self.levels) < 10:
            return self.min_level_db
        return max(self.min_level_db, float(np.percentile(self.levels, 20)) + FLOOR_MARGIN_DB)

    def feed(self, samples: np.ndarray) -> list[LiveFrame]:
        if samples.dtype == np.int16:
            samples = samples.astype(np.float32) / 32768.0
        self.pending = np.concatenate([self.pending, np.asarray(samples, dtype=np.float32).ravel()])
        out = []
        while len(self.pending) >= self.frame:
            fr, self.pending = self.pending[: self.frame], self.pending[self.frame:]
            self.samples_seen += self.frame
            self.since_hop += self.frame
            level = float(20 * np.log10(np.sqrt(np.mean(fr ** 2) + 1e-12) + 1e-9))
            self.last_level = level
            thr = self.threshold_db()
            self.levels.append(level)
            if level > thr:
                self.speech.append(fr)
                self.since_speech = 0.0
                while len(self.speech) * FRAME_S > self.window_s:
                    self.speech.popleft()
            else:
                self.since_speech += FRAME_S
                if self.since_speech >= PAUSE_RESET_S and self.speech:
                    self.speech.clear()
                    self.history = []
            if self.since_hop >= self.hop:
                self.since_hop = 0
                out.append(self._analyse())
        return out

    def _analyse(self) -> LiveFrame:
        t = self.samples_seen / audio.SR
        speech_s = len(self.speech) * FRAME_S
        if speech_s < MIN_SPEECH_S or self.since_speech >= PAUSE_RESET_S:
            if self.since_speech >= PAUSE_RESET_S:
                self.history = []
            return LiveFrame(t, self.last_level, False, speech_s)
        emb = self.embedder.embed_batch([np.concatenate(list(self.speech))])[0]
        g = self.get_gallery()
        cands = g.search(emb, k=self.k) if len(g) else []
        self.history.append({c.person_id: c.probability for c in cands})
        self.history = self.history[-self.smooth:]
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
            out.append({"person_id": pid, "name": info["name"], "kind": info["kind"], "flagged": bool(info.get("alert")),
                        "alert_sound": info.get("alert_sound"), "alert_message": info.get("alert_message"),
                        "probability": round(p, 4), "instant": round(c.probability, 4) if c else 0.0, "margin": round(p - nxt, 4)})
        alert = bool(out) and (out[0]["kind"] == "named" or out[0]["flagged"]) and out[0]["probability"] >= self.match_threshold
        return LiveFrame(t, self.last_level, True, speech_s, out, alert)


def glyph(emb: np.ndarray, n: int = 24) -> list[float]:
    """A deterministic 'voiceprint' glyph (n values 0..1) from an embedding, for avatars."""
    emb = gallery.l2n(np.asarray(emb, dtype=np.float32))
    parts = np.array_split(emb, n)
    v = np.array([float(p.sum()) for p in parts])
    v = np.tanh(v * 4.0) * 0.5 + 0.5
    return [round(float(x), 3) for x in v]
