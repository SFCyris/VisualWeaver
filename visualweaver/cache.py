# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.cache — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import hashlib
import json
import re
from typing import Any, Optional
from redisvl.query.filter import Tag
from redisvl.extensions.cache.llm import SemanticCache
from . import config, constants, embeddings, rag_admin, redis_store, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SEMANTIC CACHE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _make_cache_vectorizer() -> Any:
    """
    Build an HFTextVectorizer that uses the same sentence-transformer model
    as the RAG embedder.  redisvl uses this to embed queries and responses
    before storing/looking up cache entries.

    The short model name (e.g. "all-MiniLM-L6-v2") is expanded to its full
    HuggingFace path so redisvl can resolve it correctly.
    """
    from redisvl.utils.vectorize import HFTextVectorizer  # noqa: PLC0415
    raw = state._config.get("embedding", {}).get("model", "all-MiniLM-L6-v2")
    model = raw if "/" in raw else f"sentence-transformers/{raw}"
    prefix = embeddings.embedding_spec(raw).get("query_prefix") or ""
    if not prefix:
        return HFTextVectorizer(model=model)

    # The E5 models are trained with "query: " on the text and score differently without
    # it. visualweaver.embeddings applies it on the RAG path; the cache did not, so the two
    # halves of the app embedded the same sentence differently. Measured on
    # tests/fixtures/cache_calibration.py under multilingual-e5-base, the prefix does not
    # move the safety floor (0.97 either way) but nearly triples what the cache can serve
    # at it: 6 of 30 genuine rewordings match instead of 2.
    # SemanticCache calls embed() itself and never passes a preprocess hook, and the
    # vectorizer is a pydantic model that refuses attribute assignment — hence a subclass.
    class _PrefixedVectorizer(HFTextVectorizer):
        def embed(self, content=None, **kw):
            return super().embed(prefix + (content or ""), **kw)

        def embed_many(self, contents=None, **kw):
            return super().embed_many([prefix + (c or "") for c in (contents or [])], **kw)

    return _PrefixedVectorizer(model=model)


def _cache_embedding_model() -> str:
    """What the cached vectors were produced by — the model AND the text fed to it.

    The prefix is part of the identity: the same model with and without "query: " puts a
    sentence in a different place, so vectors written under one are not comparable with
    lookups made under the other.
    """
    m = state._config.get("embedding", {}).get("model", "all-MiniLM-L6-v2")
    canonical = m if "/" in m else f"sentence-transformers/{m}"
    pre = embeddings.embedding_spec(m).get("query_prefix") or ""
    return f"{canonical}|{pre}" if pre else canonical


# The marker lives in Redis, NOT only in the process. An in-process marker is unreachable
# on the path that actually matters: routes_settings calls invalidate_redis_clients() on
# every POST /api/config, which nulls state._semantic_cache, so a guard conditioned on the
# live object never runs — and the marker would not survive a restart either, which is the
# case a real deployment hits (index built under one model, config changed, server
# restarted, 18 stale vectors still indexed at the old dimension).
_CACHE_MODEL_KEY = "visualweaver:semcache_model"


def _stored_cache_model() -> str | None:
    try:
        v = redis_store.r().get(_CACHE_MODEL_KEY)
    except Exception:                                             # pragma: no cover
        return None
    if v is None:
        return None
    return v.decode() if isinstance(v, bytes) else str(v)


def _cache_index_dim() -> int | None:
    """The vector width of the existing cache index, or None if there is no index."""
    try:
        info = redis_store.r().execute_command(
            "FT.INFO", constants.CACHE_PREFIX.rstrip(":"))
    except Exception:
        return None                      # no index yet, or Search unavailable
    flat = []
    def _walk(x):
        if isinstance(x, (list, tuple)):
            for y in x:
                _walk(y)
        else:
            flat.append(x.decode(errors="replace") if isinstance(x, bytes) else str(x))
    _walk(info)
    for i, tok in enumerate(flat):
        if tok == "dim" and i + 1 < len(flat):
            try:
                return int(flat[i + 1])
            except ValueError:
                return None
    return None


