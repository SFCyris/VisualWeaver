# SPDX-License-Identifier: AGPL-3.0-or-later
"""The retrieval counters that the measured advice is built on.

They lived only in memory, so they emptied on every restart — and Settings is most often
opened right after one, which is exactly when the advice had nothing to show. Persisting
them is what makes the feature work at all rather than in principle.

A corrupt record is discarded rather than half-loaded: a counter that is wrong is worse
than one that is absent, because the advice built on it reads as measurement either way.
"""
import json

import pytest

from visualweaver import rag_admin, redis_store, state


@pytest.fixture
def clean_stats(clean_redis, monkeypatch):
    """Point the persistence at the test client and a test-namespaced key.

    redis_store.r() resolves the APP's configured endpoint; without redirecting it these
    tests would write to whatever Redis the host happens to run on 6379.
    """
    key = clean_redis.key("rag_stats")
    monkeypatch.setattr(rag_admin, "_RAG_STATS_KEY", key, raising=False)
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: clean_redis, raising=False)
    # A running app has completed its restore; the guard is exercised on its own below.
    monkeypatch.setattr(rag_admin, "_stats_loaded", True, raising=False)
    monkeypatch.setattr(state, "_rag_stats", {}, raising=False)
    monkeypatch.setattr(state, "_rag_stats_dirty", set(), raising=False)
    yield clean_redis
    monkeypatch.setattr(state, "_rag_stats", {}, raising=False)


def _row(**kw):
    row = dict(state.RAG_STATS_TEMPLATE)
    row.update({"raw_hist": [0] * state.RAW_HIST_BUCKETS,
                "cited_hist": [0] * state.CITED_HIST_RANKS,
                "answers_with_citations": 0})
    row.update(kw)
    return row


# ── round trip ───────────────────────────────────────────────────────────────
def test_counters_survive_a_restart(clean_stats):
    hist = [0] * state.RAW_HIST_BUCKETS
    hist[14] = 37
    state._rag_stats["docs"] = _row(queries=100, hits=40, raw_hist=hist,
                                    cited_hist=[9, 4] + [0] * 18,
                                    answers_with_citations=13)
    state._rag_stats_dirty.add("docs")
    rag_admin.save_rag_stats()

    state._rag_stats.clear()                 # the restart
    rag_admin.load_rag_stats()

    s = state._rag_stats["docs"]
    assert s["queries"] == 100 and s["hits"] == 40
    assert s["raw_hist"][14] == 37
    assert s["cited_hist"][:2] == [9, 4]
    assert s["answers_with_citations"] == 13


def test_saving_clears_the_dirty_set(clean_stats):
    state._rag_stats["docs"] = _row(queries=1)
    state._rag_stats_dirty.add("docs")
    rag_admin.save_rag_stats()
    assert not state._rag_stats_dirty


def test_saving_nothing_dirty_does_not_write(clean_stats):
    """Called after every grounded turn — it must not post to Redis when idle."""
    rag_admin.save_rag_stats()
    assert clean_stats.get(rag_admin._RAG_STATS_KEY) is None


# ── a bad record must not become bad advice ──────────────────────────────────
@pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps(["a", "list"]),
    json.dumps({"docs": "not a dict"}),
    json.dumps({"docs": {"queries": "many"}}),          # wrong type for the sample count
])
def test_a_corrupt_record_is_discarded_not_half_loaded(clean_stats, payload):
    clean_stats.set(rag_admin._RAG_STATS_KEY, payload)
    rag_admin.load_rag_stats()
    assert state._rag_stats == {}


def test_a_histogram_of_the_wrong_length_is_replaced_not_trusted(clean_stats):
    """A short histogram would index out of range on the next query; a long one would
    silently skew every percentile the advice reads off it."""
    clean_stats.set(rag_admin._RAG_STATS_KEY, json.dumps(
        {"docs": {"queries": 10, "raw_hist": [1, 2, 3], "cited_hist": [1] * 99}}))
    rag_admin.load_rag_stats()
    s = state._rag_stats["docs"]
    assert len(s["raw_hist"]) == state.RAW_HIST_BUCKETS
    assert len(s["cited_hist"]) == state.CITED_HIST_RANKS
    assert sum(s["raw_hist"]) == 0            # discarded, not padded with the bad values


