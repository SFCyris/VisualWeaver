# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which cached answer a question is allowed to reach.

A request to DRAW something used to be excluded from the cache outright — one flag gated
the lookup and the store together, so asking the identical question twice cost two model
calls and, because nothing was ever written, a later plain-text rewording could not match
either. Those turns are cached now.

They are not matched by similarity, though. A drawing request is mostly scaffolding
around one word that carries the whole meaning, and measured under this deployment's own
model those pairs sit closer together than any usable threshold:

    0.9867  chart of CO2 emissions 1990-2020   /  ...2000-2020
    0.9790  plot f(x) = sin(x)                 /  plot f(x) = sin(2x)
    0.9710  draw a BAR chart of X              /  draw a PIE chart of X

against a best genuine reword of 0.9906. So they live in their own scope partition and are
retrievable only by an exact hash of the normalised question.
"""
import pytest

from visualweaver import cache


class _FakeSemanticCache:
    """Records what it was asked, so the filter and distance can be asserted."""

    def __init__(self):
        self.checks, self.stores = [], []
        self.distance_threshold, self.ttl = 0.03, 3600

    def check(self, prompt=None, num_results=1, filter_expression=None, **kw):
        self.checks.append({"prompt": prompt, "filter": str(filter_expression), **kw})
        return []

    def store(self, prompt=None, response=None, metadata=None, filters=None):
        self.stores.append({"prompt": prompt, "filters": filters})

    def set_threshold(self, v): self.distance_threshold = v
    def set_ttl(self, v): self.ttl = v


@pytest.fixture
def fake(monkeypatch):
    from visualweaver import state
    c = _FakeSemanticCache()
    monkeypatch.setattr(state, "_semantic_cache", c)
    monkeypatch.setattr(state, "_semantic_cache_ready", True)
    monkeypatch.setattr(cache, "_get_semantic_cache", lambda: c)
    return c


# ── the question hash ────────────────────────────────────────────────────────
def test_the_same_question_hashes_the_same_however_it_is_typed():
    a = cache._qhash("Visualize The Math   Behind Fern Fractals")
    b = cache._qhash("  visualize the math behind fern fractals  ")
    assert a == b


def test_a_different_question_hashes_differently():
    assert cache._qhash("plot f(x) = sin(x)") != cache._qhash("plot f(x) = sin(2x)")
    assert cache._qhash("draw a bar chart of X") != cache._qhash("draw a pie chart of X")


def test_every_entry_carries_its_hash(fake):
    cache.cache_store("plot y = x^2", "ANSWER", None, "scope1")
    assert fake.stores[0]["filters"]["qhash"] == cache._qhash("plot y = x^2")
    assert fake.stores[0]["filters"]["scope"] == "scope1"


# ── the exact partition ──────────────────────────────────────────────────────
def test_the_exact_partition_serves_the_same_question(fake):
    """Without the hash filter the lookup falls back to similarity, where two different
    drawings measure 0.971 apart — and a bar-chart request was observed being answered
    with a pie chart."""
    cache.cache_lookup("plot f(x) = sin(x)", 0.98, "s", exact_only=True)
    f = fake.checks[0]["filter"]
    assert "qhash" in f and cache._qhash("plot f(x) = sin(x)") in f


def test_the_exact_partition_never_serves_a_different_drawing(fake):
    cache.cache_lookup("plot f(x) = sin(2x)", 0.98, "s", exact_only=True)
    assert cache._qhash("plot f(x) = sin(x)") not in fake.checks[0]["filter"]


def test_the_exact_partition_does_not_lean_on_the_distance(fake):
    """The tag decides there. Leaving the distance in play would re-admit the collisions
    the partition exists to prevent."""
    cache.cache_lookup("plot y = x^2", 0.98, "s", exact_only=True)
    assert fake.checks[0].get("distance_threshold") == 1.0


def test_an_ordinary_question_is_still_matched_by_similarity(fake):
    cache.cache_lookup("what is a vector database", 0.98, "s", exact_only=False)
    c = fake.checks[0]
    assert "qhash" not in c["filter"]
    assert "distance_threshold" not in c, "similarity matching was bypassed"


# ── the partitions cannot see each other ─────────────────────────────────────
def test_a_drawing_and_a_question_never_share_a_partition():
    """"explain knowledge graphs in redis" and "diagram knowledge graphs in redis"
    measure 0.8827 apart — inside the range an ordinary threshold accepts."""
    args = (["Ubuntu"], "gemini", "model", "", "prompt")
    assert cache._cache_scope(*args, visual=True) != cache._cache_scope(*args, visual=False)


def test_the_scope_still_separates_everything_it_did_before():
    base = dict(rag_instances=["a"], provider="g", model="m", source_filter="",
                system_prompt="p", visual=False)
    s = cache._cache_scope(**base)
    for field, other in (("rag_instances", ["b"]), ("provider", "x"), ("model", "y"),
                         ("source_filter", "f"), ("system_prompt", "q")):
        assert cache._cache_scope(**{**base, field: other}) != s, field


# ── a drawing request is recognised, not excluded ────────────────────────────
def test_a_drawing_request_is_routed_not_dropped():
    q = "visualize the math behind fern fractals and explain so that a 10 year old understands"
    assert cache.wants_visual(q) is True          # recognised -> exact partition
    assert cache.skip_reason(None, None, q) is None   # and NOT excluded


def test_an_attachment_is_still_excluded_outright():
    """That content is private to the turn and must never seed an entry another question
    could match."""
    assert cache.skip_reason(["img"], None, "what is this") is not None
    assert cache.skip_reason(None, [{"name": "a.txt"}], "what is this") is not None


# ── the vectorizer ───────────────────────────────────────────────────────────
def test_the_cache_vectorizer_applies_the_models_prefix(monkeypatch):
    """visualweaver.embeddings applies "query: " on the RAG path; the cache did not, so the
    two halves of the app embedded the same sentence differently — and at an identical
    safety floor the cache served a third of what it could."""
    from visualweaver import state
    seen = {}

    class _V:
        def __init__(self, model=None, **kw): seen["model"] = model
        def embed(self, content=None, **kw): seen["embedded"] = content; return [0.0]
        def embed_many(self, contents=None, **kw): seen["many"] = list(contents or []); return []

    import redisvl.utils.vectorize as vz
    monkeypatch.setattr(vz, "HFTextVectorizer", _V)
    monkeypatch.setattr(state, "_config", {"embedding": {"model": "intfloat/multilingual-e5-base"}})
    v = cache._make_cache_vectorizer()
    v.embed("how do i enable tls on redis")
    assert seen["embedded"].startswith("query: ")
    v.embed_many(["a", "b"])
    assert all(x.startswith("query: ") for x in seen["many"])


def test_a_model_without_a_prefix_is_left_alone(monkeypatch):
    from visualweaver import state

    class _V:
        def __init__(self, model=None, **kw): pass
        def embed(self, content=None, **kw): return content

    import redisvl.utils.vectorize as vz
    monkeypatch.setattr(vz, "HFTextVectorizer", _V)
    monkeypatch.setattr(state, "_config", {"embedding": {"model": "all-MiniLM-L6-v2"}})
    assert cache._make_cache_vectorizer().embed("hello") == "hello"


def test_the_marker_covers_the_prefix_too(monkeypatch):
    """The same model with and without the prefix puts a sentence in a different place,
    so vectors written under one are not comparable with lookups made under the other."""
    from visualweaver import state
    monkeypatch.setattr(state, "_config", {"embedding": {"model": "intfloat/multilingual-e5-base"}})
    with_prefix = cache._cache_embedding_model()
    monkeypatch.setattr(state, "_config", {"embedding": {"model": "all-MiniLM-L6-v2"}})
    assert "query: " in with_prefix
    assert "query: " not in cache._cache_embedding_model()


# ── the routes have to actually use it ───────────────────────────────────────
def test_both_chat_paths_route_drawings_to_the_exact_partition():
    """Testing cache_lookup directly proved the partition works while the caller could
    still be passing False — which is exactly how a guard ships dead."""
    import inspect
    from visualweaver import routes_chat
    src = inspect.getsource(routes_chat)
    assert src.count("exact_only = cache.wants_visual(query)") == 1
    assert src.count("exact_only      = cache.wants_visual(query)") == 1
    # passed to the lookup on both paths, and never hard-coded
    assert src.count("cache_scope, exact_only") == 2
    assert "cache_scope, False" not in src
    assert "cache_scope, True" not in src


def test_both_chat_paths_mark_the_scope_visual():
    import inspect
    from visualweaver import routes_chat
    assert inspect.getsource(routes_chat).count("visual=exact_only") == 2
