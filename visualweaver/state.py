# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.state — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import random
import pathlib
import os
from collections import OrderedDict
from typing import Any, Optional
import httpx
import redis

log = logging.getLogger(__name__)


_semantic_cache: "SemanticCache | None" = None
_semantic_cache_ready = False
# The embedding model the cached vectors were produced with. Vectors from a different
# model are not comparable, so a change has to invalidate the cache rather than score
# against it — see cache._cache_model_changed.
_semantic_cache_model: str | None = None
_search_available: dict[str, bool | None] = {}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# GLOBAL STATE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_config: dict = {}

# Redis client pool: "default" key → primary client; other keys → named endpoints
_redis_clients: dict[str, redis.Redis] = {}

# Runtime reachability of each configured Redis endpoint.
# Keys are endpoint names ("default", "RAG", …); value is True/False.
# Unknown endpoints are treated as reachable (optimistic default).
_endpoint_health: dict[str, bool] = {}

_embed_model: Optional[Any] = None  # SentenceTransformer, lazily loaded
_embed_model_name: str = ""

# In-memory session store: session_id → list of {role, content} dicts
# Bounded: every conversation ever opened used to stay resident for the life of
# the process, so RSS grew on uptime alone. Sessions are persisted in Redis, so
# evicting the least-recently-touched one only costs a reload on next access.
_MAX_LIVE_SESSIONS = int(os.environ.get("VISUALWEAVER_MAX_LIVE_SESSIONS", "500"))
_sessions: "OrderedDict[str, list]" = OrderedDict()


def touch_session(sid: str) -> None:
    """Mark a session most-recently-used and evict the coldest beyond the cap."""
    if sid in _sessions:
        _sessions.move_to_end(sid)
    while len(_sessions) > _MAX_LIVE_SESSIONS:
        old, _ = _sessions.popitem(last=False)
        log.debug(f"evicted session {old} from memory (still in Redis)")


def reap_finished_crawls() -> int:
    """Drop completed crawl tasks. They were never removed, so every crawl left a
    dead asyncio.Task behind for the life of the process."""
    done = [u for u, t in _crawl_tasks.items() if t.done()]
    for u in done:
        _crawl_tasks.pop(u, None)
        _crawl_gates.pop(u, None)
    return len(done)

_feedback: list = []
_ingestion_logs: list = []
_crawl_tasks: dict[str, asyncio.Task] = {}
# Seed URL -> gate. The event is SET while running and cleared while paused, so a
# worker awaiting it blocks between pages. Pausing between pages (rather than
# killing the task) keeps the queue, the visited set and the URL skip-list intact,
# so resuming continues instead of restarting.
_crawl_gates: dict[str, asyncio.Event] = {}
# Active crawl state — survives browser refresh/reconnect.
# Key: url.  Value: {instance, pages_done, chunks, errors, blocked, start_ts, done}
_active_crawls: dict[str, dict] = {}

# Active file-ingest jobs, the disk-file twin of the crawl state above. A file ingest
# used to be invisible the moment its SSE stream was lost: no way to see one running,
# and no way to stop one — closing the tab left it indexing with nobody watching.
# Key: job id.  Value: {job, instance, total, index, ok, errors, files, done, cancelled}
_active_ingests: dict[str, dict] = {}
# Job id -> cancel flag, SET to ask the ingest loop to stop before the next file.
# Separate from the state dict above because that one is returned as JSON.
_ingest_cancels: dict[str, asyncio.Event] = {}
# How many finished jobs to keep so a reconnecting UI can still read the outcome.
_INGEST_HISTORY = 20


# A job registered this long ago whose generator never ran will never run: the response
# body was dropped before the first chunk. Generous enough that a slow client cannot be
# mistaken for one.
_INGEST_START_GRACE = 60.0


def reap_finished_ingests(now: float | None = None) -> int:
    """Drop finished jobs beyond the keep-window, and any job that never started.

    Finished jobs are kept deliberately — a browser that reconnects after the stream
    ended still needs to learn how it ended — but keeping every one of them for the
    life of the process is a leak, which is what the crawl equivalent above does.

    The never-started case is a different leak with a worse symptom. Registration happens
    before the streaming response is returned; everything that clears a job lives in the
    generator's ``finally``. A client that disconnects before pulling the first chunk
    therefore leaves a job that is registered, not done, and unreachable — and the UI
    attaches to the first not-done job it finds, so that phantom freezes the ingest panel
    for good. Reaping it also removes the uploads it left behind.
    """
    import time as _time
    now = _time.time() if now is None else now
    dropped = 0

    for j, st in list(_active_ingests.items()):
        if st.get("done") or st.get("started"):
            continue
        if now - float(st.get("registered_at", now)) < _INGEST_START_GRACE:
            continue
        for name in st.get("upload_paths", []):
            try:
                pathlib.Path(name).unlink(missing_ok=True)
            except OSError:
                pass
        _active_ingests.pop(j, None)
        _ingest_cancels.pop(j, None)
        dropped += 1

    finished = [j for j, st in _active_ingests.items() if st.get("done")]
    stale = finished[:-_INGEST_HISTORY] if len(finished) > _INGEST_HISTORY else []
    for j in stale:
        _active_ingests.pop(j, None)
        _ingest_cancels.pop(j, None)
        dropped += 1
    return dropped