def _cache_model_changed() -> bool:
    """True when the cached vectors were produced by a different embedding model.

    Nothing invalidated the cache on an embedding-model change. Every stored prompt was
    still a vector from the OLD model, so a lookup embedded with the new one scored
    against incomparable geometry: either an outright dimension error, or — worse, when
    the dimensions happen to agree — plausible-looking distances that are meaningless,
    and an unrelated cached answer replayed as a hit.
    """
    was = state._semantic_cache_model or _stored_cache_model()
    if was is not None:
        return was != _cache_embedding_model()
    # No marker at all — every deployment that predates it, which is exactly the upgrade
    # this guard exists for. The width test catches a 384-wide index under a 768-wide
    # model, but two DIFFERENT models can share a width: swapping all-MiniLM-L6-v2 for
    # multilingual-e5-small (both 384) left every score collapsed to ~0.08, so the cache
    # never hit again while the entry count kept reporting them, for the whole 9-day TTL.
    # Unknown provenance is not a licence to keep scoring against it — discard once and
    # record the model, so the question is only ever asked on the first start.
    return _cache_index_dim() is not None


def _discard_stale_cache() -> None:
    """Drop the index AND the hashes behind it.

    redisvl's overwrite path issues DROPINDEX without DD, so the documents survive an
    index rebuild as orphans — they keep their old-model vectors and reappear the moment
    a matching index exists again.
    """
    try:
        rc = redis_store.r()
        try:
            rc.execute_command("FT.DROPINDEX", constants.CACHE_PREFIX.rstrip(":"), "DD")
        except Exception:
            pass                       # no index yet, or already gone
        dead = list(rc.scan_iter(match=f"{constants.CACHE_PREFIX}*", count=500))
        for i in range(0, len(dead), 500):
            rc.delete(*dead[i:i + 500])
        rc.delete(_CACHE_MODEL_KEY)
        if dead:
            log.info("Discarded %d cached answer%s embedded with the previous model.",
                     len(dead), "" if len(dead) == 1 else "s")
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Could not discard the stale semantic cache: {e}")


def effective_threshold() -> float:
    """The similarity the cache should actually use.

    An explicit setting wins; otherwise the model's measured calibration. The two are not
    interchangeable across models — see the note above embeddings.EMBEDDING_MODELS — so a
    deployment that has never touched this setting always gets a value that means what it
    says for the model it is running.
    """
    # `or {}` catches a falsy value, not a wrong TYPE. A hand-edited config.json
    # holding "cache": "on" reached .get() on a str and raised AttributeError out
    # of GET /api/config — the route the whole UI bootstraps from, which turned
    # one bad character into a settings screen that would not open.
    def _section(name):
        v = state._config.get(name)
        return v if isinstance(v, dict) else {}

    v = _section("cache").get("similarity_threshold")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return embeddings.cache_threshold_for(_section("embedding").get("model"))


def _reset_threshold_for_new_model() -> None:
    """Drop a threshold that was calibrated against the previous model.

    The number is not portable: 0.81 is merely permissive under all-MiniLM-L6-v2, where
    unrelated questions sit near 0.10, and serves the wrong answer under
    multilingual-e5-base, where they sit near 0.80. A value carried across the change is
    not a preference the user expressed about the new model — they never saw its scale.
    The cache itself is discarded on the same event for the same reason.
    """
    cfg = state._config.setdefault("cache", {})
    old = cfg.get("similarity_threshold")
    if old is None:
        return                       # already following the model
    # Back to FOLLOWING the model, not pinned to the number the model happens to want
    # today. Writing the figure looks identical in the panel and is not: it silently
    # opts the deployment out of the next calibration, which is the whole point of the
    # reset. The Settings checkbox reads this as "set by the embedding model".
    new = embeddings.cache_threshold_for(
        (state._config.get("embedding") or {}).get("model"))
    cfg["similarity_threshold"] = None
    log.info("Cache similarity threshold %.2f cleared (now %.2f, set by the embedding "
             "model): the previous value was calibrated for a different model and the "
             "models do not share a scale.", float(old), new)
    try:
        config.save_config(state._config)
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Could not persist the cache threshold: {e}")


