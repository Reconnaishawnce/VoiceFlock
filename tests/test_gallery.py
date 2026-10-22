import numpy as np

from voiceflock import config, gallery
from tests.conftest import rand_unit, speaker_cluster


def make_gallery(conn, rng, n_people=8, prints_each=4, named=True):
    settings = config.Settings(conn)
    centres = {}
    for i in range(n_people):
        name = f"P{i}"
        cur = conn.execute("INSERT INTO persons(name, kind) VALUES(?, ?)", (name, "named" if named else "provisional"))
        pid = cur.lastrowid
        c = rand_unit(rng)
        centres[pid] = c
        gallery.add_prints(conn, pid, 100 + i, None, c, speaker_cluster(rng, c, prints_each), "cli")
    return centres, settings


def test_search_ranks_true_speaker_first_raw_mode(conn):
    rng = np.random.default_rng(0)
    centres, settings = make_gallery(conn, rng)
    settings.set("cohort_auto", False)  # no cohort -> raw cosine mode
    g = gallery.load_gallery(conn, settings)
    assert g.score_mode == "raw"
    for pid, c in centres.items():
        probe = speaker_cluster(rng, c, 1)[0]
        cands = g.search(probe, k=5)
        assert cands[0].person_id == pid
        assert cands[0].rank == 1 and cands[1].rank == 2
        assert cands[0].margin == cands[0].probability - cands[1].probability
        assert 0 <= cands[0].probability <= 1
        assert cands[0].probability > cands[-1].probability


def test_snorm_mode_kicks_in_with_cohort(conn):
    rng = np.random.default_rng(1)
    centres, settings = make_gallery(conn, rng, n_people=30)
    settings.set("cohort_min_size", 20)
    g = gallery.load_gallery(conn, settings)
    assert g.score_mode == "snorm"
    assert len(g.cohort) == 30
    pid, c = next(iter(centres.items()))
    probe = speaker_cluster(rng, c, 1)[0]
    cands = g.search(probe, k=5)
    assert cands[0].person_id == pid
    assert cands[0].score_mode == "snorm"
    # normalised score of the true speaker should be clearly positive (z-units)
    assert cands[0].norm_score > 2.0
    # an unrelated probe should land below threshold for everyone
    stranger = rand_unit(rng)
    cs = g.search(stranger, k=5)
    assert cs[0].probability < 0.5


def test_exclude_source_prevents_self_match(conn):
    rng = np.random.default_rng(2)
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    cur = conn.execute("INSERT INTO persons(name, kind) VALUES('U1', 'provisional')")
    pid = cur.lastrowid
    c = rand_unit(rng)
    gallery.add_prints(conn, pid, 7, None, c, [], "auto-cluster")
    g = gallery.load_gallery(conn, settings)
    assert g.search(c, k=5, exclude_source=7) == []
    assert g.search(c, k=5, exclude_source=8)[0].person_id == pid


def test_kinds_filter_and_prune(conn):
    rng = np.random.default_rng(3)
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    cur = conn.execute("INSERT INTO persons(name, kind) VALUES('U', 'provisional')")
    upid = cur.lastrowid
    cur = conn.execute("INSERT INTO persons(name, kind) VALUES('N', 'named')")
    npid = cur.lastrowid
    c = rand_unit(rng)
    gallery.add_prints(conn, upid, 1, None, c, speaker_cluster(rng, c, 10), "auto-cluster")
    gallery.add_prints(conn, npid, 2, None, c, [], "enroll")
    g = gallery.load_gallery(conn, settings)
    assert {x.person_id for x in g.search(c, k=5, kinds=("named",))} == {npid}
    removed = gallery.prune_prints(conn, upid, 4)
    assert removed == 7
    assert conn.execute("SELECT COUNT(*) FROM prints WHERE person_id=?", (upid,)).fetchone()[0] == 4
    # mean print is kept first
    assert conn.execute("SELECT is_mean FROM prints WHERE person_id=? ORDER BY id LIMIT 1", (upid,)).fetchone()[0] == 1


def test_gallery_cache_reloads_on_version_bump(conn):
    rng = np.random.default_rng(4)
    settings = config.Settings(conn)
    settings.set("cohort_auto", False)
    cache = gallery.GalleryCache()
    g0 = cache.get(conn, settings)
    assert len(g0) == 0
    cur = conn.execute("INSERT INTO persons(name, kind) VALUES('A', 'named')")
    gallery.add_prints(conn, cur.lastrowid, None, None, rand_unit(rng), [], "cli")
    g1 = cache.get(conn, settings)
    assert len(g1) == 1 and g1 is not g0
    assert cache.get(conn, settings) is g1


def test_calibration_fit_separates_scores():
    rng = np.random.default_rng(5)
    t = rng.normal(0.6, 0.1, 500)
    n = rng.normal(0.2, 0.1, 500)
    cal = gallery.Calibration.fit(t, n, "raw")
    assert cal.prob(0.7) > 0.95
    assert cal.prob(0.1) < 0.05
    assert abs(cal.prob(0.4) - 0.5) < 0.1


def test_cohort_stats_exclusions():
    rng = np.random.default_rng(6)
    emb = np.stack([rand_unit(rng) for _ in range(50)])
    coh = gallery.Cohort(emb, np.arange(50), np.full(50, -1), topk=10)
    v = emb[3]
    mu_all, _ = coh.stats(v)
    mu_ex, _ = coh.stats(v, exclude_person=3)
    assert mu_all > mu_ex  # the identical entry was removed from the top-k
