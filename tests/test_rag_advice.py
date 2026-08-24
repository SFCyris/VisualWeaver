# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measured advice for the RAG controls.

The point of this module is that it never asserts a universal "recommended" value. A
similarity threshold of 0.9 is correct on a corpus scoring 0.85-0.95 and switches vector
search off entirely on one scoring 0.60-0.75, so a verdict is only meaningful against
the distribution this deployment actually observed.

These tests therefore check two things in equal measure: that a real problem is named
when the data shows one, and that NOTHING is claimed when the data is absent. The second
is the one that matters — a confidently coloured setting with no measurement behind it
is the same failure as a hardcoded price list.
"""
import pytest

from visualweaver import rag_advice as A
from visualweaver import state


def _hist(pairs) -> list[int]:
    """Build a raw-score histogram from (score, count) pairs."""
    h = [0] * state.RAW_HIST_BUCKETS
    for score, n in pairs:
        h[state.raw_bucket(score)] += n
    return h


def _ranks(pairs) -> list[int]:
    """Build a cited-rank histogram from (1-based rank, count) pairs."""
    h = [0] * state.CITED_HIST_RANKS
    for rank, n in pairs:
        h[rank - 1] += n
    return h


def _stats(*, queries=200, hist=None, cited=None, cited_answers=40) -> dict:
    return {"Ubuntu": {
        "queries": queries, "hits": 60, "chunks_total": 300, "score_sum": 40.0,
        "raw_score_sum": 0.0,
        "raw_hist": hist if hist is not None else _hist([(0.67, queries)]),
        "cited_hist": cited if cited is not None else [30, 22, 14, 7, 3] + [0] * 15,
        "answers_with_citations": cited_answers,
    }}


def _cfg(**rag) -> dict:
    base = {"similarity_threshold": 0.75, "top_k": 5, "chunk_size": 180,
            "chunk_overlap": 32, "hybrid_search": True, "rerank_candidates": 40}
    base.update(rag)
    return {"rag": base, "reranker": {"enabled": False, "top_n": 5},
            "history_max_tokens": 3000}


# ── the bucket helper ────────────────────────────────────────────────────────
@pytest.mark.parametrize("score,bucket", [
    (0.0, 0), (0.049, 0), (0.05, 1), (0.5, 10), (0.71, 14), (0.999, 19), (1.0, 19),
])
def test_scores_land_in_the_right_bucket(score, bucket):
    """1.0 must clamp into the last bucket, not one past the end of the list."""
    assert state.raw_bucket(score) == bucket


@pytest.mark.parametrize("bad", [None, "x", float("nan")])
def test_a_junk_score_does_not_raise(bad):
    """These counters are updated on every search; a bad value must not fail a query."""
    assert 0 <= state.raw_bucket(bad) < state.RAW_HIST_BUCKETS


# ── pass rate and the suggestion ─────────────────────────────────────────────
def test_pass_rate_counts_only_queries_at_or_above_the_threshold():
    h = _hist([(0.65, 30), (0.85, 70)])
    assert A.pass_rate(h, 0.80) == pytest.approx(0.70)
    assert A.pass_rate(h, 0.60) == pytest.approx(1.00)
    assert A.pass_rate(h, 0.95) == pytest.approx(0.00)


def test_pass_rate_of_an_empty_history_is_zero_not_an_error():
    assert A.pass_rate([0] * state.RAW_HIST_BUCKETS, 0.5) == 0.0


def test_the_suggestion_is_a_threshold_the_distribution_can_actually_meet():
    """Recommending the mean would discard half the queries by construction. The
    suggestion is the highest bucket edge that still keeps TARGET_PASS of them."""
    h = _hist([(0.62, 10), (0.68, 40), (0.74, 40), (0.90, 10)])
    sug = A.suggest_threshold(h, target=0.85)
    assert sug is not None
    assert A.pass_rate(h, sug) >= 0.85
    # ...and it is not needlessly low: one bucket higher would fail the target
    assert A.pass_rate(h, sug + 0.05) < 0.85


def test_no_suggestion_without_data():
    assert A.suggest_threshold([0] * state.RAW_HIST_BUCKETS) is None


# ── silence when there is nothing to say ─────────────────────────────────────
def test_a_thin_sample_yields_unknown_not_a_guess():
    """The whole design rests on this: below MIN_QUERIES the UI must show no colour."""
    a = A.build_advice(_cfg(similarity_threshold=0.9),
                       _stats(queries=A.MIN_QUERIES - 1))["settings"]
    assert a["similarity_threshold"]["status"] == "unknown"
    assert a["similarity_threshold"]["suggested"] is None


def test_no_stats_at_all_yields_unknown():
    a = A.build_advice(_cfg(), {})["settings"]
    assert a["similarity_threshold"]["status"] == "unknown"
    assert a["top_k"]["status"] == "unknown"


def test_settings_that_cannot_be_observed_are_marked_static_not_scored():
    """Changing chunk size needs a re-ingest, so the alternative is unobservable from
    here. Colouring it would be an opinion wearing a measurement's clothes."""
    a = A.build_advice(_cfg(), _stats())["settings"]
    for key in ("chunk_size", "chunk_overlap", "history_max_tokens"):
        assert a[key]["status"] == "static", key
        assert a[key]["suggested"] is None