def _apply_cache_config(c: "SemanticCache") -> None:
    """Re-apply the settings that can change without a restart.

    Only the threshold and TTL: the embedding model cannot be swapped on a live object,
    and its vectors would not be comparable anyway (see _cache_model_changed).
    """
    cfg = state._config.get("cache", {})
    try:
        want_dist = round(1.0 - effective_threshold(), 4)
        if abs(float(c.distance_threshold) - want_dist) > 1e-9:
            c.set_threshold(want_dist)
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Could not apply cache threshold: {e}")
    try:
        want_ttl = int(cfg.get("ttl", 3600))
        if int(c.ttl or 0) != want_ttl:
            c.set_ttl(want_ttl)
    except Exception as e:                                        # pragma: no cover
        log.warning(f"Could not apply cache TTL: {e}")


def _get_semantic_cache() -> "SemanticCache | None":
    """
    Lazily create and return the shared SemanticCache.

    Uses redisvl's SemanticCache which stores prompt→response pairs as
    vector embeddings and performs approximate-nearest-neighbour lookup on
    every incoming query.  A query is a cache hit when its cosine distance
    to a stored prompt is ≤ distance_threshold (i.e. similarity ≥ threshold).

    Returns None if the cache is disabled in config or if the Redis Search
    module is not available (logged once, never repeated).
    """
    if not state._config.get("cache", {}).get("enabled", True):
        return None
    if _cache_model_changed():
        was = state._semantic_cache_model or _stored_cache_model()
        log.info("Discarding the semantic cache: %s. Its vectors are not comparable "
                 "under %s.",
                 f"the embedding model changed from {was}" if was
                 else "it predates model tracking, so the model it was built with is "
                      "unknown",
                 _cache_embedding_model())
        _discard_stale_cache()
        state._semantic_cache = None
        state._semantic_cache_model = None
        _reset_threshold_for_new_model()
    if state._semantic_cache is not None:
        # The object is memoised, so everything read at build time was frozen there:
        # changing the cache threshold or TTL in Settings did nothing at all until the
        # process was restarted, with no indication. Worse than inert — redisvl filters
        # candidates at the BUILD-time distance, so lowering the threshold could not
        # widen what matched, and entries kept being written with the old TTL.
        _apply_cache_config(state._semantic_cache)
        return state._semantic_cache
    try:
        similarity_threshold = effective_threshold()
        ttl  = state._config.get("cache", {}).get("ttl", 3600)
        # SemanticCache uses cosine DISTANCE not SIMILARITY — convert
        def _build(overwrite: bool):
            return SemanticCache(
                name=constants.CACHE_PREFIX.rstrip(":"),   # "semcache"
                vectorizer=_make_cache_vectorizer(),
                distance_threshold=round(1.0 - similarity_threshold, 4),
                ttl=ttl,
                redis_client=redis_store.r(),
                # Scope tag — see _cache_scope(). A cached answer is only valid for the
                # exact corpus/provider/model/prompt it was produced under, so the scope
                # is an indexed TAG we filter on rather than part of the embedded text.
                # qhash: the exact-match partition. A request to DRAW something cannot
                # be served by similarity — two genuinely different charts measure 0.9785
                # apart on this deployment, above its own 0.97 threshold, and a bar-chart
                # request was observed being answered with a pie chart. So those entries
                # are retrievable only by an exact hash of the normalised question.
                filterable_fields=[{"name": "scope", "type": "tag"},
                                   {"name": "qhash", "type": "tag"}],
                overwrite=overwrite,
            )
        try:
            state._semantic_cache = _build(overwrite=False)
        except Exception as e:
            # An index built by an older version has no `scope` field, and redisvl
            # refuses to reuse a mismatched schema — which would leave the cache
            # permanently disabled. A cache is disposable, so rebuild it once;
            # entries are re-earned on the next few queries.
            if "does not match" not in str(e):
                raise
            log.info("Semantic cache schema changed (scope filter added) — rebuilding index; "
                     "previously cached answers are discarded.")
            state._semantic_cache = _build(overwrite=True)
        _apply_cache_config(state._semantic_cache)
        state._semantic_cache_model = _cache_embedding_model()
        try:
            redis_store.r().set(_CACHE_MODEL_KEY, state._semantic_cache_model)
        except Exception:                                         # pragma: no cover
            pass
        log.info("SemanticCache initialised (redisvl, scope-filtered)")
    except Exception as e:
        if "unknown command" in str(e).lower():
            log.warning(
                "Semantic cache disabled — Redis Search module not available. "
                "Use Redis Stack or Redis Enterprise with the Search module enabled."
            )
        else:
            log.warning(f"SemanticCache init failed: {e}")
        state._semantic_cache = None
    return state._semantic_cache


