"""Suggest who each voice in a recording is, from context rather than from voiceprints:

1. on-screen captions: OCR of the lower third of video frames taken at the start of a voice's turns
   (local RapidOCR, no network);
2. names in the recording's title and description, and the transcript name hints;
3. a language model that weighs all of it: a local Ollama model by default, or Claude with an API key.

Nothing is auto-named: results land in source_speakers.name_hints as ranked suggestions with evidence."""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
import tempfile
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Callable

from . import audio, config, db
from .transcribe import NOT_NAMES, RE_TITLE

log = logging.getLogger(__name__)
Progress = Callable[[str, float], None]
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi"}
ROLE_WORDS = NOT_NAMES | {"Of", "State", "Health", "Human", "Services", "Hearing", "Live", "Breaking", "News", "Committee", "Subcommittee", "Nominee",
                          "Finance", "Judiciary", "Armed", "Foreign", "Relations", "Intelligence", "Homeland", "Security", "Testimony", "Confirmation",
                          "Washington", "Capitol", "Hill", "Press", "Conference", "Briefing", "Interview", "Panel", "Guest", "Host", "Anchor", "Correspondent"}
TITLE_TOKENS = {"sen", "sen.", "senator", "rep", "rep.", "representative", "dr", "dr.", "mr", "mr.", "ms", "ms.", "mrs", "mrs.", "hon", "hon.", "gov", "gov.",
                "governor", "sec", "sec.", "secretary", "chairman", "chairwoman", "chair", "ranking", "member", "judge", "justice", "prof", "prof.", "professor",
                "amb.", "ambassador", "gen.", "general", "adm.", "admiral", "fmr", "fmr.", "former", "mayor", "del.", "delegate", "atty", "attorney"}
SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}


# ---------------------------------------------------------------- names from text

def _titlecase(tok: str) -> str:
    if tok.lower() in SUFFIXES:
        return {"jr": "Jr.", "jr.": "Jr.", "sr": "Sr.", "sr.": "Sr.", "ii": "II", "iii": "III", "iv": "IV"}[tok.lower()]
    if re.fullmatch(r"[A-Za-z]\.", tok):
        return tok.upper()
    parts = re.split(r"([-'])", tok)
    return "".join(p[:1].upper() + p[1:].lower() if p and p not in "-'" else p for p in parts)


def clean_caption_name(text: str) -> str | None:
    """'SEN.ELIZABETH WARREN (D-MA)' -> 'Elizabeth Warren'; role lines and junk -> None."""
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", text)
    t = re.split(r"\s[\-|•·:]\s|,\s", t)[0]
    t = re.sub(r"(?<=[A-Za-z])\.(?=[A-Z])", ". ", t)               # SEN.ELIZABETH -> SEN. ELIZABETH
    toks = [x for x in re.split(r"\s+", t.strip()) if x]
    while toks and toks[0].lower() in TITLE_TOKENS:
        toks = toks[1:]
    toks = [x for x in toks if re.fullmatch(r"[A-Za-z][A-Za-z'\-\.]*", x)]
    if not 2 <= len(toks) <= 4:
        return None
    name = " ".join(_titlecase(x) for x in toks)
    parts = name.split()
    if all(p.rstrip(".") in ROLE_WORDS for p in parts) or parts[0].rstrip(".") in ROLE_WORDS or parts[-1].rstrip(".") in ROLE_WORDS:
        return None
    if any(len(p.strip(".'-")) < 2 and not re.fullmatch(r"[A-Z]\.", p) for p in parts):
        return None
    return name


def names_in_text(text: str) -> list[str]:
    """Candidate person names in a title/description: '<Title> X Y' plus capitalised 2-3 word runs."""
    out: list[str] = []
    for m in RE_TITLE.finditer(text or ""):
        n = clean_caption_name(m.group(1))
        if n:
            out.append(n)
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z]\.)?(?:\s+[A-Z][a-z]+){1,2})\b", text or ""):
        n = clean_caption_name(m.group(1))
        if n:
            out.append(n)
    seen, uniq = set(), []
    for n in out:
        if n.lower() not in seen:
            seen.add(n.lower()); uniq.append(n)
    return uniq


