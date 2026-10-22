"""`voiceflock calibrate <folder>`: fit logistic-regression calibration from labelled pairs,
and (optionally) refresh the explicit cohort set from the same clips."""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from . import config, db, gallery, labeled, metrics
from .embed import Embedder

log = logging.getLogger(__name__)


def run(folder: Path, add_cohort: bool = True, replace_cohort: bool = False) -> dict:
    db.init_db()
    conn = db.connect()
    settings = config.Settings(conn)
    embedder = Embedder.get(settings["embedding_model"], settings["device"], config.MODEL_DIR)
    log.info("embedding clips under %s", folder)
    clips = labeled.embed_folder(folder, embedder, float(settings["max_chunk_seconds"]),
                                 progress=lambda i, n: print(f"\r  {i}/{n} clips", end="", flush=True))
    print()
    tgt, non = labeled.pairs_from_clips(clips, folder)
    if not tgt or not non:
        raise ValueError("need both same-speaker and different-speaker pairs (>=2 clips for some speaker and >=2 speakers)")

    # Cohort for s-norm: existing explicit cohort + one mean per labelled speaker (leave-speaker-out isn't
    # possible without labels on the cohort, so we build the cohort from speaker means and exclude by index).
    speakers = sorted({c.speaker for c in clips})
    spk_mean = {s: gallery.l2n(np.mean([c.mean for c in clips if c.speaker == s], axis=0)) for s in speakers}
    with db.tx(conn):
        if replace_cohort:
            conn.execute("DELETE FROM cohort")
        if add_cohort or replace_cohort:
            conn.execute("DELETE FROM cohort WHERE label LIKE 'calib:%'")
            for s, m in spk_mean.items():
                conn.execute("INSERT INTO cohort(label, embedding) VALUES(?, ?)", (f"calib:{s}", db.emb_to_blob(m)))
    cohort = gallery.build_cohort(conn, settings)
    # When scoring a clip, its own speaker's cohort mean would leak into z-norm, so give those entries
    # the speaker's index as person_id and let stats() exclude them.
    coh_labels = [r["label"] for r in conn.execute("SELECT label FROM cohort")]
    spk_idx = {s: i for i, s in enumerate(speakers)}
    pid = np.full(len(cohort), -1, dtype=np.int64)
    for i, lab in enumerate(coh_labels):
        if lab and lab.startswith("calib:") and lab[6:] in spk_idx:
            pid[i] = spk_idx[lab[6:]]
    cohort.person_ids = pid

    # score with per-clip exclusion of own speaker
    embs = gallery.l2n(np.stack([c.mean for c in clips]))
    S = embs @ embs.T
    stats = [cohort.stats(embs[i], exclude_person=spk_idx[clips[i].speaker]) for i in range(len(clips))]
    res = {}
    for mode in ("raw", "snorm"):
        def sc(i, j):
            raw = float(S[i, j])
            if mode == "raw":
                return raw
            (mi, si), (mj, sj) = stats[i], stats[j]
            return 0.5 * ((raw - mi) / si + (raw - mj) / sj)
        t = np.asarray([sc(i, j) for i, j in tgt])
        n = np.asarray([sc(i, j) for i, j in non])
        cal = gallery.Calibration.fit(t, n, mode)
        e, thr = metrics.eer(t, n)
        m1, thr1 = metrics.miss_at_fa(t, n, 0.01)
        res[mode] = {"a": cal.a, "b": cal.b, "eer": e, "eer_threshold": thr, "miss_at_1pct_fa": m1,
                     "threshold_at_1pct_fa": thr1, "n_target": int(len(t)), "n_nontarget": int(len(n)),
                     "prob_at_eer_threshold": cal.prob(thr),
                     "prob_at_1pct_fa_threshold": cal.prob(thr1) if np.isfinite(thr1) else None}
        with db.tx(conn):
            conn.execute("UPDATE calibrations SET active=0 WHERE mode=?", (mode,))
            conn.execute("INSERT INTO calibrations(mode, a, b, n_target, n_nontarget, eer, active) VALUES(?,?,?,?,?,?,1)",
                         (mode, cal.a, cal.b, len(t), len(n), e))
    db.bump_prints_version(conn)
    res["n_clips"] = len(clips)
    res["n_speakers"] = len(speakers)
    res["cohort_size"] = len(cohort)
    res["active_mode"] = "snorm" if len(cohort) >= int(settings["cohort_min_size"]) else "raw"
    conn.close()
    return res