# Asking to SEE something, distinguished from merely mentioning a picture. The previous
# rule was a bare keyword list, and for a Redis product "graph", "plot" and "diagram" are
# domain vocabulary: it excluded ordinary questions from the cache — "what is graph
# theory", "explain knowledge graphs in Redis", "how do I read a flame graph", "what is
# the plot of Hamlet" — while missing real requests that name no keyword at all ("Sugar
# molecule in 2d and 3d", "sketch the architecture"). The corpus both directions are
# measured against is tests/test_visual_intent.py; the counts live there, not here, so
# they cannot go stale in a comment.
_V = (r"(?:plot|graph|chart|diagram|draw|sketch|render|visuali[sz]e|illustrate|animate"
      r"|map\s+out)")
_N = (r"(?:charts?|graphs?|plots?|diagrams?|histograms?|scatter\s*plots?|"
      r"bar\s*(?:graph|chart)s?|pie\s*charts?|line\s*(?:graph|chart)s?|"
      r"flow\s*charts?|flow\s*diagrams?|mind\s*maps?|timelines?|"
      r"visuali[sz]ations?|illustrations?|drawings?|pictures?|images?|svgs?|figures?)")
_ASK = r"(?:show|give|make|create|generate|produce|build|draw|need|want)"

# an imperative or a request to produce one
_VISUAL_REQUEST_RE = re.compile(
    rf"^\s*(?:please\s+)?{_V}\b"
    rf"|\b(?:can|could|would|will|please)\s+(?:you\s+)?(?:please\s+)?{_V}\b"
    rf"|\b{_ASK}\s+(?:me\s+|us\s+)?(?:an?|the|some)\s+{_N}\b"
    rf"|^\s*(?:an?\s+)?{_N}\s+of\b"
    rf"|\b{_V}\s+(?:me\s+|us\s+)?(?:an?|the|this|these)\s+\w"
    rf"|\b{_V}\s+(?:it|this|that|them)\b", re.I)

# A question that merely names a picture is a question. The trailing \b matters: without
# it "the" matched "theory" and "what is graph theory" was read as a drawing request.
_INFO_OPENER_RE = re.compile(
    r"^\s*(?:what|whats|what's|who|why|which|whose|when|where|is|are|was|were|does|do|"
    r"did|how|explain|describe|define|tell|list|summari[sz]e|summarise|compare|solve)\b", re.I)

# Unambiguous even inside a question. "what would this look like as a chart?" and "how
# about a bar chart of that?" are requests to see something, and the informational veto
# below was swallowing them because they open with "what" and "how".
_VISUAL_STRONG_RE = re.compile(
    rf"\bas\s+(?:an?|the)\s+{_N}\b"
    rf"|\binto\s+(?:an?|the)\s+{_N}\b"
    rf"|\b{_N}\s+(?:of|for)\s+(?:that|this|it|the\s+above)\b"
    rf"|^\s*(?:an?|the)\s+{_N}\s+(?:showing|of|for|with)\b"
    r"|\bin\s+(?:2d|3d)\b|\bas\s+(?:an?\s+)?svg\b", re.I)

# Weaker: a bare "<noun> of" or a formula. Only consulted once the informational veto has
# had its say, because "the diagram of the OSI model" appears in questions about one.
_VISUAL_PHRASE_RE = re.compile(
    rf"\b{_N}\s+of\b|\by\s*=\s*\S|\bf\s*\(\s*x\s*\)", re.I)

# Idioms that borrow a drawing verb for something that is not a drawing. Each is a fixed
# collocation, not a keyword: "draw up a plan", "draw your own conclusions", "the plot
# summary of a book", "render a template". Measured false positives, all four.
_NOT_VISUAL_RE = re.compile(
    r"\bdraw\s+(?:up|your\s+own|on|from|a\s+(?:distinction|parallel|comparison|conclusion))\b"
    r"|\bplot\s+summar(?:y|ies)\b"
    r"|\bthe\s+plot\s+of\b"
    r"|\brender(?:s|ing|ed)?\s+(?:the\s+)?(?:template|html|markdown|page|view|component)s?\b",
    re.I)