# ---------------------------------------------------------------- frames + OCR

def _frame_from_video(video: Path, t: float, out: Path, width: int = 960) -> bool:
    cmd = [audio.ffmpeg_bin(), "-y", "-nostdin", "-loglevel", "error", "-ss", f"{max(0.0, t):.2f}", "-i", str(video),
           "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "4", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 1000


def _youtube_frame(url: str, t: float, out_dir: Path, width: int = 960) -> Path | None:
    """Download two seconds of ≤480p video around t and take one frame from it."""
    import yt_dlp
    from yt_dlp.utils import download_range_func
    tag = f"clip-{int(t)}"
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "format": "bestvideo[height<=480][ext=mp4]/bestvideo[height<=480]/best[height<=480]/best",
            "download_ranges": download_range_func(None, [(max(0.0, t), t + 2.0)]), "force_keyframes_at_cuts": True,
            "outtmpl": str(out_dir / (tag + ".%(ext)s")), "retries": 2}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    clips = sorted(out_dir.glob(tag + ".*"), key=lambda p: p.stat().st_mtime)
    if not clips:
        return None
    frame = out_dir / (tag + ".jpg")
    return frame if _frame_from_video(clips[-1], 1.0, frame, width) else None


def sample_times(conn: sqlite3.Connection, speaker_id: int, n: int = 2) -> list[float]:
    """Starts of the longest turns (captions appear when someone begins speaking), a few seconds in."""
    rows = db.rows(conn, "SELECT start_s, end_s FROM segments WHERE source_speaker_id=? ORDER BY (end_s-start_s) DESC LIMIT ?", (speaker_id, n))
    return [float(r["start_s"]) + min(3.0, max(0.5, (float(r["end_s"]) - float(r["start_s"])) / 3)) for r in rows]


def frames_for(src: dict, times: list[float], tmp: Path, progress: Progress) -> list[tuple[float, Path]]:
    out: list[tuple[float, Path]] = []
    orig = Path(src["orig_path"]) if src.get("orig_path") else None
    is_video = orig is not None and orig.exists() and orig.suffix.lower() in VIDEO_EXTS
    is_yt = bool(src.get("url")) and ("youtube.com" in src["url"] or "youtu.be" in src["url"])
    for i, t in enumerate(times):
        progress("grabbing frames", i / max(1, len(times)))
        try:
            if is_video:
                f = tmp / f"f-{int(t)}.jpg"
                if _frame_from_video(orig, t, f):
                    out.append((t, f))
            elif is_yt:
                f = _youtube_frame(src["url"], t, tmp)
                if f:
                    out.append((t, f))
        except Exception as e:  # noqa: BLE001
            log.info("frame at %.0fs failed: %s", t, e)
    return out


_OCR = None


def ocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def ocr_lower_third(frame: Path) -> list[tuple[str, float]]:
    """Text lines in the bottom 45% of the frame, with confidence."""
    global _OCR
    import numpy as np
    from PIL import Image
    from rapidocr_onnxruntime import RapidOCR
    if _OCR is None:
        _OCR = RapidOCR()
    img = Image.open(frame).convert("RGB")
    w, h = img.size
    crop = img.crop((0, int(h * 0.55), w, h))
    if crop.width < 1200:
        crop = crop.resize((crop.width * 2, crop.height * 2))
    res, _ = _OCR(np.array(crop))
    return [(str(r[1]), float(r[2])) for r in (res or []) if float(r[2]) >= 0.6]


# ---------------------------------------------------------------- metadata

