# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recorded cosine only means something beside others produced the same way.

Two things change the geometry the score was measured in: the embedding model, and
whether the text embedded was the QUESTION or a HyDE hypothesis. Answer-shaped prose
sits elsewhere in the space — measured on a real 57,805-chunk index, question mode
averaged 0.676 and HyDE mode 0.750, a shift larger than the spread the threshold has to
be read against. Pooled, the sample is bimodal and the derived threshold serves neither
mode; the panel then recommends a value that was never right for anything.
"""
import json

import pytest

from visualweaver import rag_admin, rag_advice as A, state


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    monkeypatch.setattr(state, "_rag_stats", {})
    monkeypatch.setattr(state, "_rag_stats_dirty", set())
    monkeypatch.setattr(state, "_config",
                        {"embedding": {"model": "e5"}, "hyde": {"enabled": False},
                         "rag": {"similarity_threshold": 0.5, "top_k": 5,
                                 "chunk_size": 180, "hybrid_search": True},
                         "reranker": {"enabled": False, "top_n": 5},
                         "history_max_tokens": 3000})


def _search(inst, score, n=1):
    for _ in range(n):
        state._record_rag_stats(inst, [], [{"score": score}])


# ── the regime key ───────────────────────────────────────────────────────────
def test_the_regime_names_the_model_and_the_query_mode():
    assert state.current_regime() == "e5:q"
    state._config["hyde"] = {"enabled": True}
    assert state.current_regime() == "e5:h"
    state._config["embedding"] = {"model": "minilm"}
    assert state.current_regime() == "minilm:h"


def test_things_that_are_simultaneously_true_are_not_regimes():
    """A query hits three corpora at once and one threshold must serve all three, so the
    corpus is a dimension to split on, not a regime. Reranking and hybrid do not change
    the raw pre-threshold cosine at all."""
    before = state.current_regime()
    state._config["reranker"] = {"enabled": True, "top_n": 3}
    state._config["rag"]["hybrid_search"] = False
    state._config["rag"]["top_k"] = 40
    assert state.current_regime() == before


# ── the split ────────────────────────────────────────────────────────────────
def test_toggling_hyde_starts_a_clean_sample():
    _search("K", 0.62, 60)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 60)
    s = state._rag_stats["K"]
    assert s["regime"] == "e5:h"
    assert set(s["raw_sample"]) == {0.80}, "question-mode scores leaked into HyDE mode"
    assert "e5:q" in s["alt"]


def test_the_verdict_follows_the_mode_the_next_query_will_use():
    _search("K", 0.62, 60)
    assert A.build_advice(state._config, state._rag_stats)["settings"][
        "similarity_threshold"]["suggested"] == 0.62
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 60)
    assert A.build_advice(state._config, state._rag_stats)["settings"][
        "similarity_threshold"]["suggested"] == 0.80


def test_the_pooled_answer_would_have_been_a_mode_that_never_existed():
    """0.62 and 0.80 pool to ~0.71 — a value neither mode ever produced."""
    _search("K", 0.62, 60)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 60)
    sug = A.build_advice(state._config, state._rag_stats)["settings"][
        "similarity_threshold"]["suggested"]
    assert abs(sug - 0.71) > 0.05, sug


def test_switching_back_restores_the_history_rather_than_discarding_it():
    """Discarding on toggle throws away measurement the user paid for."""
    _search("K", 0.62, 60)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 60)
    state._config["hyde"] = {"enabled": False}
    _search("K", 0.62)
    s = state._rag_stats["K"]
    assert s["regime"] == "e5:q"
    assert len(s["raw_sample"]) == 61
    assert "e5:h" in s["alt"], "the HyDE history was dropped on the way back"


def test_a_changed_embedding_model_is_a_different_regime():
    """The sharper version of the same problem: cosines from two models are not
    comparable at all, and nothing else in the row would notice."""
    _search("K", 0.62, 30)
    state._config["embedding"] = {"model": "minilm"}
    _search("K", 0.31, 30)
    s = state._rag_stats["K"]
    assert set(s["raw_sample"]) == {0.31}
    assert "e5:q" in s["alt"]


# ── budget ───────────────────────────────────────────────────────────────────
def test_the_sample_budget_is_shared_not_multiplied():
    """Per-regime reservoirs must not multiply what every grounded turn writes."""
    _search("K", 0.62, state.RAW_SAMPLE_SIZE + 50)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, state.RAW_SAMPLE_SIZE + 50)
    s = state._rag_stats["K"]
    total = len(s["raw_sample"]) + sum(len(v["s"]) for v in s["alt"].values())
    assert total <= state.RAW_SAMPLE_SIZE + 2, total


def test_the_number_of_parked_regimes_is_capped():
    for i in range(6):
        state._config["embedding"] = {"model": f"m{i}"}
        _search("K", 0.5 + i / 100, 5)
    assert len(state._rag_stats["K"]["alt"]) < state.MAX_REGIMES + 1


# ── persistence ──────────────────────────────────────────────────────────────
def test_the_split_survives_a_restart():
    """Dropping these on load would silently re-pool the modes on the next restart —
    invisible, because the numbers still look plausible."""
    _search("K", 0.62, 30)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 30)
    back = rag_admin._clean_stats_row(json.loads(json.dumps(state._rag_stats["K"]))) 
    assert back["regime"] == "e5:h"
    assert "e5:q" in back["alt"]
    assert set(back["raw_sample"]) == {0.80}


def test_a_row_from_before_regimes_existed_still_loads():
    """An existing v1.9.0 row has a sample and no regime; it must keep its history."""
    legacy = {"queries": 40, "hits": 10, "chunks_total": 50, "score_sum": 6.0,
              "raw_score_sum": 24.0, "errors": 0, "no_candidates": 0, "scored": 40,
              "cache_replays": 0, "answers_with_citations": 0,
              "raw_hist": [0] * 20, "cited_hist": [0] * 20, "raw_sample": [0.6] * 40}
    back = rag_admin._clean_stats_row(legacy)
    assert back["regime"] == state.UNKNOWN_REGIME
    assert len(back["raw_sample"]) == 40
    assert back["alt"] == {}


@pytest.mark.parametrize("alt", [
    {"x": {"s": "not-a-list"}}, {"x": {"s": [None]}}, {"x": "not-a-dict"},
    {123: {"s": [0.5]}}, "not-a-dict-at-all",
])
def test_a_malformed_alt_is_rejected_not_half_loaded(alt):
    row = dict(state.new_stats_row(), queries=5, alt=alt)
    assert rag_admin._clean_stats_row(row)["alt"] == {}


# ── everything derived from scores moves with the sample ─────────────────────
def test_the_histogram_is_split_too_not_just_the_sample():
    """Parking only raw_sample left the histogram, the mean and the scored count pooled
    across modes — so the chart drawn under the slider and the percentage drawn on top of
    it came from two different populations, permanently."""
    _search("K", 0.62, 400)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 5)
    s = state._rag_stats["K"]
    assert sum(s["raw_hist"]) == 5, f"both modes still pooled: {sum(s['raw_hist'])}"
    assert s["scored"] == 5
    assert s["raw_score_sum"] == pytest.approx(4.0, abs=0.01)
    assert sum(s["alt"]["e5:q"]["h"]) == 400


def test_a_corpus_does_not_vote_off_the_previous_mode_after_a_switch():
    """_corpus_state fell back to "binned" whenever the histogram had 20 entries, so for
    the first 20 turns after any switch the corpus voted on the OLD mode's distribution —
    wanting 0.55 where the live mode needed 0.26."""
    _search("K", 0.62, 400)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 5)
    row = A.corpus_rows(state._rag_stats, 0.60)[0]
    assert row["state"] == "thin" and row["votes"] is False


def test_switching_back_restores_the_histogram_with_the_sample():
    _search("K", 0.62, 40)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 40)
    state._config["hyde"] = {"enabled": False}
    _search("K", 0.62)
    s = state._rag_stats["K"]
    assert sum(s["raw_hist"]) == 41
    assert sum(s["alt"]["e5:h"]["h"]) == 40


def test_the_mean_divides_by_what_it_summed():
    """raw_score_sum is a sum over SCORED searches; dividing by every query understated
    the mean by the share that retrieved nothing."""
    _search("K", 0.60, 10)
    for _ in range(10):
        state._record_rag_stats("K", [], [])          # retrieved nothing
    t = A._totals(state._rag_stats)
    assert t["queries"] == 20 and t["scored"] == 10
    assert A._mean_raw(t) == pytest.approx(0.60, abs=0.001)


# ── the upgrade from 1.9.0 ───────────────────────────────────────────────────
def test_a_row_from_before_scored_existed_reports_its_real_size():
    """Reporting scored=0 made a corpus with 500 searches of history look unmeasured, and
    routed it down the histogram-only path where a legacy blob full of pre-fix zeros
    derived a threshold of 0.00 and offered it as a one-click apply."""
    legacy = {"queries": 500, "hits": 100, "chunks_total": 300, "score_sum": 72.0,
              "raw_score_sum": 72.0, "errors": 0, "answers_with_citations": 0,
              "raw_hist": [400] + [0] * 13 + [100] + [0] * 5,
              "cited_hist": [0] * 20, "raw_sample": [0.0, 0.0]}
    back = rag_admin._clean_stats_row(legacy)
    assert back["scored"] == 500
    assert back["regime"] == state.UNKNOWN_REGIME


def test_the_upgrade_never_offers_a_zero_threshold():
    """CHANGELOG 1.10.0 says the zero-threshold defect is fixed; the upgrade path
    re-created it for the first 20 grounded turns per corpus."""
    legacy = {"queries": 500, "hits": 100, "chunks_total": 300, "score_sum": 72.0,
              "raw_score_sum": 72.0, "errors": 0, "answers_with_citations": 0,
              "raw_hist": [400] + [0] * 13 + [100] + [0] * 5,
              "cited_hist": [0] * 20, "raw_sample": [0.0, 0.0]}
    state._rag_stats = {"legacy": rag_admin._clean_stats_row(legacy)}
    for turns in (0, 5, 10, 19, 25):
        a = A.build_advice(state._config, state._rag_stats)["settings"]["similarity_threshold"]
        assert a["suggested"] != 0.0, (turns, a["suggested"])
        _search("legacy", 0.72, 5)


def test_a_parked_regime_keeps_its_histogram_across_a_restart():
    """_switch_regime parks {n,sum,s,h}; the validator rebuilt the slot without h, so
    after a restart the chart the release added "so the reader can check the verdict"
    showed a handful of searches while the verdict came from hundreds."""
    _search("K", 0.62, 300)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.80, 250)
    import json
    back = rag_admin._clean_stats_row(json.loads(json.dumps(state._rag_stats["K"])))
    assert "h" in back["alt"]["e5:q"]
    assert sum(back["alt"]["e5:q"]["h"]) == 300


def test_the_analytics_mean_survives_a_regime_change():
    """raw_score_sum is parked with its regime while queries is lifetime, so dividing one
    by the other collapsed the reported mean to a fraction of the truth — and DOCS points
    users at that figure as the diagnostic for a too-strict threshold."""
    from visualweaver import routes_redis
    _search("K", 0.80, 400)
    assert routes_redis.api_rag_stats()[0]["avg_best_raw_score"] == pytest.approx(0.80)
    state._config["hyde"] = {"enabled": True}
    _search("K", 0.30, 15)
    row = routes_redis.api_rag_stats()[0]
    assert row["queries"] == 415
    assert row["avg_best_raw_score"] == pytest.approx(0.30, abs=0.01)
