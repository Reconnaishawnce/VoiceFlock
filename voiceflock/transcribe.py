"""Transcripts: faster-whisper with word timestamps, every word attributed to the diarized speaker
whose turn it falls in, plus name hints ("the chair recognizes Senator Warren" -> the next voice is
probably Warren). Raw words are kept in data/transcripts/<id>.json so attribution can be redone after a
reprocess without running Whisper again."""
from __future__ import annotations

import bisect
import json
import logging
import os
import re
import sqlite3
import sys
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable

from . import config, db

log = logging.getLogger(__name__)
Progress = Callable[[str, float], None]

TITLES = ("Senator", "Sen\\.", "Representative", "Rep\\.", "Congressman", "Congresswoman", "Chairman", "Chairwoman", "Chair",
          "Secretary", "Governor", "Mayor", "Judge", "Justice", "Professor", "Dr\\.", "Doctor", "Mr\\.", "Ms\\.", "Mrs\\.", "Mister",
          "Ambassador", "General", "Admiral", "Director", "Commissioner", "Attorney General", "Speaker", "Leader", "Officer", "Detective")
NAME = r"((?:[A-Z][a-zA-Z'\-]+)(?:\s+(?:[A-Z]\.|[A-Z][a-zA-Z'\-]+)){0,2})"
RE_TITLE = re.compile(r"\b(?:" + "|".join(TITLES) + r")\s+" + NAME)
RE_SELF = re.compile(r"\b(?:[Mm]y name is|I am|I'm|[Tt]his is)\s+" + NAME)
NOT_NAMES = {"Chairman", "Chair", "Chairwoman", "President", "Speaker", "Secretary", "Senator", "Ranking", "Member", "Witness", "Doctor",
             "Madam", "Sir", "The", "Thank", "Thanks", "Yes", "No", "Okay", "Well", "So", "And", "But", "Our", "My", "Your", "Here",
             "Gentleman", "Gentlelady", "Gentlewoman", "Leader", "Majority", "Minority", "Committee", "Congress", "Senate", "House", "General",
             "Director", "Governor", "Mayor", "Judge", "Justice", "Professor", "Ambassador", "Commissioner", "Officer", "Detective", "Ladies", "Everyone", "Everybody", "Good", "Now"}
_MODEL_LOCK = threading.Lock()
_MODEL: dict = {}


def raw_path(source_id: int) -> Path:
    return config.DATA_DIR / "transcripts" / f"{source_id}.json"