def fetch_metadata(conn: sqlite3.Connection, src: dict) -> dict:
    """Title/description/channel. YouTube descriptions are fetched once and cached on the source."""
    meta = {"title": src.get("name") or "", "description": src.get("description") or "", "channel": ""}
    url = src.get("url") or ""
    if meta["description"] or not ("youtube.com" in url or "youtu.be" in url):
        return meta
    try:
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        meta["title"] = info.get("title") or meta["title"]
        meta["description"] = (info.get("description") or "")[:4000]
        meta["channel"] = info.get("channel") or info.get("uploader") or ""
        conn.execute("UPDATE sources SET description=? WHERE id=?", (json.dumps({"description": meta["description"], "channel": meta["channel"], "title": meta["title"]}), src["id"]))
    except Exception as e:  # noqa: BLE001
        log.info("metadata fetch failed: %s", e)
    return meta


def _load_meta(src: dict) -> dict | None:
    if not src.get("description"):
        return None
    try:
        d = json.loads(src["description"])
        return d if isinstance(d, dict) else {"description": src["description"]}
    except Exception:
        return {"description": src["description"]}


# ---------------------------------------------------------------- the language model

SYSTEM = ("You identify who is speaking in a recording from context: the title and description, on-screen captions read "
          "by OCR (may contain small errors), names other participants used, and what each voice said. Reply with strict JSON only: "
          '{"speakers":[{"label":"...","name":"Full Name or null","confidence":0.0-1.0,"why":"one short sentence"}]}. '
          "Use the person's commonly used full name. Give null when the evidence is weak. Never invent people who are not supported by the evidence.")


def llm_payload(meta: dict, speakers: list[dict]) -> str:
    lines = [f"TITLE: {meta.get('title', '')}", f"CHANNEL: {meta.get('channel', '')}", f"DESCRIPTION: {(meta.get('description') or '')[:1500]}", ""]
    for sp in speakers:
        lines.append(f"VOICE {sp['label']}: speaks {sp['speech_seconds']:.0f} s in {sp['n_turns']} turns")
        if sp["ocr"]:
            lines.append("  on-screen captions while speaking: " + "; ".join(sp["ocr"][:6]))
        if sp["hints"]:
            lines.append("  names others used around their turns: " + ", ".join(f"{h['name']} ({h['score']})" for h in sp["hints"][:5]))
        if sp["said"]:
            lines.append("  what they said (excerpts): " + " … ".join(x[:300] for x in sp["said"][:4]))
        lines.append("")
    return "\n".join(lines)