# ── the verdict that would have caught the real bug ──────────────────────────
def test_a_threshold_above_the_corpus_distribution_is_called_bad():
    """The live case: scores cluster 0.58-0.78 and the threshold is 0.90, so every
    surviving hit comes from the keyword leg and the vector index contributes nothing."""
    a = A.build_advice(_cfg(similarity_threshold=0.90),
                       _stats(hist=_hist([(0.62, 60), (0.68, 80), (0.74, 60)])))["settings"]
    t = a["similarity_threshold"]
    assert t["status"] == "bad"
    assert "0%" in t["headline"]
    assert t["suggested"] is not None and t["suggested"] < 0.90


def test_a_threshold_matching_the_distribution_is_called_ok():
    """The same value must NOT be called bad on a corpus that scores higher — this is
    the test that stops the verdict becoming a disguised constant."""
    a = A.build_advice(_cfg(similarity_threshold=0.90),
                       _stats(hist=_hist([(0.92, 150), (0.96, 50)])))["settings"]
    assert a["similarity_threshold"]["status"] == "ok"
    assert a["similarity_threshold"]["suggested"] is None


def test_a_suggestion_within_a_bucket_of_the_current_value_is_not_offered():
    """Nudging someone from 0.70 to 0.68 is noise dressed as advice."""
    h = _hist([(0.72, 100), (0.78, 100)])
    a = A.build_advice(_cfg(similarity_threshold=0.70), _stats(hist=h))["settings"]
    assert a["similarity_threshold"]["suggested"] is None


# ── top_k, from what answers actually cited ──────────────────────────────────
def test_top_k_far_beyond_the_cited_working_set_is_called_bad():
    cited = [30, 22, 14, 7, 3] + [0] * 15          # 76 citations; 95% land by rank 4
    a = A.build_advice(_cfg(top_k=20, chunk_size=195),
                       _stats(cited=cited))["settings"]["top_k"]
    assert a["status"] == "bad"
    assert f"rank {A.cited_coverage_rank(cited)}" in a["headline"]
    assert "rank 5" not in a["headline"], "the rare deepest rank is not the working set"
    assert a["suggested"] == A.cited_coverage_rank(cited) + A.TOPK_HEADROOM


def test_top_k_in_line_with_usage_is_called_ok():
    a = A.build_advice(_cfg(top_k=6),
                       _stats(cited=[30, 22, 14, 7, 3] + [0] * 15))["settings"]["top_k"]
    assert a["status"] == "ok"
    assert a["suggested"] is None


def test_top_k_says_what_it_costs_even_without_enough_citations():
    """The token figure needs no measurement — it is arithmetic on the current values,
    and it is the number that makes the setting worth looking at."""
    a = A.build_advice(_cfg(top_k=20, chunk_size=195),
                       _stats(cited_answers=0))["settings"]["top_k"]
    assert a["status"] == "unknown"
    assert "5,265 tokens" in a["detail"]


def test_the_suggestion_never_raises_top_k():
    """Advice may trim what is unused; proposing MORE context from usage data alone
    would be an inference the citations cannot support."""
    a = A.build_advice(_cfg(top_k=3), _stats(cited=[9, 8, 7] + [0] * 17))["settings"]["top_k"]
    assert a["suggested"] is None or a["suggested"] <= 3


# ── presets ──────────────────────────────────────────────────────────────────
def test_a_preset_reports_only_what_it_would_change():
    d = A.preset_diff("recall", _cfg(similarity_threshold=0.70, top_k=5))
    paths = {c["path"] for c in d["changes"]}
    assert "rag.similarity_threshold" not in paths   # already at the preset's value
    assert "reranker.enabled" in paths