def test_clearing_removes_it_from_redis_too(clean_stats):
    state._rag_stats["docs"] = _row(queries=5)
    state._rag_stats_dirty.add("docs")
    rag_admin.save_rag_stats()
    rag_admin.clear_rag_stats()
    assert state._rag_stats == {}
    assert clean_stats.get(rag_admin._RAG_STATS_KEY) is None
    rag_admin.load_rag_stats()
    assert state._rag_stats == {}             # and stays gone across a reload


# ── citation recording ───────────────────────────────────────────────────────
def test_recording_citations_counts_each_rank(clean_stats):
    state.record_citations("docs", [1, 1, 3])
    assert state._rag_stats["docs"]["cited_hist"][0] == 2
    assert state._rag_stats["docs"]["cited_hist"][2] == 1
    assert state._rag_stats["docs"]["answers_with_citations"] == 1


@pytest.mark.parametrize("ranks", [[0], [-1], [state.CITED_HIST_RANKS + 1], [999]])
def test_a_rank_outside_the_histogram_is_ignored(clean_stats, ranks):
    state.record_citations("docs", ranks)
    assert sum(state._rag_stats["docs"]["cited_hist"]) == 0


def test_recording_nothing_creates_no_row(clean_stats):
    state.record_citations("docs", [])
    state.record_citations("", [1])
    assert state._rag_stats == {}


# ── the restore guard ────────────────────────────────────────────────────────
def test_nothing_is_flushed_before_the_restore_has_run(clean_stats, monkeypatch):
    """The window between the server accepting traffic and _bg_init reaching
    load_rag_stats is seconds wide. A turn landing inside it used to SET the whole key
    from a dict holding one fresh row, destroying every other instance's history."""
    state._rag_stats["docs"] = _row(queries=500)
    state._rag_stats_dirty.add("docs")
    rag_admin.save_rag_stats()
    good = clean_stats.get(rag_admin._RAG_STATS_KEY)          # the pre-existing history
    assert good is not None

    monkeypatch.setattr(rag_admin, "_stats_loaded", False, raising=False)
    state._rag_stats.clear()
    state._rag_stats["fresh"] = _row(queries=1)               # the turn that beat the restore
    state._rag_stats_dirty.add("fresh")
    rag_admin.save_rag_stats()
    assert clean_stats.get(rag_admin._RAG_STATS_KEY) == good, "history was overwritten"


def test_the_restore_does_not_discard_a_row_already_accumulating(clean_stats, monkeypatch):
    """If a turn creates a row before the restore lands, a blind update() would throw
    that row away. The in-memory one wins."""
    state._rag_stats["docs"] = _row(queries=7)
    state._rag_stats_dirty.add("docs")
    rag_admin.save_rag_stats()
    state._rag_stats.clear()
    state._rag_stats["docs"] = _row(queries=99)               # landed before the restore
    monkeypatch.setattr(rag_admin, "_stats_loaded", False, raising=False)
    rag_admin.load_rag_stats()
    assert state._rag_stats["docs"]["queries"] == 99
    assert rag_admin._stats_loaded is True                    # flushing is unblocked


def test_a_failed_flush_keeps_the_rows_dirty_for_the_next_attempt(clean_stats, monkeypatch):
    class Boom:
        def set(self, *a, **k): raise RuntimeError("redis down")
    monkeypatch.setattr(redis_store, "r", lambda *a, **k: Boom(), raising=False)
    state._rag_stats["docs"] = _row(queries=3)
    state._rag_stats_dirty.add("docs")
    rag_admin.save_rag_stats()
    assert "docs" in state._rag_stats_dirty, "the unwritten row was silently forgotten"


# ── per-field validation ─────────────────────────────────────────────────────
@pytest.mark.parametrize("row", [
    {"queries": 10, "hits": "3"},                       # wrong type on a scalar
    {"queries": 10, "hits": True},                      # bool is not a count
    {"queries": 10, "score_sum": None},
    {"queries": 10, "errors": []},
])
def test_a_row_with_a_bad_scalar_is_rejected_whole(clean_stats, row):
    """Only `queries` used to be checked, so a string in `hits` loaded fine and then
    500'd /api/rag/stats on the division.

    A bad HISTOGRAM is handled differently — see the test below. The scalars are
    interdependent (hits/queries is a rate), so one bad one poisons the row; the two
    histograms are independent of each other and of most scalars, so a bad one is
    zeroed along with just the counters that read it."""
    clean_stats.set(rag_admin._RAG_STATS_KEY, json.dumps({"docs": row}))
    rag_admin.load_rag_stats()
    assert "docs" not in state._rag_stats


