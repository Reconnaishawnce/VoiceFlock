"""Gallery search, adaptive s-norm and calibration.

All embeddings are L2-normalised so inner product == cosine similarity.

Score pipeline for a probe embedding e against print p of person P:
  raw      = cos(e, p)
  z-norm   = (raw - mean_k(cos(e, C))) / std_k(cos(e, C))      (probe vs cohort, top-k)
  t-norm   = (raw - mean_k(cos(p, C))) / std_k(cos(p, C))      (print vs cohort, top-k)
  s-norm   = (z + t) / 2
  prob     = sigmoid(a * (score + b))                            (logistic calibration)
Per person we keep the best print score; candidates are ranked by probability.
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

import numpy as np

from . import config, db

try:  # faiss is optional: numpy fallback does the same thing for small galleries
    import faiss  # type: ignore
except Exception:  # pragma: no cover
    faiss = None


def l2n(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    if x.ndim == 1:
        n = np.linalg.norm(x) + 1e-9
        return x / n
    n = np.linalg.norm(x, axis=1, keepdims=True) + 1e-9
    return x / n


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    ex = math.exp(x)
    return ex / (1.0 + ex)


# ---------------------------------------------------------------------------
# Cohort + adaptive s-norm
# ---------------------------------------------------------------------------

@dataclass
class Cohort:
    emb: np.ndarray                      # (N, d) normalised
    person_ids: np.ndarray               # (N,) int, -1 if unlabelled
    source_ids: np.ndarray               # (N,) int, -1 if unlabelled
    topk: int = 200

    def __len__(self) -> int:
        return int(self.emb.shape[0])

    def stats(self, vec: np.ndarray, exclude_person: int = -1, exclude_source: int = -1) -> tuple[float, float]:
        """Mean/std of the top-k cohort similarities for `vec` (adaptive s-norm)."""
        s = self.emb @ vec
        mask = np.ones(len(s), dtype=bool)
        if exclude_person >= 0:
            mask &= self.person_ids != exclude_person
        if exclude_source >= 0:
            mask &= self.source_ids != exclude_source
        s = s[mask]
        if s.size < 3:
            return 0.0, 1.0
        k = min(self.topk, s.size)
        top = np.partition(s, s.size - k)[s.size - k:]
        mu = float(top.mean())
        sd = float(top.std())
        return mu, max(sd, 1e-3)


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

@dataclass
class Calibration:
    mode: str      # raw | snorm
    a: float
    b: float

    def prob(self, score: float) -> float:
        return sigmoid(self.a * (score + self.b))

    @staticmethod
    def fit(target: np.ndarray, nontarget: np.ndarray, mode: str) -> "Calibration":
        """Logistic regression (balanced) on 1-D scores -> Calibration."""
        from sklearn.linear_model import LogisticRegression

        x = np.concatenate([target, nontarget]).reshape(-1, 1).astype(np.float64)
        y = np.concatenate([np.ones(len(target)), np.zeros(len(nontarget))])
        lr = LogisticRegression(class_weight="balanced", C=1e4, max_iter=2000)
        lr.fit(x, y)
        a = float(lr.coef_[0][0])
        b0 = float(lr.intercept_[0])
        # p = sigmoid(a*s + b0) = sigmoid(a*(s + b0/a))
        return Calibration(mode=mode, a=a, b=b0 / a if abs(a) > 1e-9 else 0.0)


def load_calibrations(conn: sqlite3.Connection, settings: config.Settings) -> dict[str, Calibration]:
    cals = {
        "raw": Calibration("raw", settings["calib_raw_a"], settings["calib_raw_b"]),
        "snorm": Calibration("snorm", settings["calib_snorm_a"], settings["calib_snorm_b"]),
    }
    for r in conn.execute("SELECT mode, a, b FROM calibrations WHERE active=1"):
        cals[r["mode"]] = Calibration(r["mode"], float(r["a"]), float(r["b"]))
    return cals


# ---------------------------------------------------------------------------
# Gallery
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    person_id: int
    name: str
    kind: str
    raw_score: float
    norm_score: float
    probability: float
    score_mode: str
    print_id: int
    margin: float = 0.0
    rank: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Gallery:
    """In-memory snapshot of the prints table with a FAISS/numpy index."""

    emb: np.ndarray                          # (M, d)
    print_ids: np.ndarray
    person_ids: np.ndarray
    source_ids: np.ndarray
    persons: dict[int, dict]                 # person_id -> {name, kind}
    cohort: Cohort
    calibrations: dict[str, Calibration]
    cohort_min_size: int = 20
    version: int = 0
    _index: object = field(default=None, repr=False)
    _tstats: dict[int, tuple[float, float]] = field(default_factory=dict, repr=False)

    def __len__(self) -> int:
        return int(self.emb.shape[0])

    @property
    def score_mode(self) -> str:
        return "snorm" if len(self.cohort) >= self.cohort_min_size else "raw"

    def _ensure_index(self):
        if self._index is None and faiss is not None and len(self) > 0:
            idx = faiss.IndexFlatIP(self.emb.shape[1])
            idx.add(np.ascontiguousarray(self.emb, dtype=np.float32))
            self._index = idx
        return self._index

    def _tstat(self, i: int) -> tuple[float, float]:
        pid = int(self.print_ids[i])
        st = self._tstats.get(pid)
        if st is None:
            st = self.cohort.stats(self.emb[i], exclude_person=int(self.person_ids[i]),
                                   exclude_source=int(self.source_ids[i]))
            self._tstats[pid] = st
        return st

    def search(self, probe: np.ndarray, k: int = 5, exclude_source: int = -1,
               kinds: tuple[str, ...] | None = None, min_prob: float = 0.0) -> list[Candidate]:
        """Top-k persons for a probe embedding.

        exclude_source: never match a probe against prints taken from its own recording.
        kinds: restrict to person kinds (e.g. ('named',)).
        """
        if len(self) == 0:
            return []
        probe = l2n(probe)
        mode = self.score_mode
        # Pull enough neighbours that per-person aggregation still yields k persons.
        n_hit = min(len(self), max(k * 30, 200))
        index = self._ensure_index()
        if index is not None:
            scores, idxs = index.search(probe.reshape(1, -1).astype(np.float32), n_hit)
            scores, idxs = scores[0], idxs[0]
        else:
            s = self.emb @ probe
            idxs = np.argsort(-s)[:n_hit]
            scores = s[idxs]

        if mode == "snorm":
            zmu, zsd = self.cohort.stats(probe, exclude_source=exclude_source)
        best: dict[int, Candidate] = {}
        for sc, i in zip(scores, idxs):
            if i < 0:
                continue
            pid = int(self.person_ids[i])
            if exclude_source >= 0 and int(self.source_ids[i]) == exclude_source:
                continue
            info = self.persons.get(pid)
            if info is None or (kinds and info["kind"] not in kinds):
                continue
            raw = float(sc)
            if mode == "snorm":
                tmu, tsd = self._tstat(int(i))
                norm = 0.5 * ((raw - zmu) / zsd + (raw - tmu) / tsd)
            else:
                norm = raw
            prob = self.calibrations[mode].prob(norm)
            cur = best.get(pid)
            if cur is None or norm > cur.norm_score:
                best[pid] = Candidate(pid, info["name"], info["kind"], raw, norm, prob, mode, int(self.print_ids[i]))
        cands = sorted(best.values(), key=lambda c: -c.norm_score)[:k]
        for r, c in enumerate(cands):
            c.rank = r + 1
            nxt = cands[r + 1].probability if r + 1 < len(cands) else 0.0
            c.margin = c.probability - nxt
        return [c for c in cands if c.probability >= min_prob]

    def score_pair(self, a: np.ndarray, b: np.ndarray) -> tuple[float, float, float]:
        """(raw, normalised, probability) for two embeddings using this gallery's cohort+calibration."""
        a, b = l2n(a), l2n(b)
        raw = float(a @ b)
        mode = self.score_mode
        if mode == "snorm":
            zmu, zsd = self.cohort.stats(a)
            tmu, tsd = self.cohort.stats(b)
            norm = 0.5 * ((raw - zmu) / zsd + (raw - tmu) / tsd)
        else:
            norm = raw
        return raw, norm, self.calibrations[mode].prob(norm)


