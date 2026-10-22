"""End-to-end processing of one source:

  convert -> diarize -> per-speaker embeddings + quality -> preview clips
  -> gallery search (top-k candidates, calibrated prob, margin)
  -> decide: match known person / attach to unnamed voice / create unnamed voice / incognito

Everything the UI shows comes from the rows written here."""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio, config, db, gallery
from .diarize import Diarizer, Turn
from .embed import Embedder, make_chunks

log = logging.getLogger(__name__)

Progress = Callable[[str, float], None]


@dataclass
class SpeakerResult:
    label: str
    segments: list[tuple[float, float]]
    speech_seconds: float
    mean_emb: np.ndarray | None = None
    chunk_embs: np.ndarray | None = None
    chunk_quality: list[float] = field(default_factory=list)
    snr_db: float = 0.0
    clip_frac: float = 0.0
    quality: float = 0.0
    clip_path: str | None = None
    source_speaker_id: int | None = None
    candidates: list[gallery.Candidate] = field(default_factory=list)
    decision: str = "unknown"      # matched | clustered | new_unknown | unknown | incognito | enrolled
    person_id: int | None = None


def group_turns(turns: list[Turn]) -> dict[str, list[tuple[float, float]]]:
    by: dict[str, list[tuple[float, float]]] = {}
    for t in turns:
        by.setdefault(t.speaker, []).append((t.start, t.end))
    # merge touching/overlapping segments of the same speaker
    out = {}
    for spk, segs in by.items():
        segs.sort()
        merged: list[list[float]] = []
        for s, e in segs:
            if merged and s <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        out[spk] = [(s, e) for s, e in merged]
    return out


def prepare_source(conn: sqlite3.Connection, source: dict, progress: Progress) -> Path:
    """Make sure the source has a 16 kHz wav; download URL first if needed."""
    sid = source["id"]
    orig = Path(source["orig_path"]) if source["orig_path"] else None
    if orig is None or not orig.exists():
        if source["url"]:
            progress("downloading", 0.0)
            out_dir = config.ORIG_DIR / f"url_{sid}"
            orig = audio.download_url(source["url"], out_dir)
            conn.execute("UPDATE sources SET orig_path=?, original_filename=?, name=COALESCE(NULLIF(name,''), ?) WHERE id=?",
                         (str(orig), orig.name, orig.stem, sid))
            if not source["name"] or source["name"] == source["url"]:
                conn.execute("UPDATE sources SET name=? WHERE id=?", (orig.stem, sid))
        else:
            raise audio.AudioError("source file is missing")
    wav_path = config.AUDIO_DIR / f"{sid}.wav"
    if not wav_path.exists():
        progress("converting", 0.0)
        audio.to_wav16k(orig, wav_path)
    dur = audio.duration_seconds(wav_path)
    sha = source["sha256"] or audio.sha256_file(orig)
    conn.execute("UPDATE sources SET wav_path=?, duration_s=?, sha256=? WHERE id=?", (str(wav_path), dur, sha, sid))
    return wav_path


def analyse(wav: np.ndarray, turns: list[Turn], settings: config.Settings, embedder: Embedder,
            progress: Progress) -> list[SpeakerResult]:
    """Per-speaker embeddings and quality from diarization turns."""
    min_speech = float(settings["min_speech_seconds"])
    min_seg = float(settings["min_segment_seconds"])
    max_chunk = float(settings["max_chunk_seconds"])
    energy_db, hop_s = audio.frame_energy(wav)

    results = []
    grouped = group_turns(turns)
    all_speech = [seg for segs in grouped.values() for seg in segs]
    for spk, segs in sorted(grouped.items(), key=lambda kv: -sum(e - s for s, e in kv[1])):
        speech = float(sum(e - s for s, e in segs))
        if speech < min_speech:
            continue
        results.append(SpeakerResult(label=spk, segments=segs, speech_seconds=speech))

    # embed all chunks of all speakers in one go (fewer, fuller batches)
    all_chunks: list[np.ndarray] = []
    owners: list[tuple[int, float, float]] = []
    for i, r in enumerate(results):
        ch = make_chunks(wav, audio.SR, r.segments, min_seg, max_chunk)
        if not ch:  # only short turns: embed them anyway, concatenated
            cat = np.concatenate([wav[int(s * audio.SR):int(e * audio.SR)] for s, e in r.segments])
            ch = [(cat, r.segments[0][0], r.segments[0][0] + len(cat) / audio.SR)]
        for c, a, b in ch:
            all_chunks.append(c)
            owners.append((i, a, b))
    progress("embedding", 0.0)
    embs = np.zeros((0, config.EMBED_DIM), dtype=np.float32)
    if all_chunks:
        parts = []
        bs = 64
        for b0 in range(0, len(all_chunks), bs):
            parts.append(embedder.embed_batch(all_chunks[b0:b0 + bs]))
            progress("embedding", min(1.0, (b0 + bs) / len(all_chunks)))
        embs = np.concatenate(parts)

    for i, r in enumerate(results):
        idx = [j for j, o in enumerate(owners) if o[0] == i]
        if not idx:
            continue
        e = embs[idx]
        # chunk quality = duration-weighted (longer chunks give better embeddings)
        durs = np.asarray([owners[j][2] - owners[j][1] for j in idx], dtype=np.float32)
        w = durs / durs.sum()
        mean = (e * w[:, None]).sum(axis=0)
        r.mean_emb = mean / (np.linalg.norm(mean) + 1e-9)
        # order chunks by duration and keep the best N
        keep = int(settings["max_prints_per_speaker"])
        order = np.argsort(-durs)[:keep]
        r.chunk_embs = e[order]
        r.chunk_quality = [float(durs[k]) for k in order]
        r.snr_db = audio.estimate_snr_db(energy_db, hop_s, r.segments, all_speech)
        r.clip_frac = audio.clipping_fraction(wav, r.segments)
        r.quality = audio.quality_score(r.speech_seconds, r.snr_db, r.clip_frac)
    return [r for r in results if r.mean_emb is not None]