def test_one_bad_row_does_not_discard_the_good_ones(clean_stats):
    clean_stats.set(rag_admin._RAG_STATS_KEY, json.dumps({
        "bad": {"queries": 10, "hits": "no"},
        "good": {"queries": 40, "hits": 20, "chunks_total": 5, "score_sum": 1.0,
                 "raw_score_sum": 2.0, "errors": 0,
                 "raw_hist": [2] * state.RAW_HIST_BUCKETS,
                 "cited_hist": [0] * state.CITED_HIST_RANKS,
                 "answers_with_citations": 3, "raw_sample": [0.6, 0.7]},
    }))
    rag_admin.load_rag_stats()
    assert "bad" not in state._rag_stats
    assert state._rag_stats["good"]["queries"] == 40
    assert state._rag_stats["good"]["raw_sample"] == [0.6, 0.7]


def test_rejecting_a_histogram_also_zeroes_the_count_that_depends_on_it(clean_stats):
    """queries surviving beside a zeroed histogram renders as a confident
    "200 queries, 0% clear this" — a red verdict built on no data."""
    for bad in ([1, 2, 3], ["a"] * state.RAW_HIST_BUCKETS, [-1] * state.RAW_HIST_BUCKETS):
        state._rag_stats.clear()
        clean_stats.set(rag_admin._RAG_STATS_KEY, json.dumps(
            {"docs": {"queries": 200, "raw_hist": bad}}))
        rag_admin.load_rag_stats()
        assert state._rag_stats["docs"]["queries"] == 0, bad
        assert sum(state._rag_stats["docs"]["raw_hist"]) == 0, bad


def test_the_score_sample_round_trips(clean_stats):
    state._rag_stats["docs"] = _row(queries=3, raw_sample=[0.61, 0.72, 0.68])
    state._rag_stats_dirty.add("docs")
    rag_admin.save_rag_stats()
    state._rag_stats.clear()
    rag_admin.load_rag_stats()
    assert state._rag_stats["docs"]["raw_sample"] == [0.61, 0.72, 0.68]


# ── what the sample costs to persist ─────────────────────────────────────────
def test_scores_are_stored_at_the_precision_the_slider_can_use():
    """Full float repr more than doubles what every grounded turn writes to Redis, and
    the extra digits cannot change a verdict: the threshold steps in 0.01."""
    r = state.new_stats_row()
    for x in (0.4567890123456789, 0.1, 1 / 3):
        r["queries"] += 1
        state._reservoir_add(r, x)
    assert r["raw_sample"] == [0.4568, 0.1, 0.3333]
    assert all(len(repr(v)) <= 6 for v in r["raw_sample"]), r["raw_sample"]


def test_the_extra_digits_change_no_verdict():
    import random
    from visualweaver import rag_advice as A
    random.seed(7)
    raw = [random.uniform(0.30, 0.80) for _ in range(500)]
    rounded = [round(x, state.RAW_SAMPLE_DP) for x in raw]
    for thr in (0.31, 0.35, 0.50, 0.66, 0.79):
        assert A.pass_rate_exact(raw, thr) == A.pass_rate_exact(rounded, thr), thr


def test_a_row_written_before_rounding_is_rounded_on_the_way_in():
    """Otherwise it keeps paying full repr on every flush until its entries roll over."""
    row = rag_admin._clean_stats_row(
        dict(state.new_stats_row(), queries=5,
             raw_sample=[0.4567890123456789, 0.987654321]))
    assert row["raw_sample"] == [0.4568, 0.9877]


def test_the_flush_stays_small_at_the_sample_cap():
    """A regression here is invisible — it costs bytes per turn, not correctness."""
    import json
    r = state.new_stats_row()
    for i in range(state.RAW_SAMPLE_SIZE * 4):
        r["queries"] += 1
        state._reservoir_add(r, 0.3 + (i % 5000) / 10000)
    assert len(r["raw_sample"]) == state.RAW_SAMPLE_SIZE
    kb = len(json.dumps({"corpus": r})) / 1024
    assert kb < 6.0, f"one instance serialises to {kb:.1f} KB per flush"