def wants_visual(query: str) -> bool:
    """
    True when the query asks for a chart/graph/plot.

    The semantic cache keys only on query text, so without this a chart request
    that is similar to an earlier text answer would return that text (no chart),
    and two similar chart requests would return the first one's SVG. We skip the
    cache (lookup and store) for visual-intent queries so every chart is fresh.
    """
    q = query or ""
    if _NOT_VISUAL_RE.search(q):
        return False
    if _VISUAL_REQUEST_RE.search(q) or _VISUAL_STRONG_RE.search(q):
        return True
    if _INFO_OPENER_RE.match(q):
        return False
    return bool(_VISUAL_PHRASE_RE.search(q))


# (short label shown ON the badge, full sentence available beside it). A reason that
# lives only in a title attribute is invisible on touch and to anyone not hunting for it
# — which is how a working cache reads as a broken one.
_SKIP_REASONS = {
    "image":  ("image attached", "an image is attached"),
    "file":   ("file attached", "a file is attached to this turn"),
}
# A request to DRAW something is NOT in here any more. It used to be, and that one flag
# gated the lookup and the store together, so asking the identical question twice cost
# two model calls and left a permanent hole: a later non-visual rephrase could not hit
# either, because nothing had ever been written. Those turns are cached now, in their own
# scope partition, retrievable only by an exact hash of the question — see cache_lookup.


def skip_reason(images, file_context, query: str) -> str | None:
    """Why this turn will not use the cache, phrased for the user, or None.

    One definition for both the websocket and HTTP paths: they had the same condition
    written out twice, so the two could disagree about whether a turn was cacheable.
    The wording is user-facing — it is shown on the answer.
    """
    k = skip_kind(images, file_context, query)
    return _SKIP_REASONS[k][1] if k else None


def skip_kind(images, file_context, query: str = "") -> str | None:
    """Which rule excluded this turn entirely, as a stable key the UI can label.

    Attachments only: their content is private to the turn and must never seed an entry
    another question could match. A drawing request is not excluded — it is cached in an
    exact-match partition instead.
    """
    if images:
        return "image"
    if file_context:
        return "file"
    return None


def skip_label(kind: str | None) -> str | None:
    """The short form, for the badge itself."""
    return _SKIP_REASONS[kind][0] if kind in _SKIP_REASONS else None


async def _effective_rag_instances(rag_instances: list[str] | None) -> list[str]:
    """The instances a query will actually search — disabled ones are dropped.

    The cache scope must be built from this, not from the requested list. An
    instance toggled off produces an ungrounded answer; scoping it as "answered
    against X" meant re-enabling X replayed that ungrounded answer as a hit.
    """
    out: list[str] = []
    for name in rag_instances or []:
        try:
            meta, _ep = await rag_admin._rag_meta_cached_async(name)
            if (meta or {}).get("enabled", True):
                out.append(name)
        except Exception:
            out.append(name)   # unknown state: assume it counts, never cross scopes
    return out


def _cache_scope(rag_instances: list[str] | None = None, provider: str = "",
                 model: str = "", source_filter: str = "",
                 system_prompt: str = "", visual: bool = False) -> str:
    """Identity of the *conditions* an answer was produced under.

    A semantic cache keyed on the question alone is wrong in two ways:

      * **Correctness** — the same question against a different knowledge base,
        provider, model or system prompt has a different correct answer, but the
        cache would replay the first one (together with the *other* corpus's
        chunks as its provenance).
      * **Privacy** — on a shared instance, an answer derived from one user's
        uploaded document would be served to anyone whose question landed within
        the similarity threshold.

    Everything that can change the answer therefore goes into a scope tag, and
    lookups are filtered to a matching scope. Instances are sorted so that
    ``[a,b]`` and ``[b,a]`` share a cache.
    """
    payload = "|".join([
        ",".join(sorted(rag_instances or [])),
        provider or "",
        model or "",
        source_filter or "",
        hashlib.sha256((system_prompt or "").encode()).hexdigest()[:16],
        # Requests to DRAW something live in their own partition. Without this a question
        # about a diagram could be answered with a diagram, and vice versa: "explain
        # knowledge graphs in redis" and "diagram knowledge graphs in redis" measure
        # 0.8827 apart, which is inside the range an ordinary threshold accepts.
        "v" if visual else "t",
    ])
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _normalize_query(q: str) -> str:
    """Canonical cache key for a query: collapse runs of whitespace and casefold,
    so trivial variants ("Enable X?" / "enable  x ?") map to the same entry and hit
    even at a high similarity threshold. Applied IDENTICALLY on lookup and store.
    It only folds case and whitespace — different words ("enable" vs "disable")
    stay distinct, so it never merges semantically different questions."""
    return " ".join((q or "").split()).casefold()