def test_a_preset_already_applied_reports_no_changes():
    cfg = {"rag": {"similarity_threshold": 0.75, "top_k": 4, "hybrid_search": True},
           "reranker": {"enabled": False}, "history_max_tokens": 1500}
    assert A.preset_diff("economy", cfg)["changes"] == []


def test_no_preset_touches_chunking():
    """Chunk size and overlap only take effect on re-ingest. A preset that changed them
    would silently invalidate every stored embedding."""
    for p in A.RAG_PRESETS:
        assert not [k for k in p["values"] if "chunk_" in k], p["id"]


def test_every_preset_is_reachable_and_described():
    assert {p["id"] for p in A.RAG_PRESETS} == {"precision", "recall", "economy"}
    for p in A.RAG_PRESETS:
        assert p["when"] and p["name"]
        assert A.preset_diff(p["id"], _cfg()) is not None


def test_an_unknown_preset_id_returns_none_rather_than_raising():
    assert A.preset_diff("nope") is None


# ── the sample, not the buckets ──────────────────────────────────────────────
# A histogram cannot answer the question this feature exists for. The threshold slider
# steps in 0.01 and buckets are 0.05 wide, so a corpus scoring 0.81 with the threshold
# at 0.82 lives inside one bucket — and the bucketed answer was not merely imprecise,
# it was inverted: it reported "100% of queries clear this", in green, when the runtime
# truth was 0%.
@pytest.mark.parametrize("thr,expected", [
    (0.80, 1.0), (0.81, 1.0), (0.82, 0.0), (0.84, 0.0), (0.90, 0.0),
])
def test_the_pass_rate_matches_the_runtime_predicate_exactly(thr, expected):
    """rag.search_rag keeps a chunk when score >= threshold. Nothing less will do."""
    sample = [0.81] * 200
    assert A.pass_rate_exact(sample, thr) == expected
    # ...and it agrees with the predicate itself, not merely with a stored expectation
    assert A.pass_rate_exact(sample, thr) == sum(x >= thr for x in sample) / len(sample)


def test_a_bucketed_pass_rate_would_have_inverted_this_verdict():
    """Kept as the regression's epitaph: the old path really did say 100%."""
    h = [0] * state.RAW_HIST_BUCKETS
    h[state.raw_bucket(0.81)] = 200
    assert A.pass_rate_exact([0.81] * 200, 0.84) == 0.0
    assert A.pass_rate(h, 0.84) < 0.5      # interpolated, no longer 1.0


def test_the_suggested_threshold_actually_achieves_its_target():
    """Asserted against the raw scores, not against the model that produced it — the
    previous test compared suggest_threshold to pass_rate, which is circular."""
    import random
    random.seed(11)
    sample = [random.uniform(0.55, 0.85) for _ in range(500)]
    for target in (0.60, 0.85, 0.95):
        thr = A.suggest_threshold_exact(sample, target)
        actual = sum(1 for x in sample if x >= thr) / len(sample)
        assert actual >= target - 0.02, (target, thr, actual)


def test_the_mean_is_exact_not_a_bucket_midpoint():
    """Midpoints reported 0.63 for a corpus scoring exactly 0.60 — half a bucket out,
    which is the whole distance between working and not on a tight corpus."""
    t = {"queries": 1000, "raw_score_sum": 600.0,
         "raw_hist": _hist([(0.60, 1000)]), "cited_hist": [0] * 20}
    assert A._mean_raw(t) == pytest.approx(0.60)


# ── presets state intent, never a threshold ──────────────────────────────────
def test_no_preset_hardcodes_a_similarity_threshold():
    """The whole point. A threshold that is strict on one corpus switches vector search
    off on another; constants.py records this codebase already shipping that bug once."""
    for p in A.RAG_PRESETS:
        assert "rag.similarity_threshold" not in p["values"], p["id"]
        assert isinstance(p["pass_target"], float)


def test_a_preset_threshold_is_derived_from_the_observed_scores():
    import random
    random.seed(5)
    sample = [random.uniform(0.58, 0.78) for _ in range(400)]
    for pid in ("precision", "recall", "economy"):
        d = A.preset_diff(pid, _cfg(similarity_threshold=0.90), sample)
        thr = d["values"]["rag.similarity_threshold"]
        assert 0.55 <= thr <= 0.80, (pid, thr)      # inside what the corpus can produce
        assert d["note"] is None