def wanted(settings: config.Settings) -> bool:
    mode = str(settings["transcribe_auto"] or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _dll_dirs() -> None:
    """ctranslate2 needs cuBLAS/cuDNN; torch's CUDA wheels ship them, so point Windows at torch/lib."""
    if sys.platform != "win32":
        return
    try:
        import torch
        lib = Path(torch.__file__).parent / "lib"
        if lib.exists():
            os.add_dll_directory(str(lib))
            os.environ["PATH"] = str(lib) + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


def pick_model(settings: config.Settings) -> tuple[str, str, str]:
    """(model name, device, compute type). 'auto' picks large-v3-turbo on a GPU and base on CPU."""
    name = str(settings["transcribe_model"] or "auto")
    cuda = False
    try:
        import torch
        cuda = bool(torch.cuda.is_available())
    except Exception:
        pass
    dev = str(settings["device"] or "auto")
    if dev == "cpu":
        cuda = False
    if name == "auto":
        name = "large-v3-turbo" if cuda else "base"
    if name == "large-v3-turbo":
        name = "deepdml/faster-whisper-large-v3-turbo-ct2"
    return name, ("cuda" if cuda else "cpu"), ("float16" if cuda else "int8")


def get_model(settings: config.Settings):
    name, device, compute = pick_model(settings)
    key = (name, device, compute)
    with _MODEL_LOCK:
        if _MODEL.get("key") == key:
            return _MODEL["model"], device
        _dll_dirs()
        from faster_whisper import WhisperModel
        try:
            model = WhisperModel(name, device=device, compute_type=compute, download_root=str(config.MODEL_DIR / "whisper"))
        except Exception as e:  # noqa: BLE001
            if device == "cuda":
                log.warning("whisper on CUDA failed (%s); falling back to CPU int8", e)
                device = "cpu"
                model = WhisperModel(name, device="cpu", compute_type="int8", download_root=str(config.MODEL_DIR / "whisper"))
            else:
                raise
        _MODEL.update(key=key, model=model)
        return model, device


def run_whisper(path: Path, settings: config.Settings, progress: Progress, duration_s: float | None) -> tuple[str, list[dict]]:
    """-> (language, words[{s, e, w, p}]). Replaceable in tests."""
    model, device = get_model(settings)
    lang = str(settings["transcribe_language"] or "").strip() or None
    segments, info = model.transcribe(str(path), language=lang, word_timestamps=True, vad_filter=True,
                                      beam_size=5 if device == "cuda" else 1, condition_on_previous_text=False)
    words: list[dict] = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({"s": round(float(w.start), 2), "e": round(float(w.end), 2), "w": w.word, "p": round(float(w.probability), 3)})
        if duration_s:
            progress(f"transcribing ({device})", min(0.99, float(seg.end) / duration_s))
    return (info.language or lang or ""), words


def transcribe_source(conn: sqlite3.Connection, source_id: int, progress: Progress | None = None, force: bool = False) -> dict:
    progress = progress or (lambda stage, frac: None)
    settings = config.Settings(conn)
    src = db.row(conn, "SELECT * FROM sources WHERE id=?", (source_id,))
    if src is None:
        raise ValueError("source not found")
    rp = raw_path(source_id)
    conn.execute("UPDATE sources SET transcript_status='running' WHERE id=?", (source_id,))
    try:
        if rp.exists() and not force:
            raw = json.loads(rp.read_text(encoding="utf-8"))
            language, words = raw.get("language", ""), raw["words"]
            progress("attributing words to voices", 0.5)
        else:
            path = Path(src["wav_path"]) if src["wav_path"] and Path(src["wav_path"]).exists() else (Path(src["orig_path"]) if src["orig_path"] else None)
            if path is None or not path.exists():
                raise FileNotFoundError("no audio on disk for this recording")
            progress("loading speech model", 0.0)
            language, words = run_whisper(path, settings, progress, src["duration_s"])
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(json.dumps({"language": language, "words": words}, ensure_ascii=False), encoding="utf-8")
        with db.tx(conn):
            n = attribute(conn, source_id, words)
            conn.execute("UPDATE sources SET transcript_status='done', transcript_language=?, transcript_info=? WHERE id=?",
                         (language, json.dumps({"words": len(words), "segments": n, "model": pick_model(settings)[0]}), source_id))
    except Exception as e:
        conn.execute("UPDATE sources SET transcript_status='failed', transcript_info=? WHERE id=?",
                     (json.dumps({"error": f"{type(e).__name__}: {e}"[:300]}), source_id))
        raise
    return {"language": language, "words": len(words), "segments": n}


# ---------------------------------------------------------------- attribution

def _turns(conn: sqlite3.Connection, source_id: int) -> list[tuple[float, float, int]]:
    rows = db.rows(conn, "SELECT g.start_s, g.end_s, g.source_speaker_id AS sid FROM segments g JOIN source_speakers ss ON ss.id=g.source_speaker_id"
                         " WHERE ss.source_id=? ORDER BY g.start_s", (source_id,))
    return [(float(r["start_s"]), float(r["end_s"]), int(r["sid"])) for r in rows]


def _speaker_at(turns: list[tuple[float, float, int]], starts: list[float], t: float, slack: float = 0.6) -> int | None:
    i = bisect.bisect_right(starts, t) - 1
    best, best_d = None, slack
    for j in (i, i + 1, i - 1):
        if 0 <= j < len(turns):
            s, e, sid = turns[j]
            if s <= t <= e:
                return sid
            d = s - t if t < s else t - e
            if d < best_d:
                best, best_d = sid, d
    return best


def group_words(words: list[dict], turns: list[tuple[float, float, int]], max_gap: float = 1.2, max_len: float = 25.0) -> list[dict]:
    """Consecutive words of one speaker -> one transcript line; split on long pauses, sentence ends after ~25 s, or a speaker change."""
    starts = [t[0] for t in turns]
    out: list[dict] = []
    cur: dict | None = None
    for w in words:
        mid = (w["s"] + w["e"]) / 2
        sid = _speaker_at(turns, starts, mid)
        tok = w["w"]
        if cur is not None and (sid != cur["sid"] or w["s"] - cur["e"] > max_gap or
                                (w["s"] - cur["s"] > max_len and cur["text"].rstrip().endswith((".", "?", "!")))):
            out.append(cur); cur = None
        if cur is None:
            cur = {"sid": sid, "s": w["s"], "e": w["e"], "text": tok.strip()}
        else:
            cur["e"] = w["e"]; cur["text"] += tok if tok.startswith((" ", ",", ".", "?", "!", "'")) or cur["text"].endswith(("-", "'")) else " " + tok
    if cur is not None:
        out.append(cur)
    for o in out:
        o["text"] = re.sub(r"\s+", " ", o["text"]).strip()
    return [o for o in out if o["text"]]


def attribute(conn: sqlite3.Connection, source_id: int, words: list[dict]) -> int:
    """Rebuild transcript_segments and per-speaker name hints for a recording from raw words."""
    turns = _turns(conn, source_id)
    lines = group_words(words, turns)
    conn.execute("DELETE FROM transcript_segments WHERE source_id=?", (source_id,))
    conn.executemany("INSERT INTO transcript_segments(source_id, source_speaker_id, start_s, end_s, text) VALUES(?,?,?,?,?)",
                     [(source_id, o["sid"], o["s"], o["e"], o["text"]) for o in lines])
    hints = name_hints(lines, turns)
    conn.execute("UPDATE source_speakers SET name_hints=NULL WHERE source_id=?", (source_id,))
    for sid, lst in hints.items():
        conn.execute("UPDATE source_speakers SET name_hints=? WHERE id=?", (json.dumps(lst, ensure_ascii=False), sid))
    return len(lines)


def _clean_name(n: str) -> str | None:
    parts = [p for p in n.replace(",", " ").split() if p]
    while parts and parts[0].rstrip(".") in NOT_NAMES:
        parts = parts[1:]
    while parts and parts[-1].rstrip(".") in NOT_NAMES:
        parts = parts[:-1]
    if not parts or len(" ".join(parts)) < 3:
        return None
    return " ".join(parts)


def name_hints(lines: list[dict], turns: list[tuple[float, float, int]], window_after: float = 20.0, window_before: float = 8.0) -> dict[int, list[dict]]:
    """Who is each voice, judging by names spoken around their turns?
    - "my name is X" / "I'm X" said by a voice -> strong hint for that voice
    - "<Title> X" said by voice A -> hint for the other voice whose turn starts within 20 s after (introductions,
      "the chair recognizes"), and a weaker one for the other voice that just finished speaking ("thank you, Senator X")"""
    score: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    turn_starts = sorted((s, sid) for s, _, sid in turns)
    turn_ends = sorted((e, sid) for _, e, sid in turns)
    ts = [t for t, _ in turn_starts]
    te = [t for t, _ in turn_ends]
    for ln in lines:
        text, sid, t0, t1 = ln["text"], ln["sid"], ln["s"], ln["e"]
        at = lambda m: t0 + (t1 - t0) * (m.start() / max(1, len(text)))   # when in the line the mention falls  # noqa: E731
        for m in RE_SELF.finditer(text):
            nm = _clean_name(m.group(1))
            if nm and sid is not None:
                score[sid][nm] += 3
        for m in RE_TITLE.finditer(text):
            nm = _clean_name(m.group(1))
            if not nm:
                continue
            tm = at(m)
            if t1 - tm <= 6.0:          # near the end of the turn: an introduction / hand-over -> the next voice
                i = bisect.bisect_right(ts, tm)
                while i < len(turn_starts) and turn_starts[i][0] <= tm + window_after:
                    if turn_starts[i][1] != sid:
                        score[turn_starts[i][1]][nm] += 2
                        break
                    i += 1
            if tm - t0 <= 6.0:          # near the start: addressing whoever just spoke ("thank you, Senator X")
                j = bisect.bisect_left(te, tm) - 1
                while j >= 0 and turn_ends[j][0] >= tm - window_before:
                    if turn_ends[j][1] != sid:
                        score[turn_ends[j][1]][nm] += 1
                        break
                    j -= 1
    out: dict[int, list[dict]] = {}
    for sid, names in score.items():
        # "Kennedy" folds into "Robert Kennedy" when both were heard for the same voice
        for short in [n for n in list(names) if " " not in n]:
            longer = [n for n in names if " " in n and n.split()[-1] == short]
            if longer:
                names[max(longer, key=lambda n: names[n])] += names.pop(short)
        ranked = sorted(names.items(), key=lambda kv: -kv[1])[:5]
        out[sid] = [{"name": n, "score": round(s, 1)} for n, s in ranked if s >= 2]
        if not out[sid]:
            del out[sid]
    return out


# ---------------------------------------------------------------- search

def search(conn: sqlite3.Connection, q: str, limit: int = 50) -> list[dict]:
    q = q.strip()
    if not q:
        return []
    # FTS5 phrase query (quotes escape everything), fall back to LIKE if FTS is unavailable
    try:
        rows = db.rows(conn,
            "SELECT t.id, t.source_id, t.source_speaker_id, t.start_s, t.end_s, snippet(transcript_fts, 0, '[', ']', '…', 14) AS snippet,"
            " s.name AS source_name, ss.label, p.name AS person_name FROM transcript_fts f JOIN transcript_segments t ON t.id=f.rowid"
            " JOIN sources s ON s.id=t.source_id LEFT JOIN source_speakers ss ON ss.id=t.source_speaker_id LEFT JOIN persons p ON p.id=ss.person_id"
            " WHERE transcript_fts MATCH ? AND s.status!='deleted' ORDER BY rank LIMIT ?", ('"' + q.replace('"', '""') + '"', limit))
    except sqlite3.OperationalError:
        rows = db.rows(conn,
            "SELECT t.id, t.source_id, t.source_speaker_id, t.start_s, t.end_s, t.text AS snippet, s.name AS source_name, ss.label, p.name AS person_name"
            " FROM transcript_segments t JOIN sources s ON s.id=t.source_id LEFT JOIN source_speakers ss ON ss.id=t.source_speaker_id"
            " LEFT JOIN persons p ON p.id=ss.person_id WHERE t.text LIKE ? AND s.status!='deleted' ORDER BY t.source_id DESC, t.start_s LIMIT ?", (f"%{q}%", limit))
    return rows