def _qhash(query: str) -> str:
    """Identity of the question itself, after the same normalisation the vector sees."""
    return hashlib.sha256(_normalize_query(query).encode()).hexdigest()[:32]


def cache_lookup(query: str, threshold: float = 0.92, scope: str = "",
                 exact_only: bool = False) -> dict | None:
    """
    Look up the nearest cached response via redisvl SemanticCache.
    Returns {"response": str, "score": float} or None.

    ``scope`` (see _cache_scope) restricts the search to entries produced under
    the same corpus/provider/model/prompt; an empty scope matches only entries
    stored without one.

    ``exact_only`` requires the stored question to be the SAME question, not a similar
    one. Used for requests to draw something: measured on this deployment, "draw a bar
    chart of redis memory usage by keyspace" and the same sentence with "pie" sit at
    0.9785 — above the configured threshold — while the only genuine repeats measured
    0.9851 and 0.9932. There is no threshold in a 0.015-wide window; asking the identical
    question is the only safe match, and it is also the case worth serving.
    """
    # During the first few seconds after a restart the background warm may not have built
    # the vectorizer yet; treat that as a cache miss rather than paying the ~3 s build here.
    if state._semantic_cache is None and not state._semantic_cache_ready:
        return None
    cache = _get_semantic_cache()
    if cache is None:
        return None
    try:
        flt = Tag("scope") == (scope or "_none")
        if exact_only:
            flt = flt & (Tag("qhash") == _qhash(query))
        # The vector search still runs — redisvl has no non-vector path — but on the
        # exact partition the tag decides, so the distance is opened right up rather
        # than being asked to do work it has been measured to be bad at.
        hits = cache.check(prompt=_normalize_query(query), num_results=1,
                           filter_expression=flt,
                           **({"distance_threshold": 1.0} if exact_only else {}))
        if hits:
            h = hits[0]
            dist  = float(h.get("vector_distance", h.get("score", 1.0)))
            score = round(1.0 - dist, 4)
            if exact_only or score >= threshold:
                try:
                    meta = h.get("metadata") or {}
                    cached_chunks = json.loads(meta.get("chunks_json", "[]"))
                except Exception:
                    cached_chunks = []
                return {"response": h.get("response", ""), "score": score, "entry_id": h.get("entry_id", ""), "chunks": cached_chunks}
    except Exception as e:
        log.error(f"Cache lookup error: {e}")
    return None


def cache_store(query: str, response: str, chunks: list | None = None, scope: str = ""):
    """Store a query→response pair in the SemanticCache with TTL.
    Chunks are stored as JSON metadata so they can be re-displayed on cache hits.
    ``scope`` tags the entry with the conditions it was produced under so a later
    lookup under different conditions cannot match it (see _cache_scope).
    """
    # Skip storing during the warm window rather than triggering the ~3 s inline build.
    if state._semantic_cache is None and not state._semantic_cache_ready:
        return
    cache = _get_semantic_cache()
    if cache is None:
        return
    try:
        metadata = {"chunks_json": json.dumps(chunks)} if chunks else None
        # Every entry carries its question hash, so the exact partition costs nothing to
        # maintain and an ordinary entry can still be found by similarity.
        cache.store(prompt=_normalize_query(query), response=response, metadata=metadata,
                    filters={"scope": scope or "_none", "qhash": _qhash(query)})
    except Exception as e:
        log.error(f"Cache store error: {e}")

