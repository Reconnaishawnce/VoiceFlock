"""Command line: serve | worker | ingest | enroll | calibrate | evaluate | cohort | persons | settings."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__, config, db


def _log(verbose: bool):
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_serve(a):
    import uvicorn
    from .api import create_app
    app = create_app(run_worker=not a.no_worker)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


def cmd_worker(a):
    from .worker import worker_loop
    db.init_db()
    worker_loop(name=a.name)


def cmd_ingest(a):
    """Queue local files / URLs for processing (same as the web upload)."""
    from . import audio, worker
    db.init_db()
    conn = db.connect()
    config.ensure_dirs()
    pid = None
    if a.mode == "enroll":
        if not a.person:
            sys.exit("enroll mode needs --person NAME")
        p = db.row(conn, "SELECT id FROM persons WHERE name=? COLLATE NOCASE AND kind='named'", (a.person,))
        pid = p["id"] if p else conn.execute("INSERT INTO persons(name, kind) VALUES(?, 'named')", (a.person,)).lastrowid
    paths = []
    for item in a.inputs:
        if item.startswith("http://") or item.startswith("https://"):
            paths.append(("url", item))
            continue
        p = Path(item)
        if p.is_dir():
            paths += [("file", f) for f in sorted(p.rglob("*")) if f.is_file() and f.suffix.lower() in audio.AUDIO_EXTS]
        elif p.is_file():
            paths.append(("file", p))
        else:
            print(f"skip (not found): {item}")
    for kind, p in paths:
        with db.tx(conn):
            if kind == "url":
                cur = conn.execute("INSERT INTO sources(name, url, mode, incognito, enroll_person_id) VALUES(?,?,?,?,?)",
                                   (p, p, a.mode, int(a.incognito), pid))
            else:
                cur = conn.execute("INSERT INTO sources(name, original_filename, orig_path, mode, incognito, enroll_person_id)"
                                   " VALUES(?,?,?,?,?,?)", (p.stem, p.name, str(p.resolve()), a.mode, int(a.incognito), pid))
            jid = worker.enqueue(conn, int(cur.lastrowid))
        print(f"queued job {jid}: {p}")
    if a.run:
        from .worker import claim_job, run_job
        from .gallery import GalleryCache
        g = GalleryCache()
        while True:
            job = claim_job(conn, "cli")
            if job is None:
                break
            print(f"processing job {job['id']} (source {job['source_id']}) ...")
            run_job(conn, job, g)
            j = db.row(conn, "SELECT status, error FROM jobs WHERE id=?", (job["id"],))
            print(f"  -> {j['status']} {j['error'] or ''}")
            s = db.row(conn, "SELECT meta FROM sources WHERE id=?", (job["source_id"],))
            if s and s["meta"]:
                for sp in json.loads(s["meta"]).get("speakers", []):
                    top = sp.get("top") or {}
                    print(f"     {sp['label']:>12} {sp['speech_seconds']:7.1f}s q={sp['quality']:5.1f} "
                          f"{sp['decision']:<12} {top.get('name','')} p={top.get('probability', 0):.2f}")


def cmd_enroll(a):
    """Enrol single-speaker clips directly (no diarization): voiceflock enroll NAME clip1 clip2 ..."""
    import tempfile
    from . import audio, gallery
    from .embed import Embedder, embed_file
    db.init_db()
    conn = db.connect()
    settings = config.Settings(conn)
    emb = Embedder.get(settings["embedding_model"], settings["device"], config.MODEL_DIR)
    p = db.row(conn, "SELECT id FROM persons WHERE name=? COLLATE NOCASE AND kind='named'", (a.name,))
    pid = p["id"] if p else conn.execute("INSERT INTO persons(name, kind) VALUES(?, 'named')", (a.name,)).lastrowid
    n = 0
    with tempfile.TemporaryDirectory() as td:
        for i, f in enumerate(a.clips):
            wav_path = Path(td) / f"{i}.wav"
            audio.to_wav16k(Path(f), wav_path)
            wav = audio.load_wav(wav_path)
            mean, chunks = embed_file(emb, wav, audio.SR, float(settings["max_chunk_seconds"]))
            with db.tx(conn):
                n += gallery.add_prints(conn, int(pid), None, None, mean, list(chunks[: int(settings["max_prints_per_speaker"])]),
                                        "cli", len(wav) / audio.SR, None)
                gallery.prune_prints(conn, int(pid), int(settings["max_prints_per_person"]))
            print(f"enrolled {f} ({len(wav)/audio.SR:.1f}s)")
    print(f"person #{pid} '{a.name}': +{n} prints")


def cmd_calibrate(a):
    from .calibrate import run
    res = run(Path(a.folder), add_cohort=not a.no_cohort, replace_cohort=a.replace_cohort)
    print(json.dumps(res, indent=2))
    m = res["active_mode"]
    print(f"\nActive scoring mode: {m}. EER={res[m]['eer']*100:.2f}%  miss@1%FA={res[m]['miss_at_1pct_fa']*100:.2f}%")
    print(f"Calibrated probability at the 1%-FA threshold: {res[m]['prob_at_1pct_fa_threshold']}")
    print("Tip: set the match threshold in Settings near that probability for ~1% false alarms.")


def cmd_evaluate(a):
    from .evaluate import run
    rep = run(Path(a.folder), Path(a.out), enroll_per_speaker=a.enroll_per_speaker, against_gallery=a.against_gallery)
    print(json.dumps(rep, indent=2))
    v, i = rep["verification"], rep["identification"]
    print(f"\nrank-1 accuracy: {i['rank1_accuracy']}   EER: {v['eer']*100 if v['eer']==v['eer'] else float('nan'):.2f}%"
          f"   miss@1%FA: {v['miss_rate_at_1pct_fa']*100 if v['miss_rate_at_1pct_fa']==v['miss_rate_at_1pct_fa'] else float('nan'):.2f}%")
    print(f"DET curve: {v['det_csv']}  {v['det_png'] or ''}")


def cmd_cohort(a):
    """Add impostor embeddings for s-norm from a folder of clips (any speakers, one embedding per clip)."""
    import tempfile
    from . import audio
    from .embed import Embedder, embed_file
    db.init_db()
    conn = db.connect()
    settings = config.Settings(conn)
    emb = Embedder.get(settings["embedding_model"], settings["device"], config.MODEL_DIR)
    folder = Path(a.folder)
    files = [f for f in sorted(folder.rglob("*")) if f.is_file() and f.suffix.lower() in audio.AUDIO_EXTS]
    if a.replace:
        conn.execute("DELETE FROM cohort")
    n = 0
    with tempfile.TemporaryDirectory() as td:
        for i, f in enumerate(files):
            wav_path = Path(td) / f"{i}.wav"
            audio.to_wav16k(f, wav_path)
            wav = audio.load_wav(wav_path)
            if len(wav) < audio.SR:
                continue
            mean, _ = embed_file(emb, wav, audio.SR, float(settings["max_chunk_seconds"]))
            conn.execute("INSERT INTO cohort(label, embedding) VALUES(?, ?)", (f"file:{f.name}", db.emb_to_blob(mean)))
            n += 1
            print(f"\r  {i+1}/{len(files)}", end="", flush=True)
    db.bump_prints_version(conn)
    total = conn.execute("SELECT COUNT(*) FROM cohort").fetchone()[0]
    print(f"\nadded {n} cohort embeddings (total explicit cohort: {total})")


def cmd_persons(a):
    db.init_db()
    conn = db.connect()
    for p in db.rows(conn, "SELECT p.*, (SELECT COUNT(*) FROM prints WHERE person_id=p.id) AS n_prints,"
                           " (SELECT COUNT(DISTINCT source_id) FROM source_speakers WHERE person_id=p.id) AS n_sources"
                           " FROM persons p ORDER BY kind DESC, name"):
        print(f"#{p['id']:<5} {p['kind']:<12} prints={p['n_prints']:<4} sources={p['n_sources']:<4} {p['name']}")


def cmd_settings(a):
    db.init_db()
    conn = db.connect()
    s = config.Settings(conn)
    if a.set:
        for kv in a.set:
            k, _, v = kv.partition("=")
            s.set(k, v)
            print(f"{k} = {s.get(k)!r}")
        db.bump_prints_version(conn)
    else:
        for k, v in s.all().items():
            print(f"{k:<28} {v!r}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="voiceflock", description=f"VoiceFlock {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web UI + API (+ a background worker)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-worker", action="store_true", help="don't run a worker in this process")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("worker", help="run a job-queue worker process")
    s.add_argument("--name")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("ingest", help="queue files/folders/URLs for processing")
    s.add_argument("inputs", nargs="+")
    s.add_argument("--mode", choices=["bulk", "match", "enroll"], default="bulk")
    s.add_argument("--person", help="person name (enroll mode)")
    s.add_argument("--incognito", action="store_true")
    s.add_argument("--run", action="store_true", help="process the queue in this process after queuing")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("enroll", help="enrol single-speaker clips for NAME (no diarization)")
    s.add_argument("name")
    s.add_argument("clips", nargs="+")
    s.set_defaults(fn=cmd_enroll)

    s = sub.add_parser("calibrate", help="fit calibration from <folder>/<speaker>/<clips> (+ optional pairs.csv)")
    s.add_argument("folder")
    s.add_argument("--no-cohort", action="store_true", help="don't add these speakers to the s-norm cohort")
    s.add_argument("--replace-cohort", action="store_true", help="replace the explicit cohort with these speakers")
    s.set_defaults(fn=cmd_calibrate)

    s = sub.add_parser("evaluate", help="rank-1 accuracy, DET curve, miss@1%FA on a labelled folder")
    s.add_argument("folder")
    s.add_argument("--out", default="eval_out")
    s.add_argument("--enroll-per-speaker", type=int, default=1)
    s.add_argument("--against-gallery", action="store_true", help="identify probes against the live gallery instead")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("cohort", help="add impostor clips for score normalisation")
    s.add_argument("folder")
    s.add_argument("--replace", action="store_true")
    s.set_defaults(fn=cmd_cohort)

    s = sub.add_parser("persons", help="list people / unknown voices")
    s.set_defaults(fn=cmd_persons)

    s = sub.add_parser("settings", help="show or set settings (key=value)")
    s.add_argument("--set", nargs="*", metavar="KEY=VALUE")
    s.set_defaults(fn=cmd_settings)
    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    _log(a.verbose)
    a.fn(a)


if __name__ == "__main__":
    main()