def write_speakers(conn: sqlite3.Connection, source_id: int, wav: np.ndarray, results: list[SpeakerResult],
                   settings: config.Settings, incognito: bool) -> None:
    clip_len = float(settings["clip_seconds"])
    for r in results:
        cur = conn.execute(
            "INSERT INTO source_speakers(source_id, label, speech_seconds, n_segments, first_start_s, quality_score,"
            " snr_db, clip_fraction, embedding) VALUES(?,?,?,?,?,?,?,?,?)",
            (source_id, r.label, r.speech_seconds, len(r.segments), r.segments[0][0], r.quality, r.snr_db,
             r.clip_frac, db.emb_to_blob(r.mean_emb)))
        r.source_speaker_id = int(cur.lastrowid)
        conn.executemany("INSERT INTO segments(source_speaker_id, start_s, end_s) VALUES(?,?,?)",
                         [(r.source_speaker_id, s, e) for s, e in r.segments])
        if not incognito:
            clip = config.CLIP_DIR / f"{source_id}_{r.source_speaker_id}.wav"
            audio.write_clip_concat(wav, r.segments, clip_len, clip)
            r.clip_path = clip.name
            conn.execute("UPDATE source_speakers SET clip_path=? WHERE id=?", (clip.name, r.source_speaker_id))