def test_a_stricter_preset_derives_a_higher_threshold_than_a_permissive_one():
    import random
    random.seed(5)
    sample = [random.uniform(0.58, 0.78) for _ in range(400)]
    got = {p: A.preset_diff(p, _cfg(), sample)["values"]["rag.similarity_threshold"]
           for p in ("precision", "recall", "economy")}
    assert got["precision"] > got["economy"] > got["recall"], got


def test_without_data_a_preset_leaves_the_threshold_alone_and_says_so():
    d = A.preset_diff("recall", _cfg(similarity_threshold=0.90), [])
    assert "rag.similarity_threshold" not in d["values"]
    assert d["note"] and "not enough" in d["note"]
    assert not any(c["path"] == "rag.similarity_threshold" for c in d["changes"])


def test_a_preset_reports_the_full_value_map_not_just_the_diff():
    """Applying only the diff left a control the user had staged differently untouched,
    so the preset silently was not what it claimed."""
    d = A.preset_diff("recall", _cfg(top_k=8), [0.7] * 100)
    assert not any(c["path"] == "rag.top_k" for c in d["changes"])   # matches saved
    assert d["values"]["rag.top_k"] == 8                              # still applied


def test_every_change_carries_a_human_label():
    d = A.preset_diff("recall", _cfg(similarity_threshold=0.9), [0.7] * 100)
    for c in d["changes"]:
        assert c["label"] and c["label"] != c["path"], c


# ── errors are counted, never scored ─────────────────────────────────────────
def test_failing_retrieval_is_named_as_such_not_blamed_on_the_threshold():
    """Recording failures as "best score 0.0" made a broken index look like a badly
    tuned threshold, and the advice offered a one-click button setting it to zero."""
    a = A.build_advice(_cfg(similarity_threshold=0.66),
                       {"d": dict(_stats(queries=5)["Ubuntu"], errors=50)})["settings"]
    t = a["similarity_threshold"]
    assert t["status"] == "bad"
    assert "failing" in t["headline"].lower()
    assert t["suggested"] is None, "must not offer a threshold when retrieval is erroring"


def test_a_few_errors_do_not_suppress_a_healthy_verdict():
    a = A.build_advice(_cfg(similarity_threshold=0.66),
                       {"d": dict(_stats(queries=200)["Ubuntu"], errors=1)})["settings"]
    assert a["similarity_threshold"]["status"] != "bad" or "failing" not in \
        a["similarity_threshold"]["headline"].lower()


# ── top_k under reranking ────────────────────────────────────────────────────
def test_top_k_is_judged_against_what_actually_reaches_the_model():
    """With reranking on, only top_n chunks are sent, so a deeper rank could not have
    been cited whatever its quality. Judging against top_k read "nothing below rank 5
    is used" from chunks that were never shown to the model."""
    cfg = _cfg(top_k=20)
    cfg["reranker"] = {"enabled": True, "top_n": 5}
    a = A.build_advice(cfg, _stats(cited=[30, 20, 10, 5, 2] + [0] * 15))["settings"]["top_k"]
    assert a["status"] == "ok"
    assert a["suggested"] is None


def test_citations_recorded_but_none_in_range_is_unknown_not_a_green_rank_zero():
    a = A.build_advice(_cfg(top_k=5),
                       _stats(cited=[0] * 20, cited_answers=40))["settings"]["top_k"]
    assert a["status"] == "unknown"
    assert "rank 0" not in a["headline"]


# ── a rank recorded under an older top_k must not outlive it ─────────────────
def test_one_deep_outlier_does_not_hold_top_k_open():
    """The all-time max let a single curious answer that reached rank 18 justify sending
    eighteen chunks with every answer after it."""
    h = [(1, 400), (2, 300), (3, 150), (18, 3)]
    a = A.build_advice(_cfg(top_k=20), _stats(cited=_ranks(h)))["settings"]["top_k"]
    assert a["status"] == "bad"
    assert a["suggested"] == 5                     # 3 + headroom, not 18


def test_a_rank_the_current_top_k_cannot_produce_never_reaches_the_ui():
    """History under top_k=20, now set to 5: "ranks up to 17 are in use" was rendered in
    green, and the negative headroom fell through to the healthy branch.

    The citations have to sit DEEP for this to bite — a shallow working set never
    exceeds the current top_k, so the clamp is not reached and the test proves nothing.
    """
    cited = _ranks([(15, 100), (16, 100), (17, 100)])
    assert A.cited_coverage_rank(cited) == 17 > 5, "input must exercise the clamp"
    a = A.build_advice(_cfg(top_k=5), _stats(cited=cited))["settings"]["top_k"]
    assert "17" not in a["headline"] and "17" not in a["detail"]
    assert a["status"] == "ok"


