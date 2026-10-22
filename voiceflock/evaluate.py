"""`voiceflock evaluate <folder>`: closed-set identification (rank-1) and verification
(EER, DET curve, miss rate at 1% FA) on a labelled folder, using the live gallery's
cohort and calibration so numbers reflect what the app would output."""
from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import numpy as np

from . import config, db, gallery, labeled, metrics
from .embed import Embedder

log = logging.getLogger(__name__)


def run(folder: Path, out_dir: Path, enroll_per_speaker: int = 1, against_gallery: bool = False) -> dict:
    db.init_db()
    conn = db.connect()
    settings = config.Settings(conn)
    embedder = Embedder.get(settings["embedding_model"], settings["device"], config.MODEL_DIR)
    clips = labeled.embed_folder(folder, embedder, float(settings["max_chunk_seconds"]),
                                 progress=lambda i, n: print(f"\r  {i}/{n} clips", end="", flush=True))
    print()
    out_dir.mkdir(parents=True, exist_ok=True)
    live = gallery.load_gallery(conn, settings)
    cohort, cals = live.cohort, live.calibrations

    # ---- verification: all pairs ----
    tgt, non = labeled.pairs_from_clips(clips, folder)
    scores_t, scores_n, probs_t, probs_n = [], [], [], []
    tmp = gallery.Gallery(emb=np.zeros((0, config.EMBED_DIM), np.float32), print_ids=np.zeros(0, np.int64),
                          person_ids=np.zeros(0, np.int64), source_ids=np.zeros(0, np.int64), persons={},
                          cohort=cohort, calibrations=cals, cohort_min_size=int(settings["cohort_min_size"]))
    for i, j in tgt:
        _, s, p = tmp.score_pair(clips[i].mean, clips[j].mean)
        scores_t.append(s); probs_t.append(p)
    for i, j in non:
        _, s, p = tmp.score_pair(clips[i].mean, clips[j].mean)
        scores_n.append(s); probs_n.append(p)
    scores_t, scores_n = np.asarray(scores_t), np.asarray(scores_n)
    e, thr_e = metrics.eer(scores_t, scores_n) if len(scores_t) and len(scores_n) else (float("nan"), float("nan"))
    m1, thr1 = metrics.miss_at_fa(scores_t, scores_n, 0.01) if len(scores_t) and len(scores_n) else (float("nan"), float("nan"))
    mdcf = metrics.min_dcf(scores_t, scores_n) if len(scores_t) and len(scores_n) else float("nan")
    thr_p, fa, miss = metrics.det_curve(scores_t, scores_n)
    with open(out_dir / "det.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["threshold", "false_alarm_rate", "miss_rate", "probability"])
        for t, f, m in zip(thr_p, fa, miss):
            w.writerow([f"{t:.5f}", f"{f:.5f}", f"{m:.5f}", f"{cals[tmp.score_mode].prob(float(t)):.4f}"])
    plotted = metrics.save_det_plot(scores_t, scores_n, str(out_dir / "det.png"),
                                    title=f"DET ({tmp.score_mode}, EER={e*100:.2f}%)") if len(scores_t) else False

    # ---- identification: enrol first N clips per speaker, probe the rest ----
    by_spk: dict[str, list[int]] = {}
    for i, c in enumerate(clips):
        by_spk.setdefault(c.speaker, []).append(i)
    enrol_idx, probe_idx = [], []
    for spk, idxs in by_spk.items():
        if len(idxs) <= enroll_per_speaker:
            continue
        enrol_idx += idxs[:enroll_per_speaker]
        probe_idx += idxs[enroll_per_speaker:]
    rank1 = rank5 = 0
    rows = []
    if enrol_idx and probe_idx:
        if against_gallery:
            g = live
            names = {pid: info["name"] for pid, info in g.persons.items()}
        else:
            spk_ids = {s: k + 1 for k, s in enumerate(sorted(by_spk))}
            embs, pids, prids, sids = [], [], [], []
            for i in enrol_idx:
                c = clips[i]
                for e_ in [c.mean] + list(c.chunks):
                    embs.append(e_); pids.append(spk_ids[c.speaker]); prids.append(len(prids) + 1); sids.append(i)
            g = gallery.Gallery(emb=gallery.l2n(np.stack(embs)), print_ids=np.asarray(prids), person_ids=np.asarray(pids),
                                source_ids=np.asarray(sids), persons={v: {"name": k, "kind": "named"} for k, v in spk_ids.items()},
                                cohort=cohort, calibrations=cals, cohort_min_size=int(settings["cohort_min_size"]))
            names = {v: k for k, v in spk_ids.items()}
        for i in probe_idx:
            c = clips[i]
            cands = g.search(c.mean, k=5)
            top = [names.get(x.person_id, str(x.person_id)) for x in cands]
            hit1 = bool(top) and top[0] == c.speaker
            hit5 = c.speaker in top
            rank1 += hit1; rank5 += hit5
            rows.append({"probe": str(c.path.relative_to(folder)), "truth": c.speaker, "top1": top[0] if top else None,
                         "prob": round(cands[0].probability, 4) if cands else None,
                         "margin": round(cands[0].margin, 4) if cands else None, "hit1": hit1, "hit5": hit5})
        with open(out_dir / "identification.csv", "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["probe"])
            w.writeheader(); w.writerows(rows)

    n_probe = len(probe_idx)
    report = {
        "n_clips": len(clips), "n_speakers": len(by_spk), "score_mode": tmp.score_mode, "cohort_size": len(cohort),
        "verification": {"n_target": int(len(scores_t)), "n_nontarget": int(len(scores_n)), "eer": e,
                         "eer_threshold": thr_e, "eer_prob": cals[tmp.score_mode].prob(thr_e) if np.isfinite(thr_e) else None,
                         "miss_rate_at_1pct_fa": m1, "threshold_at_1pct_fa": thr1,
                         "prob_at_1pct_fa": cals[tmp.score_mode].prob(thr1) if np.isfinite(thr1) else None,
                         "min_dcf_p0.01": mdcf, "det_csv": str(out_dir / "det.csv"),
                         "det_png": str(out_dir / "det.png") if plotted else None},
        "identification": {"enroll_per_speaker": enroll_per_speaker, "n_probes": n_probe,
                           "rank1_accuracy": rank1 / n_probe if n_probe else None,
                           "rank5_accuracy": rank5 / n_probe if n_probe else None,
                           "against_live_gallery": against_gallery},
        "current_match_threshold": float(settings["match_threshold"]),
    }
    with open(out_dir / "report.json", "w") as fh:
        json.dump(report, fh, indent=2)
    conn.close()
    return report
