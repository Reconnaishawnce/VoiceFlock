"""Verification metrics on 1-D scores: EER, DET curve, miss rate at a fixed false-alarm rate."""
from __future__ import annotations

import numpy as np


def det_curve(target: np.ndarray, nontarget: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (thresholds, false_alarm_rate, miss_rate) sweeping every distinct score."""
    target = np.asarray(target, dtype=np.float64)
    nontarget = np.asarray(nontarget, dtype=np.float64)
    thr = np.unique(np.concatenate([target, nontarget]))
    # accept if score >= threshold
    ts = np.sort(target)
    ns = np.sort(nontarget)
    # miss: targets below threshold; FA: nontargets at/above threshold
    miss = np.searchsorted(ts, thr, side="left") / max(1, len(ts))
    fa = 1.0 - np.searchsorted(ns, thr, side="left") / max(1, len(ns))
    return thr, fa, miss


def eer(target: np.ndarray, nontarget: np.ndarray) -> tuple[float, float]:
    """(EER, threshold at EER)."""
    thr, fa, miss = det_curve(target, nontarget)
    if thr.size == 0:
        return float("nan"), float("nan")
    i = int(np.argmin(np.abs(fa - miss)))
    return float((fa[i] + miss[i]) / 2.0), float(thr[i])


def miss_at_fa(target: np.ndarray, nontarget: np.ndarray, fa_rate: float = 0.01) -> tuple[float, float]:
    """Miss rate at the loosest threshold whose false-alarm rate is <= fa_rate; returns (miss, threshold)."""
    thr, fa, miss = det_curve(target, nontarget)
    ok = np.where(fa <= fa_rate)[0]
    if ok.size == 0:
        return 1.0, float("nan")
    i = int(ok[0])
    return float(miss[i]), float(thr[i])


def min_dcf(target: np.ndarray, nontarget: np.ndarray, p_target: float = 0.01, c_miss: float = 1.0,
            c_fa: float = 1.0) -> float:
    thr, fa, miss = det_curve(target, nontarget)
    dcf = c_miss * p_target * miss + c_fa * (1 - p_target) * fa
    norm = min(c_miss * p_target, c_fa * (1 - p_target))
    return float(dcf.min() / norm) if dcf.size else float("nan")


def save_det_plot(target: np.ndarray, nontarget: np.ndarray, path: str, title: str = "DET") -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from scipy.stats import norm  # noqa: F401
        _probit = norm.ppf
    except Exception:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            from statistics import NormalDist
            _nd = NormalDist()
            _probit = np.vectorize(lambda p: _nd.inv_cdf(min(max(p, 1e-6), 1 - 1e-6)))
        except Exception:
            return False
    _, fa, miss = det_curve(target, nontarget)
    fa = np.clip(fa, 1e-5, 1 - 1e-5)
    miss = np.clip(miss, 1e-5, 1 - 1e-5)
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot(_probit(fa), _probit(miss))
    ticks = [0.001, 0.01, 0.05, 0.1, 0.2, 0.4, 0.6, 0.8]
    ax.set_xticks(_probit(ticks))
    ax.set_xticklabels([f"{t*100:g}" for t in ticks])
    ax.set_yticks(_probit(ticks))
    ax.set_yticklabels([f"{t*100:g}" for t in ticks])
    ax.set_xlabel("False alarm rate (%)")
    ax.set_ylabel("Miss rate (%)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True
