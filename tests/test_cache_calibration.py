# SPDX-License-Identifier: AGPL-3.0-or-later
"""The semantic cache's similarity threshold is a property of the embedding model.

The models do not share a scale. On the set in tests/fixtures/cache_calibration.py, two
unrelated questions score 0.10 apart under all-MiniLM-L6-v2 and 0.80 apart under
multilingual-e5-base — so one number cannot mean the same thing in both, and a threshold
carried across a model change is not the setting the user thought they had. A deployment
running e5-base on a value chosen for MiniLM served a cached answer to a different
question on most of the negative pairs.

The fast tests here check the wiring. The derivation itself downloads models, so it runs
only under --runslow; it is what keeps the shipped numbers honest rather than remembered.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
import cache_calibration as FIX                                    # noqa: E402

from visualweaver import cache, embeddings, state                    # noqa: E402

REGISTERED = [s["repo"] for s in embeddings.EMBEDDING_MODELS.values()]


# ── every model in the registry carries one ──────────────────────────────────
@pytest.mark.parametrize("repo", REGISTERED)
def test_every_registered_model_is_calibrated(repo):
    """A model added without one silently inherits another model's scale."""
    spec = embeddings.embedding_spec(repo)
    assert isinstance(spec.get("cache_threshold"), float), repo
    assert 0.5 < spec["cache_threshold"] < 1.0, repo


def test_an_unregistered_model_gets_the_strictest_measured_value():
    """Too strict costs model calls; too loose serves the wrong answer."""
    v = embeddings.cache_threshold_for("some-org/never-seen")
    assert v == embeddings.UNCALIBRATED_CACHE_THRESHOLD
    assert v >= max(s["cache_threshold"] for s in embeddings.EMBEDDING_MODELS.values())


# ── what the running cache actually uses ─────────────────────────────────────
@pytest.fixture
def cfg(monkeypatch):
    c = {"cache": {"enabled": True, "similarity_threshold": None, "ttl": 3600},
         "embedding": {"model": "all-MiniLM-L6-v2"}}
    monkeypatch.setattr(state, "_config", c)
    return c


def test_an_unset_threshold_follows_the_model(cfg):
    assert cache.effective_threshold() == 0.93
    cfg["embedding"]["model"] = "intfloat/multilingual-e5-base"
    assert cache.effective_threshold() == 0.98
    cfg["embedding"]["model"] = "BAAI/bge-m3"
    assert cache.effective_threshold() == 0.93


def test_an_explicit_threshold_is_honoured(cfg):
    """Config stays authoritative for someone who has measured their own corpus."""
    cfg["cache"]["similarity_threshold"] = 0.85
    assert cache.effective_threshold() == 0.85


def test_the_shipped_default_is_unset(cfg):
    from visualweaver import constants
    assert constants.DEFAULT_CONFIG["cache"]["similarity_threshold"] is None


def test_a_model_change_drops_a_threshold_calibrated_for_the_old_one(cfg, monkeypatch):
    """The reported case: 0.81 is merely permissive under MiniLM, where unrelated
    questions sit near 0.10, and serves the wrong answer under e5-base, where they sit
    near 0.80. The user never saw the new scale, so it is not a preference about it."""
    from visualweaver import config
    monkeypatch.setattr(config, "save_config", lambda *_a, **_k: None)
    cfg["cache"]["similarity_threshold"] = 0.81
    cfg["embedding"]["model"] = "intfloat/multilingual-e5-base"
    cache._reset_threshold_for_new_model()
    # cleared, not re-pinned: writing the figure the model wants TODAY looks identical
    # in the panel and silently opts the deployment out of the NEXT calibration.
    assert cfg["cache"]["similarity_threshold"] is None
    assert cache.effective_threshold() == 0.98


def test_the_reset_leaves_it_following_later_model_changes_too(cfg, monkeypatch):
    from visualweaver import config
    monkeypatch.setattr(config, "save_config", lambda *_a, **_k: None)
    cfg["cache"]["similarity_threshold"] = 0.81
    cfg["embedding"]["model"] = "intfloat/multilingual-e5-base"
    cache._reset_threshold_for_new_model()
    cfg["embedding"]["model"] = "all-MiniLM-L6-v2"
    assert cache.effective_threshold() == 0.93