def ask_llm(settings: config.Settings, payload: str) -> dict:
    provider = str(settings["llm_provider"] or "none").lower()
    if provider == "ollama":
        body = json.dumps({"model": settings["ollama_model"], "stream": False, "format": "json", "options": {"temperature": 0},
                           "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": payload}]}).encode()
        req = urllib.request.Request(str(settings["ollama_url"]).rstrip("/") + "/api/chat", data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            text = json.loads(r.read())["message"]["content"]
    elif provider == "anthropic":
        key = settings["anthropic_api_key"]
        if not key:
            raise RuntimeError("no Anthropic API key set")
        body = json.dumps({"model": settings["anthropic_model"], "max_tokens": 1500, "system": SYSTEM,
                           "messages": [{"role": "user", "content": payload}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body,
                                     headers={"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=120) as r:
            text = json.loads(r.read())["content"][0]["text"]
    else:
        return {}
    return parse_llm(text)


def parse_llm(text: str) -> dict:
    """label -> {name, confidence, why}; tolerant of prose around the JSON."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    out = {}
    for s in data.get("speakers", []) or []:
        if not isinstance(s, dict) or not s.get("label"):
            continue
        name = s.get("name")
        if isinstance(name, str) and name.strip().lower() not in ("", "null", "unknown", "none"):
            name = clean_caption_name(name.strip()) or name.strip()
        else:
            name = None
        try:
            conf = float(s.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0.0
        out[str(s["label"])] = {"name": name, "confidence": max(0.0, min(1.0, conf)), "why": str(s.get("why") or "")[:200]}
    return out


def llm_status(settings: config.Settings) -> dict:
    provider = str(settings["llm_provider"] or "none").lower()
    st = {"provider": provider, "ocr": ocr_available()}
    if provider == "ollama":
        try:
            with urllib.request.urlopen(str(settings["ollama_url"]).rstrip("/") + "/api/tags", timeout=3) as r:
                models = [m["name"] for m in json.loads(r.read()).get("models", [])]
            st.update(reachable=True, models=models, model_present=any(m.split(":")[0] == str(settings["ollama_model"]).split(":")[0] for m in models))
        except Exception as e:  # noqa: BLE001
            st.update(reachable=False, error=str(e)[:160])
    elif provider == "anthropic":
        st.update(key_set=bool(settings["anthropic_api_key"]), model=settings["anthropic_model"])
    return st


# ---------------------------------------------------------------- putting it together

def suggest_names(conn: sqlite3.Connection, source_id: int, progress: Progress | None = None) -> dict:
    progress = progress or (lambda stage, frac: None)
    settings = config.Settings(conn)
    src = db.row(conn, "SELECT * FROM sources WHERE id=?", (source_id,))
    if src is None:
        raise ValueError("source not found")
    src = dict(src)
    cached = _load_meta(src)
    if cached:
        src["description"] = cached.get("description", "")
        meta = {"title": cached.get("title") or src["name"], "description": cached.get("description", ""), "channel": cached.get("channel", "")}
    else:
        src["description"] = ""
        progress("reading title and description", 0.0)
        meta = fetch_metadata(conn, src)
    meta_names = names_in_text(meta["title"] + "\n" + meta["description"])
    speakers = db.rows(conn, "SELECT * FROM source_speakers WHERE source_id=? AND ignored=0 ORDER BY speech_seconds DESC", (source_id,))
    evidence: dict[int, dict[str, dict]] = {sp["id"]: defaultdict(lambda: {"score": 0.0, "evidence": []}) for sp in speakers}

    def add(sid: int, name: str, score: float, why: str) -> None:
        name = _merge_key(evidence[sid], name)
        e = evidence[sid][name]
        e["score"] += score
        if why not in e["evidence"]:
            e["evidence"].append(why)

    # 1. transcript hints, recomputed from the stored transcript lines (independent of any earlier run)
    from .transcribe import _turns, name_hints
    lines = [{"sid": r["sid"], "s": float(r["s"]), "e": float(r["e"]), "text": r["text"]} for r in conn.execute(
        "SELECT source_speaker_id AS sid, start_s AS s, end_s AS e, text FROM transcript_segments WHERE source_id=? ORDER BY start_s", (source_id,))]
    if lines:
        for sid, lst in name_hints(lines, _turns(conn, source_id)).items():
            if sid in evidence:
                for h in lst:
                    add(sid, h["name"], float(h["score"]), "introduced or addressed by name in the transcript")

    # 2. on-screen captions
    ocr_text: dict[int, list[str]] = defaultdict(list)
    if settings["suggest_ocr"] and ocr_available() and speakers:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            plan = [(sp["id"], t) for sp in speakers[:12] for t in sample_times(conn, sp["id"], 2)]
            frames = frames_for(src, [t for _, t in plan], tmp, progress)
            by_t = {round(t, 2): f for t, f in frames}
            for i, (sid, t) in enumerate(plan):
                f = by_t.get(round(t, 2))
                if not f:
                    continue
                progress("reading captions", i / max(1, len(plan)))
                try:
                    for text, conf in ocr_lower_third(f):
                        n = clean_caption_name(text)
                        if n:
                            ocr_text[sid].append(n)
                            add(sid, n, 4.0 * conf, f"on-screen caption “{text.strip()}” at {_fmt(t)}")
                except Exception as e:  # noqa: BLE001
                    log.info("ocr failed on %s: %s", f, e)

    # 3. title/description: a bonus for names already supported by something else
    for sid in evidence:
        for name in list(evidence[sid]):
            if any(_same(name, m) for m in meta_names):
                add(sid, name, 1.0, "named in the title or description")

    # 4. the language model
    llm_used = None
    if str(settings["llm_provider"] or "none").lower() != "none" and speakers:
        progress("asking the language model", 0.0)
        said = _said(conn, source_id)
        pay = [{"label": sp["label"], "speech_seconds": sp["speech_seconds"] or 0, "n_turns": sp["n_segments"] or 0,
                "ocr": ocr_text.get(sp["id"], []), "said": said.get(sp["id"], []),
                "hints": [{"name": n, "score": round(v["score"], 1)} for n, v in sorted(evidence[sp["id"]].items(), key=lambda kv: -kv[1]["score"])[:5]]}
               for sp in speakers[:16]]
        try:
            picks = ask_llm(settings, llm_payload(meta, pay))
            llm_used = settings["llm_provider"]
            for sp in speakers:
                p = picks.get(sp["label"])
                if p and p["name"] and p["confidence"] >= 0.3:
                    add(sp["id"], p["name"], 4.0 * p["confidence"], f"language model ({int(p['confidence'] * 100)}%): {p['why']}")
        except Exception as e:  # noqa: BLE001
            log.warning("llm suggestion failed: %s", e)
            llm_used = f"failed: {e}"

    # write ranked suggestions back
    n_sugg = 0
    with db.tx(conn):
        for sp in speakers:
            ranked = sorted(evidence[sp["id"]].items(), key=lambda kv: -kv[1]["score"])
            out = [{"name": n, "score": round(v["score"], 1), "evidence": v["evidence"][:4]} for n, v in ranked if v["score"] >= 2.0][:4]
            n_sugg += bool(out)
            conn.execute("UPDATE source_speakers SET name_hints=? WHERE id=?", (json.dumps(out, ensure_ascii=False) if out else None, sp["id"]))
    return {"speakers": len(speakers), "with_suggestions": n_sugg, "meta_names": meta_names[:10], "ocr_frames": sum(len(v) for v in ocr_text.values()),
            "llm": llm_used}


def _said(conn: sqlite3.Connection, source_id: int) -> dict[int, list[str]]:
    out: dict[int, list[str]] = defaultdict(list)
    for r in conn.execute("SELECT source_speaker_id AS sid, text FROM transcript_segments WHERE source_id=? AND source_speaker_id IS NOT NULL"
                          " ORDER BY (end_s-start_s) DESC", (source_id,)):
        if len(out[r["sid"]]) < 4:
            out[r["sid"]].append(r["text"])
    return out


def _core(name: str) -> list[str]:
    """Tokens that identify a person: no suffixes (Jr., III), no middle initials, lower-case."""
    toks = [t.strip(".,").lower() for t in name.split()]
    return [t for t in toks if t and t not in {x.strip(".") for x in SUFFIXES} and len(t) > 1]


def _same(a: str, b: str) -> bool:
    """'Robert Kennedy' ~ 'Robert F. Kennedy Jr.' ~ 'Kennedy'; not 'John Kennedy' vs 'Robert Kennedy'."""
    ca, cb = _core(a), _core(b)
    if not ca or not cb or ca[-1] != cb[-1]:
        return False
    return set(ca) <= set(cb) or set(cb) <= set(ca)


def _merge_key(bucket: dict, name: str) -> str:
    """Reuse an existing key for the same person ('Warren' vs 'Elizabeth Warren'), preferring the longer name."""
    for k in list(bucket):
        if _same(k, name):
            if len(name) > len(k):
                bucket[name] = bucket.pop(k)
                return name
            return k
    return name


def _fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60}:{t % 60:02d}"