def test_a_genuinely_wide_working_set_is_left_alone():
    """The outlier rule must not become a rule against wide retrieval."""
    a = A.build_advice(_cfg(top_k=20),
                       _stats(cited=_ranks([(r, 100) for r in range(1, 19)])))["settings"]["top_k"]
    assert a["suggested"] is None and a["status"] == "ok"


def test_the_coverage_rank_is_the_bulk_not_the_extreme():
    assert A.cited_coverage_rank(_ranks([(1, 400), (2, 300), (3, 150), (18, 3)])) == 3
    assert A.cited_coverage_rank(_ranks([(r, 100) for r in range(1, 19)])) == 18
    assert A.cited_coverage_rank([0] * 20) == 0


# ── two panels, one fact ─────────────────────────────────────────────────────
def test_the_threshold_and_hybrid_panels_cannot_contradict_each_other():
    """Both answer "is vector search matching anything". One read the exact sample and
    the other the 0.05 buckets, so on a tight corpus they printed different answers."""
    # 0.82 sits inside the 0.80-0.85 bucket, so the bucketed estimate reads 60% while
    # the real scores read 0% — one side of the 25% line each. At 0.84 both say "bad"
    # and the contradiction is invisible.
    cfg = _cfg(similarity_threshold=0.82, hybrid_search=False)
    st = _stats(queries=200, hist=_hist([(0.81, 200)]))
    st["Ubuntu"]["raw_sample"] = [0.81] * 200
    assert A.pass_rate(st["Ubuntu"]["raw_hist"], 0.82) >= 0.25 > \
        A.pass_rate_exact(st["Ubuntu"]["raw_sample"], 0.82), "input must discriminate"
    a = A.build_advice(cfg, st)["settings"]
    assert a["similarity_threshold"]["headline"].startswith("Only 0%")
    assert "not matching" in a["hybrid_search"]["headline"]


def test_hybrid_is_not_condemned_when_vector_search_is_working():
    cfg = _cfg(similarity_threshold=0.50, hybrid_search=False)
    st = _stats(queries=200, hist=_hist([(0.81, 200)]))
    st["Ubuntu"]["raw_sample"] = [0.81] * 200
    a = A.build_advice(cfg, st)["settings"]["hybrid_search"]
    assert "not matching" not in a["headline"]


# ── advice and action answer to the same evidence standard ───────────────────
@pytest.mark.parametrize("n", [1, 3, 19])
def test_a_preset_refuses_the_threshold_on_the_same_evidence_the_verdict_refuses(n):
    """Shipped defect, caught live: the verdict said "3 of 20 queries needed before the
    score distribution can be read" while the preset directly below it derived a
    threshold from those 3 and offered a one-click apply. The panel enforced a standard
    on itself that the button beside it ignored."""
    sample = [0.0] * n
    for pid in ("precision", "recall", "economy"):
        d = A.preset_diff(pid, _cfg(similarity_threshold=0.9), sample)
        assert "rag.similarity_threshold" not in d["values"], (pid, n)
        assert not any(c["path"] == "rag.similarity_threshold" for c in d["changes"])
        assert d["note"] and str(A.MIN_QUERIES) in d["note"]


def test_the_live_deployment_case_no_longer_proposes_a_zero_threshold():
    """Exactly what the running install returned: three searches that retrieved nothing,
    and all three presets offering to change the threshold from 0.90 to 0.00 — which
    accepts every chunk in the corpus."""
    for pid in ("precision", "recall", "economy"):
        d = A.preset_diff(pid, _cfg(similarity_threshold=0.9), [0.0, 0.0, 0.0])
        assert d["values"].get("rag.similarity_threshold") is None


def test_a_corpus_that_matches_nothing_yields_a_diagnosis_not_a_setting():
    """With plenty of data but every score zero, a derived 0.00 is arithmetically
    correct and useless: applying it turns the threshold off."""
    d = A.preset_diff("recall", _cfg(), [0.0] * (A.MIN_QUERIES * 2))
    assert "rag.similarity_threshold" not in d["values"]
    assert "not matching" in d["note"]