def test_an_explicit_value_is_cleared_even_when_it_matches_the_new_model(cfg, monkeypatch):
    """It was chosen against the OLD model's scale. That it coincides with the new
    model's figure is luck, and leaving it pinned opts the deployment out of the next
    change for no reason the user would recognise."""
    from visualweaver import config
    monkeypatch.setattr(config, "save_config", lambda *_a, **_k: None)
    cfg["cache"]["similarity_threshold"] = 0.93          # what MiniLM happens to want
    cache._reset_threshold_for_new_model()
    assert cfg["cache"]["similarity_threshold"] is None


def test_an_unset_threshold_is_not_materialised_by_a_model_change(cfg, monkeypatch):
    """Auto must stay auto, or it stops following the next change."""
    from visualweaver import config
    monkeypatch.setattr(config, "save_config", lambda *_a, **_k: None)
    cache._reset_threshold_for_new_model()
    assert cfg["cache"]["similarity_threshold"] is None


# ── the derivation itself ────────────────────────────────────────────────────
def _pairwise(model, pairs, prefix):
    """Embed exactly as the cache does: normalise, then prefix.

    Measuring the raw sentence put three of the four shipped thresholds a full step too
    low. cache._normalize_query casefolds, so "TLS" becomes "tls" and the pair
    "enable TLS" / "disable TLS" rises from 0.9635 to 0.9713 — past a 0.97 floor derived
    without it. A live lookup then served the enable answer to a disable question.
    """
    import numpy as np
    from visualweaver.cache import _normalize_query
    fmt = lambda t: prefix + _normalize_query(t)
    a = model.encode([fmt(p[0]) for p in pairs], normalize_embeddings=True)
    b = model.encode([fmt(p[1]) for p in pairs], normalize_embeddings=True)
    return np.sum(a * b, axis=1)


@pytest.mark.slow
@pytest.mark.parametrize("repo", REGISTERED)
def test_the_shipped_threshold_is_the_lowest_with_no_false_hits(repo, request):
    """Re-derives the number rather than trusting it.

    The criterion is precision-first: the two errors do not cost the same. A false hit
    serves a cached answer to a DIFFERENT question; a miss costs one model call. So the
    shipped value is the lowest that produced zero false hits — and it must still be,
    or the comment beside it in embeddings.py has become fiction.
    """
    if not request.config.getoption("--runslow", default=False):
        pytest.skip("downloads embedding models; run with --runslow")
    import numpy as np
    from sentence_transformers import SentenceTransformer

    spec = embeddings.embedding_spec(repo)
    m = SentenceTransformer(repo if "/" in repo else f"sentence-transformers/{repo}")
    pre = spec["query_prefix"]
    hits = _pairwise(m, FIX.PARAPHRASE, pre)
    negs = np.concatenate([_pairwise(m, FIX.HARD_NEG, pre),
                           _pairwise(m, FIX.EASY_NEG, pre)])
    shipped = spec["cache_threshold"]

    assert int((negs >= shipped).sum()) == 0, (
        f"{repo}: the shipped {shipped} admits "
        f"{int((negs >= shipped).sum())} pairs that are NOT the same question")
    lower = round(shipped - 0.01, 2)
    assert int((negs >= lower).sum()) > 0, (
        f"{repo}: {lower} is also clean, so {shipped} gives up recall for nothing")
    assert int((hits >= shipped).sum()) > 0, (
        f"{repo}: {shipped} catches no paraphrase at all — the cache would never hit")


