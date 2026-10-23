"""Paths, defaults and the settings table.

Settings live in SQLite so the web UI can edit them. Environment variables
override a few of them (HF token, device, data dir) so Docker deployments
can be configured without touching the UI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("VOICEFLOCK_DATA_DIR", "./data")).resolve()
DB_PATH = DATA_DIR / "voiceflock.db"
AUDIO_DIR = DATA_DIR / "audio"      # converted 16 kHz mono wavs
ORIG_DIR = DATA_DIR / "originals"   # uploaded files as received
CLIP_DIR = DATA_DIR / "clips"       # short per-speaker clips for the UI
MODEL_DIR = DATA_DIR / "models"     # HF / speechbrain caches

EMBED_DIM = 192  # ECAPA-TDNN (speechbrain/spkrec-ecapa-voxceleb)

# Every setting has a default, a type and a short description (shown in UI).
SETTING_SPECS: dict[str, dict[str, Any]] = {
    # --- matching ---
    "match_threshold": {"default": 0.75, "type": "float", "group": "Matching",
        "label": "Match threshold",
        "help": "Calibrated probability needed to call a speaker a match. Below this the speaker is 'unknown'."},
    "confirm_threshold": {"default": 0.90, "type": "float", "group": "Matching",
        "label": "High-confidence threshold",
        "help": "The UI reminds you to only Confirm matches at or above this probability."},
    "unknown_cluster_threshold": {"default": 0.80, "type": "float", "group": "Matching",
        "label": "Unknown-voice clustering threshold",
        "help": "Probability needed to attach a new recording to an existing unnamed (provisional) voice."},
    "num_candidates": {"default": 5, "type": "int", "group": "Matching",
        "label": "Candidates per speaker", "help": "How many top candidates to show per speaker."},
    "save_unknown_voices": {"default": True, "type": "bool", "group": "Matching",
        "label": "Save unknown voices",
        "help": "When a speaker matches nobody, keep it as an unnamed voice so future recordings can match it."},
    "incognito_default": {"default": False, "type": "bool", "group": "Matching",
        "label": "Incognito by default",
        "help": "Incognito uploads are matched but never added to the voice database."},
    # --- quality gates ---
    "min_speech_seconds": {"default": 3.0, "type": "float", "group": "Quality",
        "label": "Min speech per speaker (s)",
        "help": "Diarized speakers with less speech than this are ignored."},
    "min_enroll_seconds": {"default": 6.0, "type": "float", "group": "Quality",
        "label": "Min speech to save a new unknown voice (s)",
        "help": "Shorter speakers are matched but not saved as new unnamed voices."},
    "min_segment_seconds": {"default": 0.8, "type": "float", "group": "Quality",
        "label": "Min segment length (s)", "help": "Diarization turns shorter than this are skipped for embedding."},
    "max_chunk_seconds": {"default": 8.0, "type": "float", "group": "Quality",
        "label": "Max chunk length (s)", "help": "Longer turns are split into chunks of this length before embedding."},
    "max_prints_per_speaker": {"default": 12, "type": "int", "group": "Quality",
        "label": "Max prints kept per recording-speaker",
        "help": "Chunk embeddings stored per speaker per recording (best quality first), plus the mean embedding."},
    "max_prints_per_person": {"default": 60, "type": "int", "group": "Quality",
        "label": "Max prints per person", "help": "Oldest auto-clustered prints are pruned beyond this."},
    "stills_per_speaker": {"default": 3, "type": "int", "group": "Quality",
        "label": "Video stills per speaker",
        "help": "Low-res frames (about 15 KB each) at the speaker's longest turns, from uploaded videos or YouTube's preview storyboards. 3 stills × 30 speakers ≈ 1.4 MB per hearing. 0 disables."},
    "still_width": {"default": 320, "type": "int", "group": "Quality",
        "label": "Still width (px)", "help": "For frames cut from uploaded videos. YouTube storyboard tiles are whatever YouTube serves (≈160-320 px)."},
    "clip_seconds": {"default": 8.0, "type": "float", "group": "Quality",
        "label": "Preview clip length (s)", "help": "Length of the per-speaker preview clip shown in the UI."},
    # --- score normalisation / calibration ---
    "cohort_topk": {"default": 200, "type": "int", "group": "Scoring",
        "label": "Adaptive s-norm cohort top-k",
        "help": "Number of closest cohort embeddings used for adaptive score normalisation."},
    "cohort_min_size": {"default": 20, "type": "int", "group": "Scoring",
        "label": "Min cohort size for s-norm",
        "help": "Below this many cohort embeddings, raw cosine scores are used instead of s-norm."},
    "cohort_auto": {"default": True, "type": "bool", "group": "Scoring",
        "label": "Auto-build cohort from gallery",
        "help": "Use one mean embedding per known/unnamed voice as cohort, in addition to the explicit cohort set."},
    "calib_raw_a": {"default": 14.0, "type": "float", "group": "Scoring",
        "label": "Default calibration slope (raw cosine)", "help": "p = sigmoid(a*(score+b)) until you run `calibrate`."},
    "calib_raw_b": {"default": -0.34, "type": "float", "group": "Scoring",
        "label": "Default calibration offset (raw cosine)", "help": ""},
    "calib_snorm_a": {"default": 1.4, "type": "float", "group": "Scoring",
        "label": "Default calibration slope (s-norm)", "help": "p = sigmoid(a*(score+b)) until you run `calibrate`."},
    "calib_snorm_b": {"default": -3.6, "type": "float", "group": "Scoring",
        "label": "Default calibration offset (s-norm)", "help": ""},
    # --- alerts ---
    "alert_default_message": {"default": "Attention. [NAME], your voice was detected nearby. It's time to regulate mass surveillance.",
        "type": "str", "group": "Alerts", "label": "Default spoken alert",
        "help": "Spoken (text-to-speech) when a flagged voice is heard. [NAME] is replaced with the person's name. Per-person messages override this."},
    "alert_default_sound": {"default": "siren", "type": "str", "group": "Alerts",
        "label": "Default alarm sound", "help": "siren | klaxon | chime | none"},
    "library_export_path": {"default": "", "type": "str", "group": "Alerts",
        "label": "Auto-export phone library to",
        "help": "A file or folder, e.g. C:\\Users\\you\\Dropbox\\VoiceFlock. The library JSON is rewritten there whenever voices or alerts change; share that file's link and the phone fetches it on its own."},
    "alert_speak_on_laptop": {"default": True, "type": "bool", "group": "Alerts",
        "label": "Laptop Live mode plays alarm + speaks", "help": "Uses the browser's speech engine; the phone always does."},
    # --- live listening ---
    "live_window_s": {"default": 4.0, "type": "float", "group": "Live",
        "label": "Live window (s of speech)", "help": "Seconds of actual speech (pauses skipped) embedded at a time in Live mode."},
    "live_hop_s": {"default": 1.0, "type": "float", "group": "Live",
        "label": "Live update interval (s)", "help": "How often Live mode re-scores."},
    "live_min_level_db": {"default": -45.0, "type": "float", "group": "Live",
        "label": "Live silence floor (dBFS)", "help": "Frames quieter than this are never speech; the floor also adapts to room noise."},
    # --- models ---
    "hf_token": {"default": "", "type": "secret", "group": "Models",
        "label": "HuggingFace token",
        "help": "Needed for the gated pyannote models. Accept the model terms on huggingface.co first."},
    "diarization_pipeline": {"default": "pyannote/speaker-diarization-community-1", "type": "str", "group": "Models",
        "label": "Diarization pipeline",
        "help": "Fallback: pyannote/speaker-diarization-3.1"},
    "embedding_model": {"default": "speechbrain/spkrec-ecapa-voxceleb", "type": "str", "group": "Models",
        "label": "Speaker embedding model", "help": "SpeechBrain ECAPA-TDNN."},
    "device": {"default": "auto", "type": "str", "group": "Models",
        "label": "Device", "help": "auto | cpu | cuda | mps"},
    "max_speakers": {"default": 0, "type": "int", "group": "Models",
        "label": "Max speakers hint (0 = auto)", "help": "Upper bound passed to the diarizer; 0 lets it decide."},
}

ENV_OVERRIDES = {
    "hf_token": ("VOICEFLOCK_HF_TOKEN", "HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"),
    "device": ("VOICEFLOCK_DEVICE",),
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, AUDIO_DIR, ORIG_DIR, CLIP_DIR, MODEL_DIR):
        d.mkdir(parents=True, exist_ok=True)


def coerce(key: str, value: Any) -> Any:
    spec = SETTING_SPECS[key]
    t = spec["type"]
    if t == "float":
        return float(value)
    if t == "int":
        return int(float(value))
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)
    return "" if value is None else str(value)


class Settings:
    """Dict-like view over the settings table with defaults and env overrides."""

    def __init__(self, conn):
        self.conn = conn

    def get(self, key: str) -> Any:
        spec = SETTING_SPECS[key]
        for env in ENV_OVERRIDES.get(key, ()):
            v = os.environ.get(env)
            if v:
                return coerce(key, v)
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return spec["default"]
        try:
            return coerce(key, json.loads(row[0]))
        except (ValueError, TypeError):
            return spec["default"]

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def set(self, key: str, value: Any) -> None:
        if key not in SETTING_SPECS:
            raise KeyError(key)
        value = coerce(key, value)
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def all(self, mask_secrets: bool = True) -> dict[str, Any]:
        out = {}
        for k, spec in SETTING_SPECS.items():
            v = self.get(k)
            if mask_secrets and spec["type"] == "secret" and v:
                v = "•••••" + str(v)[-4:]
            out[k] = v
        return out

    def specs(self) -> dict[str, dict[str, Any]]:
        return {k: {kk: vv for kk, vv in s.items()} for k, s in SETTING_SPECS.items()}