def build_cohort(conn: sqlite3.Connection, settings: config.Settings) -> Cohort:
    embs, pids, sids = [], [], []
    for r in conn.execute("SELECT embedding FROM cohort"):
        embs.append(db.blob_to_emb(r["embedding"]))
        pids.append(-1)
        sids.append(-1)
    if settings["cohort_auto"]:
        # one mean embedding per person (mean of its mean-prints, else all prints)
        rows = conn.execute(
            "SELECT person_id, source_id, embedding, is_mean FROM prints ORDER BY person_id").fetchall()
        by_p: dict[int, list] = {}
        src_p: dict[int, int] = {}
        for r in rows:
            by_p.setdefault(int(r["person_id"]), []).append(db.blob_to_emb(r["embedding"]))
            src_p.setdefault(int(r["person_id"]), int(r["source_id"] or -1))
        for pid, lst in by_p.items():
            embs.append(l2n(np.mean(np.stack(lst), axis=0)))
            pids.append(pid)
            sids.append(src_p.get(pid, -1))
    if not embs:
        emb = np.zeros((0, config.EMBED_DIM), dtype=np.float32)
    else:
        emb = l2n(np.stack(embs))
    return Cohort(emb, np.asarray(pids, dtype=np.int64), np.asarray(sids, dtype=np.int64),
                  topk=int(settings["cohort_topk"]))


