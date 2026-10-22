"""Re-run gallery matching for already-processed recordings (no ML needed).

Embeddings are stored per recording-speaker, so when you name an unknown voice
or enroll somebody, every earlier recording can be re-checked in seconds and
retroactive alerts appear ("Alice was also in that file from last month")."""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

from . import config, db, gallery

log = logging.getLogger(__name__)


def rematch(conn: sqlite3.Connection, source_ids: list[int] | None = None,
            gcache: gallery.GalleryCache | None = None, progress: Callable[[str, float], None] | None = None) -> dict:
    gcache = gcache or gallery.GalleryCache()
    settings = config.Settings(conn)
    k = int(settings["num_candidates"])
    match_thr = float(settings["match_threshold"])
    cluster_thr = float(settings["unknown_cluster_threshold"])
    save_unknown = bool(settings["save_unknown_voices"])
    max_pp = int(settings["max_prints_per_person"])

    q = ("SELECT ss.*, s.incognito AS src_incognito, p.kind AS person_kind FROM source_speakers ss"
         " JOIN sources s ON s.id=ss.source_id LEFT JOIN persons p ON p.id=ss.person_id"
         " WHERE ss.embedding IS NOT NULL AND (ss.assignment IS NULL OR ss.assignment='auto')")
    params: tuple = ()
    if source_ids:
        q += f" AND ss.source_id IN ({','.join('?' * len(source_ids))})"
        params = tuple(source_ids)
    speakers = db.rows(conn, q, params)
    stats = {"speakers": len(speakers), "new_alerts": 0, "clustered": 0}
    for i, sp in enumerate(speakers):
        if progress and i % 20 == 0:
            progress("rematching", i / max(1, len(speakers)))
        emb = db.blob_to_emb(sp["embedding"])
        with db.tx(conn):
            g = gcache.get(conn, settings)
            rejected = {r["person_id"] for r in conn.execute(
                "SELECT person_id FROM matches WHERE source_speaker_id=? AND status='rejected'", (sp["id"],))}
            was_alert = db.row(conn, "SELECT 1 AS x FROM matches m JOIN persons p ON p.id=m.person_id"
                                     " WHERE m.source_speaker_id=? AND m.rank=1 AND m.status='auto' AND p.kind='named'",
                               (sp["id"],)) is not None
            conn.execute("DELETE FROM matches WHERE source_speaker_id=? AND status IN ('open','auto')", (sp["id"],))
            cands = [c for c in g.search(emb, k=k + len(rejected), exclude_source=int(sp["source_id"]))
                     if c.person_id not in rejected][:k]
            for r, c in enumerate(cands):
                c.rank = r + 1
                c.margin = c.probability - (cands[r + 1].probability if r + 1 < len(cands) else 0.0)
                conn.execute(
                    "INSERT INTO matches(source_speaker_id, person_id, rank, raw_score, norm_score, probability, margin,"
                    " score_mode, status) VALUES(?,?,?,?,?,?,?,?, 'open')",
                    (sp["id"], c.person_id, c.rank, c.raw_score, c.norm_score, c.probability, c.margin, c.score_mode))
            top = cands[0] if cands else None
            incognito = bool(sp["src_incognito"])
            if top and top.kind == "named" and top.probability >= match_thr:
                conn.execute("UPDATE matches SET status='auto' WHERE source_speaker_id=? AND rank=1", (sp["id"],))
                if sp["person_id"] is None and not incognito:
                    conn.execute("UPDATE source_speakers SET person_id=?, assignment='auto' WHERE id=?",
                                 (top.person_id, sp["id"]))
                if not was_alert:
                    stats["new_alerts"] += 1
            elif (top and top.kind == "provisional" and top.probability >= cluster_thr and sp["person_id"] is None
                  and not incognito and save_unknown):
                chunks = []  # chunk embeddings were never stored for unassigned speakers; mean is enough
                gallery.add_prints(conn, top.person_id, int(sp["source_id"]), int(sp["id"]), emb, chunks,
                                   "auto-cluster", sp["speech_seconds"], sp["quality_score"])
                gallery.prune_prints(conn, top.person_id, max_pp)
                conn.execute("UPDATE matches SET status='auto' WHERE source_speaker_id=? AND rank=1", (sp["id"],))
                conn.execute("UPDATE source_speakers SET person_id=?, assignment='auto' WHERE id=?",
                             (top.person_id, sp["id"]))
                stats["clustered"] += 1
            elif sp["person_id"] is not None and sp["person_kind"] == "provisional" and top \
                    and top.person_id == sp["person_id"]:
                # still its own unnamed cluster: keep the auto flag so "heard in N recordings" stays accurate
                conn.execute("UPDATE matches SET status='auto' WHERE source_speaker_id=? AND rank=1", (sp["id"],))
    if progress:
        progress("rematching", 1.0)
    return stats
