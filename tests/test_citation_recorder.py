# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an answer's [n] markers are allowed to mean.

These counters are the only evidence the RAG advice has for "is top_k too high", so a
marker miscounted here becomes a wrong recommendation shown to the user in green.
"""
import pathlib

import pytest

from visualweaver import state
from visualweaver.routes_chat import _record_answer_citations


@pytest.fixture(autouse=True)
def _isolated_stats(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(state, "_rag_stats", store)
    monkeypatch.setattr(state, "_rag_stats_dirty", set())
    return store


def _chunks(*specs):
    """(rank, instance) pairs as retrieval would hand them over."""
    return [{"n": n, "instance": inst} for n, inst in specs]


def _cited(store, inst):
    return [(i + 1, c) for i, c in enumerate(store[inst]["cited_hist"]) if c]


# ── subscripts are not citations ─────────────────────────────────────────────
def test_a_fenced_code_block_does_not_vote():
    """An answer about code is full of [0] and [1] that index arrays. Counted as
    citations they inflate exactly the corpora most likely to be developer docs."""
    _record_answer_citations(
        "See [1].\n```python\nprint(argv[1], rows[7])\n```\nAlso [2].",
        _chunks((1, "docs"), (2, "docs"), (7, "docs")))
    assert dict(_cited(state._rag_stats, "docs")) == {1: 1, 2: 1}


def test_a_tilde_fence_does_not_vote():
    _record_answer_citations("Ref [1].\n~~~\nrows[7]\n~~~",
                             _chunks((1, "docs"), (7, "docs")))
    assert dict(_cited(state._rag_stats, "docs")) == {1: 1}


def test_inline_code_does_not_vote():
    _record_answer_citations("Use `cols[9]` as shown in [3].",
                             _chunks((3, "docs"), (9, "docs")))
    assert dict(_cited(state._rag_stats, "docs")) == {3: 1}


def test_an_unclosed_fence_still_swallows_its_contents():
    """A truncated stream leaves the fence open; the tail must not start voting."""
    _record_answer_citations("Ref [1].\n```\nrows[7]\nmore[8]",
                             _chunks((1, "d"), (7, "d"), (8, "d")))
    assert dict(_cited(state._rag_stats, "d")) == {1: 1}


def test_prose_citations_survive_alongside_code():
    _record_answer_citations("As [1] and [2, 3] show:\n```\nx[4]\n```\nand [5].",
                             _chunks(*[(n, "d") for n in range(1, 6)]))
    assert dict(_cited(state._rag_stats, "d")) == {1: 1, 2: 1, 3: 1, 5: 1}


# ── one answer is one answer ─────────────────────────────────────────────────
def test_an_answer_citing_two_corpora_counts_as_one_answer():
    """Summing a per-instance counter across corpora made one answer look like two, and
    unlocked the advice on half the evidence it says it needs."""
    _record_answer_citations("[1] and [2]", _chunks((1, "a"), (2, "b")))
    total = sum(r["answers_with_citations"] for r in state._rag_stats.values())
    assert total == 1, state._rag_stats


def test_both_corpora_still_get_their_ranks():
    _record_answer_citations("[1] and [2]", _chunks((1, "a"), (2, "b")))
    assert dict(_cited(state._rag_stats, "a")) == {1: 1}
    assert dict(_cited(state._rag_stats, "b")) == {2: 1}


def test_the_answer_lands_on_the_corpus_that_supplied_most_of_it():
    _record_answer_citations("[1][2][3]", _chunks((1, "a"), (2, "a"), (3, "b")))
    assert state._rag_stats["a"]["answers_with_citations"] == 1
    assert state._rag_stats["b"]["answers_with_citations"] == 0


def test_a_single_corpus_answer_is_unchanged():
    _record_answer_citations("[1][2]", _chunks((1, "a"), (2, "a")))
    assert state._rag_stats["a"]["answers_with_citations"] == 1


# ── nothing to record ────────────────────────────────────────────────────────
@pytest.mark.parametrize("answer", ["", "No citations here.", "```\n[1]\n```"])
def test_an_answer_without_citations_records_nothing(answer):
    _record_answer_citations(answer, _chunks((1, "a")))
    assert not state._rag_stats


def test_a_marker_with_no_matching_chunk_is_ignored():
    """A model that invents [9] must not create a phantom rank-9 citation."""
    _record_answer_citations("[9]", _chunks((1, "a")))
    assert not state._rag_stats


# ── the deleted-corpus leak ──────────────────────────────────────────────────
def test_deleting_a_corpus_forgets_its_statistics():
    """Otherwise its scores keep steering the advice for a corpus that is gone — and
    come back on every restart, because the row is persisted."""
    _record_answer_citations("[1]", _chunks((1, "gone")))
    assert "gone" in state._rag_stats
    state.drop_rag_stats("gone")
    assert "gone" not in state._rag_stats
    assert "gone" in state._rag_stats_dirty, "the removal must be persisted too"


def test_dropping_an_unknown_corpus_is_harmless():
    state.drop_rag_stats("never-existed")
    assert not state._rag_stats_dirty


def test_the_delete_route_actually_drops_the_statistics(monkeypatch):
    """Testing drop_rag_stats alone proved the helper worked while the route never
    called it — the statistics survived the corpus and came back on every restart."""
    from visualweaver import rag_admin, routes_instances

    class _RC:
        def delete(self, *_a): pass
    saved = []
    monkeypatch.setattr(rag_admin, "_rc_for", lambda *a, **k: _RC())
    monkeypatch.setattr(rag_admin, "reset_rag", lambda *a, **k: None)
    monkeypatch.setattr(rag_admin, "invalidate_rag_meta", lambda *a, **k: None)
    monkeypatch.setattr(rag_admin, "save_rag_stats", lambda: saved.append(1))

    _record_answer_citations("[1]", _chunks((1, "doomed")))
    assert "doomed" in state._rag_stats

    routes_instances.api_delete_instance("doomed")
    assert "doomed" not in state._rag_stats
    assert saved, "the removal has to reach Redis, or it returns on restart"


# ── every row has the same shape, and its own lists ──────────────────────────
def test_a_fresh_row_carries_every_counter():
    """Fields used to appear only once the recorder that owns them had run, so a row's
    shape depended on which happened first — and direct indexing raised KeyError."""
    row = state.new_stats_row()
    for field in ("queries", "hits", "errors", "raw_score_sum",
                  "answers_with_citations", "raw_hist", "cited_hist", "raw_sample"):
        assert field in row, field


def test_two_corpora_do_not_share_their_lists():
    """dict(TEMPLATE) is a shallow copy. A list left in the template would be the same
    object in every row, and one corpus's scores would accumulate into all of them."""
    a, b = state.new_stats_row(), state.new_stats_row()
    for field in ("raw_hist", "cited_hist", "raw_sample"):
        assert a[field] is not b[field], field
    a["raw_sample"].append(0.9)
    a["cited_hist"][0] += 1
    assert b["raw_sample"] == [] and b["cited_hist"][0] == 0