def test_a_preset_still_derives_once_there_is_enough(_seeded=None):
    import random
    random.seed(4)
    s = [random.uniform(0.35, 0.75) for _ in range(A.MIN_QUERIES * 4)]
    got = {p: A.preset_diff(p, _cfg(), s)["values"].get("rag.similarity_threshold")
           for p in ("precision", "recall", "economy")}
    assert all(v is not None and 0.3 < v < 0.8 for v in got.values()), got
    assert got["precision"] > got["economy"] > got["recall"], got


# ── one threshold, several corpora ───────────────────────────────────────────
def _corpus(n, mu, sd=0.06, seed=0, **extra):
    """A row shaped the way state._record_rag_stats actually builds one — histogram
    included. Leaving raw_hist empty made a test pass that should have caught the panel
    drawing one corpus's chart under another corpus's percentage."""
    import random
    random.seed(seed or int(mu * 1000) + n)
    s = [max(0.0, random.gauss(mu, sd)) for _ in range(n)]
    hist = [0] * state.RAW_HIST_BUCKETS
    for x in s:
        hist[state.raw_bucket(x)] += 1
    return dict({"queries": n, "errors": 0, "raw_sample": s, "raw_score_sum": sum(s),
                 "raw_hist": hist, "cited_hist": [0] * 20, "answers_with_citations": 0,
                 "hits": 0, "chunks_total": 0, "score_sum": 0.0, "scored": n,
                 "no_candidates": 0, "cache_replays": 0}, **extra)


def test_the_suggestion_is_the_highest_value_every_corpus_can_meet():
    """min, not max and not the midpoint. The asymmetry is measured, not preferred: too
    low costs precision, which a reranker can absorb; too high switches vector retrieval
    OFF for that corpus, which nothing recovers."""
    rows = A.corpus_rows({"lowish": _corpus(300, 0.30), "highish": _corpus(300, 0.66)}, 0.5)
    c = A.consensus(rows)
    wants = sorted(r["wants"] for r in rows if r["votes"])
    assert c["suggested"] == wants[0], (c["suggested"], wants)
    assert c["suggested"] < wants[-1]


def test_a_dark_corpus_is_not_averaged_away_by_a_healthy_one():
    """Pooling reported the mean pass rate, so one corpus retrieving nothing beside one
    retrieving everything read as "50% clear this" — in amber, when half the deployment
    was dark."""
    rows = A.corpus_rows({"dark": _corpus(300, 0.20), "bright": _corpus(300, 0.90)}, 0.75)
    worst = A.pass_now_min(rows, 0.75)
    mean = sum(r["pass_now"] for r in rows) / len(rows)
    assert worst == min(r["pass_now"] for r in rows)
    assert worst < mean - 0.2, (worst, mean)


def test_the_hybrid_panel_fires_when_any_corpus_is_dark_not_when_the_average_is():
    cfg = _cfg(similarity_threshold=0.75, hybrid_search=False)
    a = A.build_advice(cfg, {"dark": _corpus(300, 0.20),
                             "bright": _corpus(300, 0.90)})["settings"]["hybrid_search"]
    assert "not matching" in a["headline"], a["headline"]


# ── coverage ─────────────────────────────────────────────────────────────────
def test_the_coverage_note_names_what_was_not_measured():
    """At a 90% hit rate it takes 200 questions to reach the 20 the verdict needs."""
    adv = A.build_advice(_cfg(), {"K": _corpus(120, 0.55, cache_replays=1080)})
    c = adv["coverage"]
    assert c["measured"] == 120 and c["replayed"] == 1080
    assert c["share"] == 0.1
    assert "120 of 1,200" in c["note"] and "answered from cache" in c["note"]


def test_coverage_says_nothing_when_nothing_was_replayed():
    """A note on every deployment would be noise on most of them."""
    assert A.build_advice(_cfg(), {"K": _corpus(120, 0.55)})["coverage"]["note"] is None


# ── the number and the name have to come from the same corpus ────────────────
def _dark(n=200):
    return {"queries": n, "errors": 0, "raw_sample": [0.0] * n, "raw_score_sum": 0.0,
            "raw_hist": [n] + [0] * 19, "cited_hist": [0] * 20,
            "answers_with_citations": 0, "hits": 0, "chunks_total": 0, "score_sum": 0.0,
            "scored": n, "no_candidates": 0, "cache_replays": 0}