# Per-instance RAG query statistics (in-memory, reset on restart).
# Keys: instance name.  Values: counters used to derive hit-rate and avg score.
# Structure: {name: {queries, hits, chunks_total, score_sum}}
_rag_stats: dict[str, dict] = {}
_reranker: Optional["CrossEncoder"] = None
_reranker_model_name: str = ""
_recrawl_task: Optional[asyncio.Task] = None
_watch_task: Optional[asyncio.Task] = None


# ── Retrieval quality, kept so settings advice can be measured rather than guessed ──
# A histogram of the best PRE-threshold cosine per query. The mean alone says whether a
# threshold is roughly too strict; the distribution says which value to use instead.
RAW_HIST_BUCKETS = 20                      # 0.00-0.05, 0.05-0.10, … 0.95-1.00
CITED_HIST_RANKS = 20                      # how often the answer cited the chunk at rank i
# A bounded uniform sample of the ACTUAL scores, kept alongside the histogram.
# Buckets cannot answer the question the advice is for: with the threshold slider
# stepping in 0.01 and buckets 0.05 wide, a corpus scoring 0.81 and a threshold of 0.82
# are indistinguishable inside one bucket, and any pass-rate read off it is a guess at
# the shape within. 500 float32s per instance is ~4 KB of JSON and makes the pass rate
# and the quantile exact on the sample rather than modelled.
RAW_SAMPLE_SIZE = 500
# Cosine scores are stored to 4 decimals: 100x finer than the 0.01 the slider steps in,
# so no verdict can turn on the difference, and it more than halves what each flush
# writes to Redis (a 500-entry sample is 3.9 KB rounded against 9.8 KB at full repr).
RAW_SAMPLE_DP = 4
RAG_STATS_TEMPLATE = {
    "queries": 0, "hits": 0, "chunks_total": 0, "score_sum": 0.0,
    "raw_score_sum": 0.0,   # sum of best raw scores (pre-threshold) across all queries
    "errors": 0,            # searches that raised — counted, never scored
    "no_candidates": 0,     # searches that returned nothing at all — counted, never scored
    "scored": 0,            # searches that produced a candidate: the sample's real size
    "cache_replays": 0,     # questions answered from cache: retrieval never ran, so they
                            # are absent from every number below and the panel says so
    "answers_with_citations": 0,
}
# Non-scalar row fields, kept out of the template above because dict(TEMPLATE) is a
# shallow copy and would share one list (or one dict) across every instance.
RAG_STATS_LISTS = ("raw_hist", "cited_hist", "raw_sample")
# Only scalars belong in the template above: dict(TEMPLATE) is a shallow copy, so a list
# left in it would be the SAME list in every instance's row, and one corpus's scores
# would silently accumulate into all of them.


def new_stats_row() -> dict:
    """A complete, independent counter row. Fields were previously added lazily by
    whichever recorder ran first, so a row's shape depended on its history."""
    return dict(RAG_STATS_TEMPLATE,
                raw_hist=[0] * RAW_HIST_BUCKETS,
                cited_hist=[0] * CITED_HIST_RANKS,
                raw_sample=[],
                regime=current_regime(),
                alt={})
# Instances whose counters changed since the last flush to Redis.
_rag_stats_dirty: set[str] = set()


def raw_bucket(score: float) -> int:
    """Bucket index for a cosine score. Clamped: a score of exactly 1.0 belongs in the
    last bucket, not one past the end."""
    try:
        b = int(float(score) * RAW_HIST_BUCKETS)
    except (TypeError, ValueError):
        return 0
    return max(0, min(RAW_HIST_BUCKETS - 1, b))


def record_rag_failure(instance: str) -> None:
    """A search that RAISED is not an observation about the corpus.

    Recording failures as "best score 0.0" made a broken index indistinguishable from a
    badly-tuned threshold, and the advice then blamed the threshold and offered to set
    it to zero — while the real fault (usually a changed embedding model needing a
    re-ingest) went unnamed.
    """
    if not instance:
        return
    s = _rag_stats.setdefault(instance, new_stats_row())
    s["errors"] = s.get("errors", 0) + 1
    _rag_stats_dirty.add(instance)