def decide_and_record(conn: sqlite3.Connection, source: dict, results: list[SpeakerResult],
                      settings: config.Settings, gcache: gallery.GalleryCache) -> None:
    """Search the gallery for every speaker and apply the open-set decision rules."""
    sid = int(source["id"])
    incognito = bool(source["incognito"])
    mode = source["mode"]
    k = int(settings["num_candidates"])
    match_thr = float(settings["match_threshold"])
    cluster_thr = float(settings["unknown_cluster_threshold"])
    save_unknown = bool(settings["save_unknown_voices"]) and not incognito
    min_enroll = float(settings["min_enroll_seconds"])
    max_pp = int(settings["max_prints_per_person"])

    # enrol mode: the speaker with the most speech is the named person
    enroll_pid = source["enroll_person_id"] if mode == "enroll" else None
    dominant = max(results, key=lambda r: r.speech_seconds) if results else None

    for r in results:
        g = gcache.get(conn, settings)  # reloads if prints changed (e.g. a new unknown just created)
        cands = g.search(r.mean_emb, k=k, exclude_source=sid)
        r.candidates = cands
        for c in cands:
            conn.execute(
                "INSERT INTO matches(source_speaker_id, person_id, rank, raw_score, norm_score, probability, margin,"
                " score_mode, status) VALUES(?,?,?,?,?,?,?,?, 'open')",
                (r.source_speaker_id, c.person_id, c.rank, c.raw_score, c.norm_score, c.probability, c.margin,
                 c.score_mode))

        if enroll_pid is not None and r is dominant:
            gallery.add_prints(conn, enroll_pid, sid, r.source_speaker_id, r.mean_emb, list(r.chunk_embs),
                               "enroll", r.speech_seconds, r.quality)
            gallery.prune_prints(conn, enroll_pid, max_pp)
            conn.execute("UPDATE source_speakers SET person_id=?, assignment='enrolled' WHERE id=?",
                         (enroll_pid, r.source_speaker_id))
            r.decision, r.person_id = "enrolled", enroll_pid
            continue

        top = cands[0] if cands else None
        if top is not None and top.kind == "named" and top.probability >= match_thr:
            # Known person: flag it, but prints are only added when you Confirm.
            conn.execute("UPDATE matches SET status='auto' WHERE source_speaker_id=? AND rank=1",
                         (r.source_speaker_id,))
            if not incognito:
                conn.execute("UPDATE source_speakers SET person_id=?, assignment='auto' WHERE id=?",
                             (top.person_id, r.source_speaker_id))
            r.decision, r.person_id = "matched", top.person_id
            continue

        if incognito:
            r.decision = "incognito"
            continue

        if top is not None and top.kind == "provisional" and top.probability >= cluster_thr:
            # Same unnamed voice heard again: grow its print set so it keeps getting better.
            gallery.add_prints(conn, top.person_id, sid, r.source_speaker_id, r.mean_emb, list(r.chunk_embs),
                               "auto-cluster", r.speech_seconds, r.quality)
            gallery.prune_prints(conn, top.person_id, max_pp)
            conn.execute("UPDATE matches SET status='auto' WHERE source_speaker_id=? AND rank=1",
                         (r.source_speaker_id,))
            conn.execute("UPDATE source_speakers SET person_id=?, assignment='auto' WHERE id=?",
                         (top.person_id, r.source_speaker_id))
            r.decision, r.person_id = "clustered", top.person_id
            continue

        if save_unknown and r.speech_seconds >= min_enroll:
            cur = conn.execute("INSERT INTO persons(name, kind) VALUES(?, 'provisional')", ("Unknown Voice",))
            pid = int(cur.lastrowid)
            conn.execute("UPDATE persons SET name=? WHERE id=?", (f"Unknown Voice {pid}", pid))
            gallery.add_prints(conn, pid, sid, r.source_speaker_id, r.mean_emb, list(r.chunk_embs),
                               "auto-cluster", r.speech_seconds, r.quality)
            conn.execute("UPDATE source_speakers SET person_id=?, assignment='auto' WHERE id=?",
                         (pid, r.source_speaker_id))
            r.decision, r.person_id = "new_unknown", pid
            continue

        r.decision = "unknown"


def process_source(conn: sqlite3.Connection, source_id: int, progress: Progress | None = None,
                   gcache: gallery.GalleryCache | None = None) -> dict:
    progress = progress or (lambda stage, frac: None)
    gcache = gcache or gallery.GalleryCache()
    settings = config.Settings(conn)
    source = db.row(conn, "SELECT * FROM sources WHERE id=?", (source_id,))
    if source is None:
        raise ValueError(f"source {source_id} not found")

    conn.execute("UPDATE sources SET status='processing', error=NULL WHERE id=?", (source_id,))
    # Re-processing: drop derived rows (prints cascade via source_speakers).
    conn.execute("DELETE FROM source_speakers WHERE source_id=?", (source_id,))
    db.bump_prints_version(conn)

    wav_path = prepare_source(conn, source, progress)
    source = db.row(conn, "SELECT * FROM sources WHERE id=?", (source_id,))
    wav = audio.load_wav(wav_path)

    progress("loading models", 0.0)
    diar = Diarizer.get(settings["diarization_pipeline"], settings["hf_token"], settings["device"], config.MODEL_DIR)
    emb = Embedder.get(settings["embedding_model"], settings["device"], config.MODEL_DIR)

    progress("diarizing", 0.0)
    turns = diar.run(wav, audio.SR, int(settings["max_speakers"]),
                     progress=lambda step, f: progress(f"diarizing: {step}", f))

    results = analyse(wav, turns, settings, emb, progress)

    progress("matching", 0.0)
    with db.tx(conn):
        write_speakers(conn, source_id, wav, results, settings, bool(source["incognito"]))
        decide_and_record(conn, source, results, settings, gcache)
        summary = {
            "n_turns": len(turns),
            "speakers": [{"label": r.label, "speech_seconds": round(r.speech_seconds, 1), "quality": r.quality,
                          "decision": r.decision, "person_id": r.person_id,
                          "top": r.candidates[0].as_dict() if r.candidates else None} for r in results],
        }
        conn.execute("UPDATE sources SET status='done', n_speakers=?, processed_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),"
                     " meta=? WHERE id=?", (len(results), json.dumps(summary), source_id))
    progress("done", 1.0)
    return summary
