import numpy as np

from voiceflock import metrics


def test_eer_and_miss_at_fa():
    rng = np.random.default_rng(0)
    t = rng.normal(3.0, 1.0, 2000)
    n = rng.normal(0.0, 1.0, 20000)
    e, thr = metrics.eer(t, n)
    assert 0.03 < e < 0.1
    assert 1.0 < thr < 2.0
    m, thr1 = metrics.miss_at_fa(t, n, 0.01)
    _, fa, miss = metrics.det_curve(t, n)
    assert m < 0.4 and thr1 > thr
    assert np.all(np.diff(fa) <= 1e-12) and np.all(np.diff(miss) >= -1e-12)
    assert 0 < metrics.min_dcf(t, n) < 1


def test_perfect_separation():
    t = np.array([5.0, 6.0, 7.0])
    n = np.array([0.0, 1.0, 2.0])
    e, _ = metrics.eer(t, n)
    assert e == 0.0
    m, _ = metrics.miss_at_fa(t, n, 0.01)
    assert m == 0.0


def test_det_plot(tmp_path):
    rng = np.random.default_rng(1)
    ok = metrics.save_det_plot(rng.normal(2, 1, 200), rng.normal(0, 1, 200), str(tmp_path / "det.png"))
    if ok:
        assert (tmp_path / "det.png").stat().st_size > 0