@pytest.mark.slow
@pytest.mark.parametrize("repo", REGISTERED)
def test_near_identical_wording_is_what_sets_the_floor(repo, request):
    """Recorded because it is the residual risk, not a passing detail.

    In every model measured, the pair that forces the threshold up is two questions that
    share almost all their words and differ in the one that changes the answer — "enable"
    against "disable", "Q1" against "Q2", "France" against "Germany". Those score above
    most genuine paraphrases, which by definition share FEWER words. No threshold
    separates them, so the cache buys safety by giving up recall, and that is the trade
    the shipped numbers make.
    """
    if not request.config.getoption("--runslow", default=False):
        pytest.skip("downloads embedding models; run with --runslow")
    import numpy as np
    from sentence_transformers import SentenceTransformer

    spec = embeddings.embedding_spec(repo)
    m = SentenceTransformer(repo if "/" in repo else f"sentence-transformers/{repo}")
    hard = _pairwise(m, FIX.HARD_NEG, spec["query_prefix"])
    easy = _pairwise(m, FIX.EASY_NEG, spec["query_prefix"])
    assert hard.max() > easy.max() + 0.1, (repo, hard.max(), easy.max())

    a, b = FIX.HARD_NEG[int(np.argmax(hard))]
    wa, wb = set(a.lower().split()), set(b.lower().split())
    overlap = len(wa & wb) / max(len(wa), len(wb))
    assert overlap >= 0.6, (repo, a, b, overlap)


def test_the_lookup_path_resets_the_threshold_when_the_model_changed(monkeypatch, cfg):
    """Testing the helper alone proved the rule was right while the code path never
    called it — which is how the first version of the model guard shipped dead."""
    from visualweaver import cache as C, config, redis_store, state as ST

    class _R:
        def __init__(self): self.kv = {}
        def get(self, k): return self.kv.get(k)
        def set(self, k, v): self.kv[k] = v; return True
        def delete(self, *ks): [self.kv.pop(k, None) for k in ks]; return 1
        def scan_iter(self, **_k): return iter(())
        def execute_command(self, *_a): return "OK"

    monkeypatch.setattr(redis_store, "r", lambda *a, **k: _R())
    monkeypatch.setattr(config, "save_config", lambda *_a, **_k: None)
    monkeypatch.setattr(C, "_cache_index_dim", lambda: 384)     # an index exists
    monkeypatch.setattr(ST, "_semantic_cache", None)
    monkeypatch.setattr(ST, "_semantic_cache_model", None)      # no marker: an upgrade
    monkeypatch.setattr(C, "SemanticCache", None, raising=False)  # the rebuild will fail
    cfg["cache"]["similarity_threshold"] = 0.81                 # set under another model
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-base"}

    C._get_semantic_cache()
    assert cfg["cache"]["similarity_threshold"] is None, (
        "a threshold calibrated for the previous model survived the change")
    assert C.effective_threshold() == 0.98


def test_the_settings_route_resets_it_on_an_explicit_model_change():
    """The other call site: the value the Settings page shows back must already be the
    corrected one, not the previous model's."""
    import inspect
    from visualweaver import routes_settings
    src = inspect.getsource(routes_settings.api_save_config)
    assert "_reset_threshold_for_new_model()" in src
    assert 'if "similarity_threshold" not in (payload.get("cache") or {})' in src


@pytest.mark.slow
def test_the_calibration_measures_what_the_runtime_embeds(request):
    """Measuring the raw sentence put three of the four shipped thresholds a step too low.

    cache._normalize_query casefolds before the text is embedded, and the acronym goes
    with it: "how do I enable TLS on redis" against "how do I disable TLS on redis" rises
    from 0.9635 to 0.9713 once normalised — past a 0.97 floor derived without it. A live
    lookup then returned the ENABLE answer to a DISABLE question. This test exists so the
    measurement and the runtime can never drift apart again.
    """
    if not request.config.getoption("--runslow", default=False):
        pytest.skip("downloads embedding models; run with --runslow")
    from sentence_transformers import SentenceTransformer
    from visualweaver.cache import _normalize_query

    m = SentenceTransformer("intfloat/multilingual-e5-base")
    pair = [("how do I enable TLS on redis", "how do I disable TLS on redis")]
    normalised = float(_pairwise(m, pair, "query: ")[0])
    assert normalised > 0.97, normalised
    assert embeddings.cache_threshold_for("intfloat/multilingual-e5-base") > normalised, (
        "the shipped threshold admits a negation through the real pipeline")
    assert _normalize_query("how do I enable TLS on redis") == "how do i enable tls on redis"