# A recorded cosine only means something alongside others produced the same way. Two
# things change that geometry: the embedding model, and whether the text embedded was the
# QUESTION or a HyDE hypothesis (answer-shaped prose sits elsewhere in the space —
# measured on this deployment's own 57,805-chunk index, question mode averaged 0.676 and
# HyDE mode 0.750, a shift larger than the spread the threshold has to be read against).
# Pooling them makes the sample bimodal and the derived threshold serves neither.
#
# Pool over things that are simultaneously true (a query hits three corpora at once and
# one threshold must serve all three); split over things that are alternatives (only one
# retrieval mode is ever live).
MAX_REGIMES = 3
UNKNOWN_REGIME = "?"          # history recorded before regimes were tracked


def current_regime() -> str:
    model = ((_config.get("embedding") or {}).get("model") or "default")
    mode = "h" if (_config.get("hyde") or {}).get("enabled", False) else "q"
    return f"{model}:{mode}"


def regime_budget(s: dict) -> int:
    """This regime's share of the sample budget. One reservoir's worth, split."""
    return max(1, RAW_SAMPLE_SIZE // max(1, len(s.get("alt") or {}) + 1))


def _switch_regime(s: dict, regime: str) -> None:
    """Park the live reservoir under its own name and start a clean one.

    Nothing is thrown away: a user who toggles HyDE back gets their history back. The
    budget is shared, so each regime is downsampled to its share — dropping uniformly at
    random from a uniform sample leaves it uniform, so the quantile stays unbiased.
    """
    alt = s.setdefault("alt", {})
    old = s.get("regime") or UNKNOWN_REGIME
    # Everything DERIVED FROM SCORES moves together. Parking only the sample left the
    # histogram, the mean and the scored count pooled across modes, so the chart drawn
    # under the slider and the percentage drawn on top of it came from two different
    # populations — and for the first 20 turns after a switch the corpus voted off the
    # OLD mode's histogram, wanting 0.55 where the live mode needed 0.26.
    # `queries`, `hits`, `errors` and the citation ranks stay pooled: they are counts of
    # events, not measurements in a geometry.
    if s.get("raw_sample") or sum(s.get("raw_hist") or []):
        alt[old] = {"n": int(s.get("scored") or 0),
                    "sum": float(s.get("raw_score_sum") or 0.0),
                    "s": list(s.get("raw_sample") or []),
                    "h": list(s.get("raw_hist") or [0] * RAW_HIST_BUCKETS)}
    s["regime"] = regime
    prev = alt.pop(regime, None)
    s["raw_sample"] = list(prev["s"]) if prev else []
    s["raw_hist"] = list(prev.get("h") or [0] * RAW_HIST_BUCKETS) if prev \
        else [0] * RAW_HIST_BUCKETS
    s["scored"] = int(prev.get("n") or 0) if prev else 0
    s["raw_score_sum"] = float(prev.get("sum") or 0.0) if prev else 0.0
    # oldest-first eviction, and never the live one
    while len(alt) >= MAX_REGIMES:
        alt.pop(next(iter(alt)))
    share = regime_budget(s)
    for slot in list(alt.values()) + [{"s": s["raw_sample"]}]:
        if len(slot["s"]) > share:
            slot["s"][:] = random.sample(slot["s"], share)


def _reservoir_add(s: dict, score: float) -> None:
    """Keep a uniform sample of every score seen, in bounded memory.

    Classic reservoir sampling: the first RAW_SAMPLE_SIZE scores are kept outright, and
    each later one replaces a random slot with probability SIZE/n. Every score seen has
    the same chance of being in the sample, so a quantile of the sample estimates the
    quantile of the whole stream — which a histogram can only do to bucket resolution.
    """
    res = s.setdefault("raw_sample", [])
    # Count SCORED observations, not queries: a query that retrieved nothing never
    # reaches the reservoir, so using `queries` would make the replacement probability
    # SIZE/n too small and under-sample everything after the first 500.
    n = s.get("scored") or s["queries"]    # already incremented for this observation
    # The budget is shared across regimes, not per regime — otherwise parking one mode
    # and refilling another grew the row by a whole reservoir each time a user toggled
    # HyDE, and the persisted blob is written on every grounded turn.
    cap = regime_budget(s)
    if len(res) > cap:
        del res[cap:]
    if len(res) < cap:
        res.append(round(float(score), RAW_SAMPLE_DP))
        return
    j = random.randrange(n)
    if j < cap:
        res[j] = round(float(score), RAW_SAMPLE_DP)


def record_citations(instance: str, cited_ranks: list[int],
                     count_answer: bool = True) -> None:
    """Record which chunk RANKS an answer actually cited.

    Without this, "is top_k too high" is unanswerable: the app knows how many chunks it
    sent but not how many were used. Ranks are 1-based, matching the [n] markers.
    """
    if not instance or not cited_ranks:
        return
    s = _rag_stats.setdefault(instance, new_stats_row())
    s.setdefault("cited_hist", [0] * CITED_HIST_RANKS)
    for r in cited_ranks:
        if 1 <= r <= CITED_HIST_RANKS:
            s["cited_hist"][r - 1] += 1
    if count_answer:
        s["answers_with_citations"] = s.get("answers_with_citations", 0) + 1
    _rag_stats_dirty.add(instance)


def record_cache_replay(instance: str) -> None:
    """A question answered from cache, against a corpus that would otherwise have been
    searched.

    It contributes NOTHING to the score distribution — no retrieval ran, so there is no
    score, and inventing one would make every sentence in the panel false. It is counted
    for one purpose: the panel can say how much of the traffic its numbers actually speak
    for. At a 90% hit rate it takes 200 questions to reach the 20 the verdict needs, and
    without this line the user cannot tell a quiet deployment from a well-cached one.
    """
    if not instance:
        return
    s = _rag_stats.setdefault(instance, new_stats_row())
    s["cache_replays"] = s.get("cache_replays", 0) + 1
    _rag_stats_dirty.add(instance)


def drop_rag_stats(instance: str) -> None:
    """Forget a deleted corpus. Its scores would otherwise keep steering the advice for
    a corpus that no longer exists — and reappear on every restart, since the row is
    persisted."""
    if _rag_stats.pop(instance, None) is not None:
        _rag_stats_dirty.add(instance)


def _record_rag_stats(
    instance: str,
    results: list[dict],
    raw_results: list[dict] | None = None,
) -> None:
    """Update per-instance RAG stats after every search_rag() call.

    ``results``     — chunks that passed the similarity threshold (used for hit counting).
    ``raw_results`` — all KNN results before threshold filtering; used to track the best
                      raw score so we can detect threshold misconfiguration (e.g. good
                      matches getting filtered out because the threshold is too strict).
    """
    s = _rag_stats.setdefault(instance, new_stats_row())
    s["queries"] += 1
    # Best raw cosine among the pre-threshold candidates. Use max(), not [0]:
    # results are ordered by fused RRF rank, so [0] can be a keyword-only hit
    # with cosine 0 even when a strong vector match is present further down.
    if not raw_results:
        # Nothing was retrieved at all — an empty corpus, or an index that matched
        # nothing. That is not evidence about where the threshold belongs, and scoring
        # it as "best score 0.00" drags the whole distribution down: on a deployment
        # with one empty instance every preset derived a threshold of 0.00, which
        # accepts every chunk. Counted so the panel can name it, never scored.
        s["no_candidates"] += 1
        _rag_stats_dirty.add(instance)
        return
    reg = current_regime()
    if s.get("regime") != reg:
        _switch_regime(s, reg)
    s["scored"] += 1
    best_raw = max((c.get("score", 0.0) for c in raw_results), default=0.0)
    s["raw_score_sum"] += best_raw
    # A running mean cannot answer "what threshold keeps 90% of my queries" — that needs
    # the shape, not the average. Twenty buckets of 0.05 is enough to read a percentile
    # off and costs twenty ints per instance.
    s["raw_hist"][raw_bucket(best_raw)] += 1
    _reservoir_add(s, best_raw)
    if results:
        s["hits"]         += 1
        s["chunks_total"] += len(results)
        s["score_sum"]    += results[0]["score"]   # top-1 cosine similarity (post-filter)
    _rag_stats_dirty.add(instance)
# Active streaming task per session — used to cancel mid-stream when the client
# sends {"type":"abort"}.  Keyed by session id.
_chat_tasks: dict[str, asyncio.Task] = {}

# API keys sourced from environment variables — never persisted to disk
_env_key: str = ""          # ANTHROPIC_API_KEY
_openai_env_key: str = ""   # OPENAI_API_KEY
_qwen_env_key: str = ""     # DASHSCOPE_API_KEY
_mistral_env_key: str = ""  # MISTRAL_API_KEY
_groq_env_key: str = ""     # GROQ_API_KEY
_gemini_env_key: str = ""   # GEMINI_API_KEY

_config_load_error: str = ""
_shared_http_client: httpx.AsyncClient | None = None
_ollama_models_cache: list[dict] = []
_ollama_models_ts: float = 0.0
_rag_meta_gen = 0