def test_recording_into_one_corpus_leaves_the_other_untouched():
    _record_answer_citations("[1]", _chunks((1, "a")))
    _record_answer_citations("[1]", _chunks((1, "b")))
    assert state._rag_stats["a"]["cited_hist"] is not state._rag_stats["b"]["cited_hist"]
    assert state._rag_stats["a"]["cited_hist"][0] == 1
    assert state._rag_stats["b"]["cited_hist"][0] == 1


# ── the counters are read while executor threads write them ──────────────────
def test_every_reader_of_the_live_counters_materialises_first():
    """`for k, v in _rag_stats.items()` raises "dictionary changed size during
    iteration" the moment a new instance's first query lands mid-read; wrapping the
    view in list() materialises it in one step and cannot.

    Measured, not assumed: over 400 rounds against a writer thread with the switch
    interval at 1us, the lazy loop raised 21 times and the materialised form zero.
    A threaded regression test was tried first and rejected — whether the interleaving
    lands varies with unrelated work in the same function, so it passed and failed on
    the same code. This reads the source instead, which is deterministic and covers
    every site rather than only the one the test happened to call.
    """
    import ast

    offenders = []
    for path in sorted(pathlib.Path("visualweaver").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.comprehension)):
                continue
            it = node.iter
            # unwrap  x.items() / x.values() / x.keys()
            if isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute) \
               and it.func.attr in ("items", "values", "keys"):
                it = it.func.value
            src = ast.unparse(it)
            if "_rag_stats" in src and not src.startswith(("list(", "dict(", "sorted(")):
                offenders.append(f"{path}:{node.lineno}  for ... in {ast.unparse(node.iter)}")
    assert not offenders, "iterating the live counter dict:\n  " + "\n  ".join(offenders)