def test_a_corpus_that_cannot_speak_does_not_supply_the_percentage():
    """It took the number from a no_signal corpus and the name from a voting one: the
    panel printed "Only 0% of queries clear this on wiki" with wiki at 96%, and then the
    arithmetically impossible "At 0.73, 88% would" beside it."""
    st = {"wiki": _corpus(200, 0.80), "handbook": _corpus(200, 0.82), "dead": _dark()}
    a = A.build_advice(_cfg(similarity_threshold=0.70), st)["settings"]["similarity_threshold"]
    assert "Only 0%" not in a["headline"], a["headline"]
    assert a["status"] != "bad"


def test_the_reported_rate_belongs_to_a_corpus_that_votes():
    st = {"wiki": _corpus(200, 0.80), "dead": _dark()}
    rows = A.corpus_rows(st, 0.70)
    rate = A.pass_now_min(rows, 0.70)
    voters = [r for r in rows if r["votes"]]
    assert rate in [r["pass_now"] for r in voters], (rate, voters)


def test_the_pass_rate_can_never_rise_as_the_threshold_rises():
    """The contradiction the contaminated number produced. Monotonicity is a property of
    the predicate, so any pair of numbers the panel shows must respect it."""
    st = {"wiki": _corpus(300, 0.70), "dead": _dark()}
    rows = A.corpus_rows(st, 0.60)
    prev = 1.1
    for t in [i / 20 for i in range(21)]:
        cur = A.pass_now_min(A.corpus_rows(st, t), t)
        assert cur is None or cur <= prev + 1e-9, (t, cur, prev)
        if cur is not None:
            prev = cur


def test_the_hybrid_panel_is_not_condemned_by_a_dark_corpus_either():
    cfg = _cfg(similarity_threshold=0.70, hybrid_search=False)
    a = A.build_advice(cfg, {"wiki": _corpus(300, 0.85), "dead": _dark()})["settings"]
    assert "not matching" not in a["hybrid_search"]["headline"], a["hybrid_search"]["headline"]


# ── a threshold of zero is a diagnosis, not a setting ────────────────────────
def test_a_legacy_histogram_of_zeros_does_not_vote_for_a_zero_threshold():
    """_corpus_state returned "binned" before it ever tested for no signal, so a
    histogram-only corpus with every observation in the bottom bucket voted, wanted 0.00,
    and the panel offered a one-click "use 0" — which turns the threshold off."""
    legacy = dict(_dark(200), raw_sample=[])
    rows = A.corpus_rows({"old": legacy}, 0.72)
    assert rows[0]["state"] == "no_signal" and rows[0]["votes"] is False


def test_a_dead_legacy_corpus_beside_a_healthy_one_never_suggests_zero():
    legacy = dict(_dark(200), raw_sample=[])
    a = A.build_advice(_cfg(similarity_threshold=0.72),
                       {"old": legacy, "handbook": _corpus(300, 0.80)})["settings"]
    assert a["similarity_threshold"]["suggested"] != 0.0
    assert (a["similarity_threshold"]["suggested"] or 1) > 0


def test_a_mostly_floored_distribution_is_reported_not_tuned():
    """80% of observations on the measurement floor derives a threshold of 0.00, which is
    arithmetically correct and useless."""
    row = {"queries": 500, "errors": 0, "raw_sample": [], "raw_score_sum": 72.0,
           "raw_hist": [400] + [0] * 13 + [100] + [0] * 5, "cited_hist": [0] * 20,
           "answers_with_citations": 0, "hits": 100, "chunks_total": 300,
           "score_sum": 72.0, "scored": 500, "no_candidates": 0, "cache_replays": 0}
    a = A.build_advice(_cfg(similarity_threshold=0.60), {"legacy": row})["settings"]
    t = a["similarity_threshold"]
    assert t["suggested"] is None
    assert "not matching anything" in t["headline"]
    assert "Not one search" not in t["detail"], "the detail must be true of this row too"


# ── the panel must not state a number about a population it is not showing ───
def test_the_chart_shows_the_corpus_the_number_is_about():
    """The histogram pooled every corpus while the percentage above it was one corpus's,
    so the tallest bar on screen — 40 zeros out of 100 — belonged to the population the
    percentage excluded, under a green "100% clear this"."""
    st = {"alpha": _corpus(60, 0.70), "bravo": _dark(40)}
    a = A.build_advice(_cfg(similarity_threshold=0.35), st)["settings"]["similarity_threshold"]
    assert sum(a["hist"]) == 60, "the chart still pools the corpus the number excludes"
    assert a["curve_of"] == "alpha"