def load_gallery(conn: sqlite3.Connection, settings: config.Settings | None = None) -> Gallery:
    settings = settings or config.Settings(conn)
    persons = {int(r["id"]): {"name": r["name"], "kind": r["kind"]}
               for r in conn.execute("SELECT id, name, kind FROM persons")}
    rows = conn.execute("SELECT id, person_id, source_id, embedding FROM prints").fetchall()
    if rows:
        emb = l2n(np.stack([db.blob_to_emb(r["embedding"]) for r in rows]))
        print_ids = np.asarray([r["id"] for r in rows], dtype=np.int64)
        person_ids = np.asarray([r["person_id"] for r in rows], dtype=np.int64)
        source_ids = np.asarray([r["source_id"] if r["source_id"] is not None else -1 for r in rows], dtype=np.int64)
    else:
        emb = np.zeros((0, config.EMBED_DIM), dtype=np.float32)
        print_ids = person_ids = source_ids = np.zeros((0,), dtype=np.int64)
    return Gallery(
        emb=emb, print_ids=print_ids, person_ids=person_ids, source_ids=source_ids, persons=persons,
        cohort=build_cohort(conn, settings), calibrations=load_calibrations(conn, settings),
        cohort_min_size=int(settings["cohort_min_size"]), version=db.prints_version(conn),
    )


class GalleryCache:
    """Reload the gallery only when prints changed."""

    def __init__(self):
        self._g: Gallery | None = None

    def get(self, conn: sqlite3.Connection, settings: config.Settings | None = None) -> Gallery:
        v = db.prints_version(conn)
        if self._g is None or self._g.version != v:
            self._g = load_gallery(conn, settings)
        return self._g

    def invalidate(self):
        self._g = None


# ---------------------------------------------------------------------------
# Writing prints
# ---------------------------------------------------------------------------

def add_prints(conn: sqlite3.Connection, person_id: int, source_id: int | None, source_speaker_id: int | None,
               mean_emb: np.ndarray | None, chunk_embs: list[np.ndarray], origin: str,
               speech_seconds: float | None = None, quality: float | None = None) -> int:
    n = 0
    if mean_emb is not None:
        conn.execute(
            "INSERT INTO prints(person_id, source_id, source_speaker_id, embedding, is_mean, speech_seconds, quality, origin)"
            " VALUES(?,?,?,?,1,?,?,?)",
            (person_id, source_id, source_speaker_id, db.emb_to_blob(l2n(mean_emb)), speech_seconds, quality, origin))
        n += 1
    for e in chunk_embs:
        conn.execute(
            "INSERT INTO prints(person_id, source_id, source_speaker_id, embedding, is_mean, speech_seconds, quality, origin)"
            " VALUES(?,?,?,?,0,?,?,?)",
            (person_id, source_id, source_speaker_id, db.emb_to_blob(l2n(e)), speech_seconds, quality, origin))
        n += 1
    db.bump_prints_version(conn)
    return n


def prune_prints(conn: sqlite3.Connection, person_id: int, max_prints: int) -> int:
    """Keep at most max_prints prints for a person: mean prints and confirmed/enrolled first,
    then by quality, dropping the oldest auto-clustered ones."""
    rows = conn.execute(
        "SELECT id FROM prints WHERE person_id=? ORDER BY is_mean DESC, "
        "CASE origin WHEN 'enroll' THEN 0 WHEN 'confirm' THEN 0 WHEN 'named' THEN 0 WHEN 'cli' THEN 0 ELSE 1 END, "
        "COALESCE(quality, 0) DESC, id DESC", (person_id,)).fetchall()
    extra = [r["id"] for r in rows[max_prints:]]
    if extra:
        conn.executemany("DELETE FROM prints WHERE id=?", [(i,) for i in extra])
        db.bump_prints_version(conn)
    return len(extra)