def test_the_stats_endpoint_reports_every_corpus():
    """Guarding the iteration must not change what it returns."""
    from visualweaver import routes_redis
    live = {"a": state.new_stats_row(), "b": state.new_stats_row()}
    live["a"].update(queries=10, hits=6, score_sum=4.2, chunks_total=30,
                     raw_score_sum=7.0)
    state._rag_stats = live
    try:
        rows = routes_redis.api_rag_stats()
    finally:
        state._rag_stats = {}
    by = {r["name"]: r for r in rows}
    assert set(by) == {"a", "b"}
    assert by["a"]["hit_rate"] == 0.6 and by["a"]["misses"] == 4
    assert by["b"]["hit_rate"] == 0.0 and by["b"]["avg_chunks"] == 0.0


# ── a search that retrieved nothing is not evidence about the threshold ──────
def test_an_empty_corpus_does_not_score_zero_into_the_distribution():
    """The defect this fixes was live: one instance with 0 chunks returned no candidates,
    every such search recorded "best score 0.00", and the derived threshold collapsed to
    0.00 — which accepts every chunk."""
    state._record_rag_stats("empty", [], [])
    s = state._rag_stats["empty"]
    assert s["queries"] == 1
    assert s["no_candidates"] == 1
    assert s["scored"] == 0
    assert s["raw_sample"] == []
    assert sum(s["raw_hist"]) == 0
    assert s["raw_score_sum"] == 0.0


def test_a_search_that_found_candidates_is_scored():
    state._record_rag_stats("real", [], [{"score": 0.62}, {"score": 0.41}])
    s = state._rag_stats["real"]
    assert s["scored"] == 1 and s["no_candidates"] == 0
    assert s["raw_sample"] == [0.62]
    assert s["raw_score_sum"] == pytest.approx(0.62)


def test_the_reservoir_samples_against_scored_searches_not_all_queries():
    """Using `queries` made the replacement probability SIZE/n too small once unscored
    searches inflated n, so everything after the first 500 was under-sampled."""
    s = state.new_stats_row()
    s["queries"], s["scored"] = 10_000, 3
    s["raw_sample"] = [0.5] * state.RAW_SAMPLE_SIZE
    import random
    random.seed(1)
    before = list(s["raw_sample"])
    s["scored"] += 1
    state._reservoir_add(s, 0.99)
    # with n = scored = 4 and a full reservoir the new score must displace one
    assert s["raw_sample"] != before, "the new observation was never given a chance"


def test_a_genuine_zero_score_is_still_recorded():
    """A candidate that really scored 0.0 is evidence; only the absence of candidates
    is not."""
    state._record_rag_stats("weak", [], [{"score": 0.0}])
    assert state._rag_stats["weak"]["raw_sample"] == [0.0]
    assert state._rag_stats["weak"]["scored"] == 1


def test_a_replayed_question_is_counted_for_coverage():
    """A cache hit runs no retrieval, so it contributes no score — but the panel still
    has to say how much of the traffic its numbers speak for."""
    state.record_cache_replay("K")
    state.record_cache_replay("K")
    s = state._rag_stats["K"]
    assert s["cache_replays"] == 2
    assert s["queries"] == 0 and s["scored"] == 0
    assert s["raw_sample"] == [] and sum(s["raw_hist"]) == 0
    assert "K" in state._rag_stats_dirty


def test_a_replay_against_no_corpus_is_not_counted():
    state.record_cache_replay("")
    assert not state._rag_stats