def test_a_single_voter_beside_a_dead_corpus_still_names_itself():
    """A no_signal corpus correctly stops voting — and in doing so erased itself from the
    panel entirely: no corpus suffix, no "never matched" clause, nothing."""
    st = {"alpha": _corpus(60, 0.70), "bravo": _dark(40)}
    a = A.build_advice(_cfg(similarity_threshold=0.35), st)["settings"]["similarity_threshold"]
    assert "alpha" in a["headline"], a["headline"]
    assert "never matched" in a["detail"], a["detail"]
    assert "1 of 2 corpora" in a["detail"], a["detail"]


def test_coverage_counts_questions_not_per_corpus_searches():
    """One question searches every selected corpus, so summing per-corpus queries counted
    it once per corpus while a replay is recorded once per question — the ratio was wrong
    by the corpus count."""
    st = {f"c{i}": _corpus(100, 0.6, seed=i + 1) for i in range(3)}
    st["c0"]["cache_replays"] = 100
    c = A.build_advice(_cfg(), st)["coverage"]
    assert c["measured"] == 100 and c["asked"] == 200 and c["share"] == 0.5


def test_the_token_cost_counts_only_what_is_sent():
    """The reranker discards all but top_n, so quoting top_k overstated the context by
    four times on the same line that said only five ranks were in use."""
    cfg = _cfg(top_k=20, chunk_size=200)
    cfg["reranker"] = {"enabled": True, "top_n": 5}
    a = A.build_advice(cfg, _stats(cited=_ranks([(1, 40), (2, 30), (3, 20)])))["settings"]["top_k"]
    assert "1,350 tokens" in a["detail"], a["detail"]      # 5 x 200 x 1.35
    assert "5,400" not in a["detail"], "still costing the chunks the reranker throws away"


def test_nothing_is_graded_before_anything_is_measured():
    """Amber with a one-click apply, on a deployment that has measured nothing, from a
    constant this module chose — the static recommended value the feature exists to
    abolish, and it survived the reset that promises otherwise."""
    cfg = _cfg(top_k=20, hybrid_search=False)
    a = A.build_advice(cfg, {})["settings"]
    for key in ("similarity_threshold", "top_k", "reranker", "hybrid_search"):
        assert a[key]["status"] in ("unknown", "static"), (key, a[key]["status"])
        assert a[key]["suggested"] is None, key


# ── a preset may not contradict the verdict on the same screen ───────────────
def _cited(pairs, answers=60):
    h = [0] * 20
    for r, c in pairs:
        h[r - 1] = c
    return h, answers


def test_a_preset_cannot_send_more_chunks_than_the_citations_show_are_read():
    """The preset values are declared starting points, not measurements, and they
    contradicted the panel directly above them: "ranks up to 3 are in use, suggested:
    none" beside a button raising the kept-chunk count to 8."""
    h, n = _cited([(1, 40), (2, 25), (3, 10)])
    st = {"K": dict(_corpus(200, 0.60), cited_hist=h, answers_with_citations=n)}
    state._rag_stats, state._config = st, _cfg(top_k=20)
    for pid in ("precision", "recall", "economy"):
        v = A.preset_diff(pid)["values"]
        assert v.get("rag.top_k", 0) <= 5, (pid, v.get("rag.top_k"))
        assert v.get("reranker.top_n", 0) <= 5, (pid, v.get("reranker.top_n"))


def test_the_trim_says_what_measured_it():
    h, n = _cited([(1, 40), (2, 25), (3, 10)])
    state._rag_stats = {"K": dict(_corpus(200, 0.60), cited_hist=h, answers_with_citations=n)}
    state._config = _cfg(top_k=20)
    note = A.preset_diff("recall")["note"]
    assert note and "Trimmed to 5" in note and "60 cited answers" in note


def test_without_citation_evidence_the_presets_own_figures_stand():
    """Capping on nothing would be inventing a measurement."""
    state._rag_stats = {"K": _corpus(200, 0.60)}
    state._config = _cfg(top_k=20)
    assert A.preset_diff("recall")["values"]["rag.top_k"] == 8


def test_the_threshold_detail_names_the_target_that_picked_it():
    """The quantile is the user's data; the share it is read at is this module's choice,
    and the popup said only "measured from your own retrieval history"."""
    a = A.build_advice(_cfg(similarity_threshold=0.90),
                       {"K": _corpus(200, 0.60)})["settings"]["similarity_threshold"]
    assert f"{int(A.TARGET_PASS * 100)}%" in a["detail"], a["detail"]
