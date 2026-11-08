# SPDX-License-Identifier: AGPL-3.0-or-later
"""Performance regressions found in the 1.15.0 review (internal/REVIEW-1.15.0.md §3).

Each test pins the BEHAVIOUR that made the path slow, not a timing threshold: what was
imported at startup, how many times the query went through the embedding model, which
Redis commands a listing issued, whether the two MongoDB legs overlapped, and in what
order the cross-encoder was handed its pairs.
"""
import json
import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np
import pytest

from conftest import KEY_PREFIX

ROOT = Path(__file__).resolve().parents[1]


# ── 1. the cross-encoder is not imported at startup ─────────────────────────────

def test_importing_the_app_does_not_import_the_cross_encoder(tmp_path):
    """`embeddings` and `routes_settings` both ran `from sentence_transformers import
    CrossEncoder` at module import, only to set HAS_CROSSENCODER — which pulled in
    sentence-transformers, transformers and torch (~2.5 s of the 4.4 s before the first
    /api/health could answer) on every start, reranker enabled or not.

    A fresh interpreter, because this process has long since imported everything."""
    (tmp_path / "config.json").write_text("{}")
    code = (
        "import sys, json\n"
        "import visualweaver.main as m\n"
        "from visualweaver import embeddings, routes_settings\n"
        "heavy = sorted(k for k in ('sentence_transformers', 'transformers', 'torch') if k in sys.modules)\n"
        "print(json.dumps({'file': m.__file__, 'heavy': heavy,\n"
        "                  'flag': embeddings.HAS_CROSSENCODER,\n"
        "                  'health': routes_settings.api_health()['services']['reranker']}))\n"
    )
    env = dict(os.environ, VISUALWEAVER_DATA_DIR=str(tmp_path), PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["file"].startswith(str(ROOT)), f"imported the wrong checkout: {out['file']}"
    assert out["heavy"] == [], f"imported at startup: {out['heavy']}"
    # Still truthful without the import: the package is installed in this venv.
    import importlib.util
    installed = importlib.util.find_spec("sentence_transformers") is not None
    assert out["flag"] is installed and out["health"] is installed, out


def test_the_reranker_class_is_imported_on_first_use_and_a_failed_import_is_reported(monkeypatch):
    """The lazy import must still produce the reranker, through a patched module like the
    tests use, and a sentence-transformers that cannot be imported after all must turn
    the health flag off rather than keep promising a reranker that is not there."""
    from visualweaver import embeddings, routes_settings, state

    class _CE:
        def __init__(self, arg, *a, **kw):
            self.arg = arg

    fake = types.ModuleType("sentence_transformers")
    fake.CrossEncoder = _CE
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    monkeypatch.setattr(embeddings, "CrossEncoder", None, raising=False)
    monkeypatch.setattr(embeddings, "HAS_CROSSENCODER", True)
    monkeypatch.setattr(embeddings, "_local_snapshot", lambda repo: None)
    monkeypatch.setattr(embeddings, "_prepare_reranker", lambda m: m, raising=False)
    monkeypatch.setitem(state._config, "reranker", {"enabled": True, "model": "org/r"})
    saved = (state._reranker, state._reranker_model_name)
    state._reranker, state._reranker_model_name = None, ""
    try:
        got = embeddings.get_reranker()
        assert isinstance(got, _CE) and got.arg == "org/r"

        # Now the import itself fails.
        state._reranker, state._reranker_model_name = None, ""
        broken = types.ModuleType("sentence_transformers")   # no CrossEncoder attribute
        monkeypatch.setitem(sys.modules, "sentence_transformers", broken)
        monkeypatch.setattr(embeddings, "CrossEncoder", None)
        assert embeddings.get_reranker() is None
        assert embeddings.HAS_CROSSENCODER is False
        assert routes_settings.api_health()["services"]["reranker"] is False
    finally:
        state._reranker, state._reranker_model_name = saved


# ── 5. reranker: length-sorted batches; half precision on a GPU when chosen ─────

class _StubCE:
    """A CrossEncoder stand-in: records what predict() was handed, scores by a fixed
    per-text value so the right answer is known whatever order the pairs arrive in."""

    def __init__(self, device="cpu", nan_first=False):
        self._target_device = types.SimpleNamespace(type=device)
        self.calls = []
        self.precision = []
        self.nan_first = nan_first
        outer = self

        class _M:
            def half(self):
                outer.precision.append("half")
                return self

            def float(self):
                outer.precision.append("float")
                return self

        self.model = _M()

    @staticmethod
    def score(text):
        # deterministic, unrelated to length, so a sort that is not undone shows
        return (sum(map(ord, text)) % 97) / 97.0

    def predict(self, pairs, batch_size=32, show_progress_bar=None, **kw):
        self.calls.append({"texts": [p[1] for p in pairs], "batch_size": batch_size,
                           "bar": show_progress_bar})
        if self.nan_first and len(self.calls) == 1:
            return np.full(len(pairs), np.nan, dtype=np.float32)
        return np.asarray([self.score(p[1]) for p in pairs], dtype=np.float32)


def _candidates(n=12):
    words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()
    # lengths deliberately out of order
    return [{"text": " ".join(words[: (i * 7) % 11 + 1]) + f" #{i}", "source": f"s{i}"}
            for i in range(n)]


def test_rerank_sorts_pairs_by_length_and_maps_scores_back(monkeypatch):
    """The pairs went to the cross-encoder in retrieval order, 32 to a batch, so every
    batch padded to the longest chunk anywhere in it (one 512-token chunk made all 32 pay
    for 512): 641 ms on the CPU for 40 candidates where sorted batches of 8 take 380 ms.
    Sorting must be undone on the way back, or each chunk gets another chunk's score."""
    from visualweaver import embeddings
    stub = _StubCE("cpu")
    monkeypatch.setattr(embeddings, "get_reranker", lambda: stub)
    chunks = _candidates()
    expect = sorted(chunks, key=lambda c: _StubCE.score(c["text"]), reverse=True)[:5]
    got = embeddings.rerank_chunks("q", [dict(c) for c in chunks], 5)

    assert len(stub.calls) == 1
    sent = stub.calls[0]["texts"]
    assert [len(t) for t in sent] == sorted((len(t) for t in sent), reverse=True), \
        "pairs were not handed over longest first"
    assert stub.calls[0]["batch_size"] == 8, stub.calls[0]
    assert stub.calls[0]["bar"] is False
    assert [c["text"] for c in got] == [c["text"] for c in expect]
    for c in got:
        assert c["reranker_score"] == round(_StubCE.score(c["text"]), 4)


def test_half_precision_is_used_only_on_a_gpu_and_dropped_if_it_overflows(monkeypatch, cfg):
    """With reranker.half_precision on (off by default: test_uxfix_backend)."""
    from visualweaver import embeddings
    cfg["reranker"] = {**(cfg.get("reranker") or {}), "half_precision": True}
    for dev, want in (("cpu", []), ("cuda", ["half"]), ("mps", ["half"])):
        stub = embeddings._prepare_reranker(_StubCE(dev))
        assert stub.precision == want, (dev, stub.precision)
    assert _StubCE("mps").calls == []

    gpu = embeddings._prepare_reranker(_StubCE("mps", nan_first=True))
    monkeypatch.setattr(embeddings, "get_reranker", lambda: gpu)
    chunks = _candidates()
    got = embeddings.rerank_chunks("q", [dict(c) for c in chunks], 3)
    assert gpu.precision == ["half", "float"], gpu.precision
    assert len(gpu.calls) == 2 and gpu.calls[1]["batch_size"] == 16
    assert all(np.isfinite(c["reranker_score"]) for c in got)
    expect = sorted(chunks, key=lambda c: _StubCE.score(c["text"]), reverse=True)[:3]
    assert [c["text"] for c in got] == [c["text"] for c in expect]


@pytest.mark.slow
def test_sorted_and_half_precision_reranking_keep_the_real_models_top_five(request, cfg):
    """On the real cross-encoder (from the local cache only — never downloaded): the
    length-sorted path, and half precision (when chosen) where a GPU is present, rank the
    same top 5 as a plain predict over the same 40 candidates."""
    if not request.config.getoption("--runslow", default=False):
        pytest.skip("loads the cross-encoder; run with --runslow")
    import re
    from visualweaver import embeddings
    path = embeddings._local_snapshot("cross-encoder/ms-marco-MiniLM-L-6-v2")
    if not path:
        pytest.skip("cross-encoder/ms-marco-MiniLM-L-6-v2 is not in the local cache")
    from sentence_transformers import CrossEncoder
    paras = [p.strip() for p in re.split(r"\n\s*\n", (ROOT / "DOCS.md").read_text())]
    paras = [p for p in paras if len(p.split()) >= 12]
    cands, i = [], 0
    while len(cands) < 40 and i + 1 < len(paras):
        cands.append(paras[i] if i % 3 else paras[i] + "\n\n" + paras[i + 1])
        i += 3
    query = "how do I turn on hybrid search and the cross-encoder reranker"
    pairs = [(query, c) for c in cands]
    top5 = lambda s: list(np.argsort(-np.asarray(s, dtype=np.float64), kind="stable")[:5])
    import torch
    devices = ["cpu"] + (["mps"] if torch.backends.mps.is_available() else []) \
                      + (["cuda"] if torch.cuda.is_available() else [])
    for dev in devices:
        ce = CrossEncoder(path, device=dev)
        plain = ce.predict(pairs, show_progress_bar=False)
        cfg["reranker"] = {**(cfg.get("reranker") or {}), "half_precision": True}
        ce = embeddings._prepare_reranker(ce)
        assert getattr(ce, "_vw_half", False) is (dev != "cpu")
        assert top5(embeddings._predict_sorted(ce, pairs)) == top5(plain), dev


# ── 2. one embedding-model call per chat turn ───────────────────────────────────

class _StubEmbedModel:
    """Deterministic 384-wide unit vectors per text; counts encode() calls."""
    max_seq_length = 512

    def __init__(self):
        self.calls = []

    def get_sentence_embedding_dimension(self):
        return 384

    @staticmethod
    def vec(text):
        import hashlib
        rng = np.random.default_rng(int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big"))
        v = rng.standard_normal(384).astype(np.float32)
        return v / np.linalg.norm(v)

    def encode(self, sentences, **kw):
        single = isinstance(sentences, str)
        texts = [sentences] if single else list(sentences)
        self.calls.append(texts)
        out = np.stack([self.vec(t) for t in texts])
        return out[0] if single else out


class _StubSemanticCache:
    """The two redisvl SemanticCache calls cache.py makes. Without a vector it embeds the
    prompt itself through the same model, exactly as redisvl's vectorizer does."""

    def __init__(self, model, prefix):
        self.model, self.prefix = model, prefix
        self.checked, self.stored = [], []

    def _vec(self, prompt, vector):
        if vector is None:
            return list(self.model.encode([self.prefix + prompt])[0])
        return list(vector)

    def check(self, prompt=None, vector=None, num_results=1, filter_expression=None,
              distance_threshold=None, **kw):
        self.checked.append(self._vec(prompt, vector))
        return []

    def store(self, prompt, response, vector=None, metadata=None, filters=None, **kw):
        self.stored.append(self._vec(prompt, vector))
        return "k"


@pytest.fixture
def turn_rig(app_module, cfg, clean_redis, monkeypatch):
    """A real Redis-backed instance with three chunks, a stub embedding model, a stub
    answer cache and a stub LLM — the turn itself runs unpatched."""
    from conftest import REDIS_DB, REDIS_HOST, REDIS_PORT
    from visualweaver import cache, embeddings, providers, rag, redis_store, sessions, state
    from visualweaver import ws as _ns_ws
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-small"}
    cfg["reranker"] = {"enabled": False}
    cfg["hyde"] = {"enabled": False}
    cfg["rag"] = dict(cfg.get("rag", {}), hybrid_search=True, similarity_threshold=-1.0, top_k=3)
    redis_store.invalidate_redis_clients()
    model = _StubEmbedModel()
    monkeypatch.setattr(embeddings, "get_embed_model", lambda *a, **k: model)
    import uuid
    inst = f"{KEY_PREFIX}turn{uuid.uuid4().hex[:6]}"   # rag._index_ensured outlives one test
    rag.add_chunks(inst, [{"id": i, "text": t, "source": f"s{i}.md"} for i, t in enumerate(
        ["streams are append-only logs", "hashes store fields", "sets hold unique members"])])
    stub = _StubSemanticCache(model, embeddings.embedding_spec()["query_prefix"])
    monkeypatch.setattr(cache, "_get_semantic_cache", lambda: stub)
    monkeypatch.setattr(state, "_semantic_cache_ready", True)

    async def fake_stream(*a, **kw):
        for tok in ("grounded ", "answer [1]"):
            yield tok, False
        yield "", True

    monkeypatch.setattr(providers, "ollama_stream", fake_stream)
    monkeypatch.setattr(sessions, "save_session", lambda *a, **kw: None)
    monkeypatch.setattr(sessions, "record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(_ns_ws.mgr, "is_aborted", lambda sid: False, raising=False)
    model.calls.clear()
    yield types.SimpleNamespace(model=model, cache=stub, inst=inst)
    redis_store.invalidate_redis_clients()


def _assert_one_call_feeding_every_consumer(rig, query):
    from visualweaver import cache, embeddings
    prefix = embeddings.embedding_spec()["query_prefix"]
    assert len(rig.model.calls) == 1, \
        f"the query went through the model {len(rig.model.calls)} times: {rig.model.calls}"
    assert sorted(rig.model.calls[0]) == sorted({prefix + query, prefix + cache._normalize_query(query)})
    # The cache keeps its own text (casefolded and whitespace-collapsed, as calibrated):
    want_cache = _StubEmbedModel.vec(prefix + cache._normalize_query(query))
    assert len(rig.cache.checked) == 1 and len(rig.cache.stored) == 1
    for v in rig.cache.checked + rig.cache.stored:
        assert np.allclose(v, want_cache, atol=1e-6)


def test_a_chat_turn_embeds_the_query_once(turn_rig):
    """A cache miss sent the question through the embedding model three times: once for
    the cache lookup, once for retrieval, and once more to store the answer in the cache
    (redisvl embeds the prompt again inside store()). One batched call now produces both
    texts — the raw question for retrieval and the normalised one the cache is calibrated
    on — and every consumer reuses it."""
    import asyncio
    from visualweaver import routes_chat, state
    sent = []

    class _WS:
        async def send_json(self, payload):
            sent.append(payload)

    sid = f"{KEY_PREFIX}once"
    state._sessions[sid] = []
    query = "What about  Streams?"
    try:
        asyncio.run(routes_chat.handle_chat(_WS(), sid, {
            "content": query, "provider": "ollama", "model": "llama3",
            "rag_instances": [turn_rig.inst]}))
    finally:
        state._sessions.pop(sid, None)
    ctx = [m for m in sent if m.get("type") == "rag_context"]
    assert ctx and ctx[0]["chunks"], f"retrieval found nothing: {[m.get('type') for m in sent]}"
    assert any(m.get("type") == "stream_end" for m in sent)
    _assert_one_call_feeding_every_consumer(turn_rig, query)


def test_the_rest_chat_route_embeds_the_query_once(turn_rig):
    from fastapi.testclient import TestClient
    from visualweaver import appcore, state
    sid = f"{KEY_PREFIX}rest"
    query = "What about  Streams?"
    # Not entered as a context manager: that runs the startup hook, which reloads the
    # config from disk and would undo the rig.
    client = TestClient(appcore.app)
    r = client.post("/api/chat", json={"content": query, "session_id": sid,
                                       "provider": "ollama", "model": "llama3",
                                       "rag_instances": [turn_rig.inst]})
    state._sessions.pop(sid, None)
    assert r.status_code == 200, r.text
    assert r.json()["chunks"], r.json()
    _assert_one_call_feeding_every_consumer(turn_rig, query)


def test_several_instances_share_the_turns_single_embedding(turn_rig, cfg):
    """search_rag_parallel already embedded once for all instances — but separately from
    the cache lookup, so a multi-instance turn still cost three model calls."""
    import asyncio
    from visualweaver import rag, routes_chat, state
    cfg["rag"]["top_k"] = 4             # room for every chunk of both instances
    inst2 = turn_rig.inst + "b"
    rag.add_chunks(inst2, [{"id": 0, "text": "lists are ordered", "source": "l.md"}])
    turn_rig.model.calls.clear()
    sent = []

    class _WS:
        async def send_json(self, payload):
            sent.append(payload)

    sid = f"{KEY_PREFIX}multi"
    state._sessions[sid] = []
    query = "What about  Streams?"
    try:
        asyncio.run(routes_chat.handle_chat(_WS(), sid, {
            "content": query, "provider": "ollama", "model": "llama3",
            "rag_instances": [turn_rig.inst, inst2]}))
    finally:
        state._sessions.pop(sid, None)
    ctx = [m for m in sent if m.get("type") == "rag_context"]
    assert {c["instance"] for c in ctx[0]["chunks"]} == {turn_rig.inst, inst2}
    _assert_one_call_feeding_every_consumer(turn_rig, query)


# ── 3. instance listing reads a registry, not the keyspace ─────────────────────

@pytest.fixture
def listing_rig(app_module, cfg, clean_redis, monkeypatch):
    """The app on the test Redis, its registry under this run's prefix, and a recorder
    of every SCAN the client issues."""
    from conftest import REDIS_DB, REDIS_HOST, REDIS_PORT
    import redis as _redis
    from visualweaver import embeddings, instance_registry, rag, rag_admin, redis_store, state
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["redis_endpoints"] = []
    cfg["embedding"] = {"model": "intfloat/multilingual-e5-small"}
    redis_store.invalidate_redis_clients()
    rag_admin.invalidate_rag_meta()
    monkeypatch.setattr(instance_registry, "REDIS_KEY", clean_redis.key("rag_instances"))
    model = _StubEmbedModel()
    monkeypatch.setattr(embeddings, "get_embed_model", lambda *a, **k: model)
    monkeypatch.setitem(state._endpoint_health, "default", True)
    # A listing records the names it saw per connection; keep this test's out of the rest.
    monkeypatch.setattr(state, "_known_instance_names", {})
    scans = []
    real_scan = _redis.Redis.scan_iter

    def spy(self, match=None, *a, **kw):
        scans.append(match)
        return real_scan(self, match, *a, **kw)

    monkeypatch.setattr(_redis.Redis, "scan_iter", spy)
    import uuid
    tag = f"{KEY_PREFIX}L{uuid.uuid4().hex[:5]}"
    yield types.SimpleNamespace(rc=clean_redis, scans=scans, tag=tag)
    redis_store.invalidate_redis_clients()
    rag_admin.invalidate_rag_meta()


def _mine(rig, rows):
    return {r["name"]: r["chunks"] for r in rows if r["name"].startswith(rig.tag)}


def test_listing_instances_reads_the_registry_instead_of_scanning_the_keyspace(listing_rig):
    """GET /api/rag/instances walked the WHOLE keyspace with SCAN MATCH rag_meta:* on every
    call, to find instances that exist only as metadata: 52 ms p50 with 51 instances
    and 50,000 chunks, from ~100 SCAN round trips. After the registry exists, a listing
    issues no SCAN at all — and still shows the never-ingested instance."""
    import asyncio
    from visualweaver import rag, rag_admin, routes_instances
    rig = listing_rig
    empty, full = f"{rig.tag}empty", f"{rig.tag}full"
    asyncio.run(routes_instances.api_create_instance({"name": empty}))
    rag.add_chunks(full, [{"id": i, "text": f"text {i}", "source": "a.md"} for i in range(3)])
    rig.rc.set(f"rag_meta:{full}", json.dumps({"color": "#123456"}))   # written outside meta_set

    rig.scans.clear()
    rows = rag_admin.list_rag_instances()
    assert _mine(rig, rows) == {empty: 0, full: 3}, rows
    assert rig.scans == [], f"listing scanned the keyspace: {rig.scans}"
    assert next(r for r in rows if r["name"] == full)["color"] == "#123456"


def test_an_install_without_a_registry_builds_it_once_from_a_scan(listing_rig):
    """An install from before the registry has metadata keys and no registry: the first
    listing must find them (one scan), and the ones after it must not scan again."""
    from visualweaver import instance_registry, rag_admin
    rig = listing_rig
    legacy = f"{rig.tag}legacy"
    rig.rc.set(f"rag_meta:{legacy}", json.dumps({"color": "#6366f1"}))   # old version wrote it
    rig.rc.delete(instance_registry.REDIS_KEY)

    rig.scans.clear()
    assert _mine(rig, rag_admin.list_rag_instances()) == {legacy: 0}
    assert rig.scans == ["rag_meta:*"], rig.scans
    assert rig.rc.sismember(instance_registry.REDIS_KEY, instance_registry.BUILT)
    rig.scans.clear()
    assert _mine(rig, rag_admin.list_rag_instances()) == {legacy: 0}
    assert rig.scans == []


def test_the_registry_heals_and_follows_create_import_and_delete(listing_rig):
    """Every way an instance comes and goes keeps the registry exact: an index it missed
    is added, a name with neither metadata nor an index is dropped, and create, import,
    register-discovered and delete each write it."""
    import asyncio, io, zipfile
    from visualweaver import instance_registry, rag, rag_admin, routes_ingestion, routes_instances, routes_redis
    rig = listing_rig
    key = instance_registry.REDIS_KEY
    members = lambda: {m.decode() for m in rig.rc.smembers(key)}
    rag_admin.list_rag_instances()                               # registry exists

    ghost, orphan = f"{rig.tag}ghost", f"{rig.tag}orphan"
    rig.rc.sadd(key, ghost)                                      # nothing behind it
    rag.add_chunks(orphan, [{"id": 0, "text": "x", "source": "o.md"}])
    rig.rc.srem(key, orphan)                                     # an index the registry lost
    rows = _mine(rig, rag_admin.list_rag_instances())
    assert rows == {orphan: 1}, rows
    assert ghost not in members() and orphan in members()

    made = f"{rig.tag}made"
    asyncio.run(routes_instances.api_create_instance({"name": made}))
    assert made in members()
    routes_instances.api_delete_instance(made)
    assert made not in members()
    assert made not in _mine(rig, rag_admin.list_rag_instances())

    imported = f"{rig.tag}imported"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("meta.json", json.dumps({"color": "#6366f1"}))
        zf.writestr("chunks.json", json.dumps([]))

    class _Upload:
        filename = "x.zip"

        async def read(self):
            return buf.getvalue()

    asyncio.run(routes_ingestion.api_import_rag(imported, _Upload()))
    assert imported in members()

    found = f"{rig.tag}found"
    routes_redis.api_register_discovered("default", {"instances": [found]})
    assert found in members()
    assert {imported: 0, found: 0}.items() <= _mine(rig, rag_admin.list_rag_instances()).items()


# ── 4. MongoDB: the vector and keyword legs run at the same time ────────────────

def test_mongodb_vector_and_keyword_legs_overlap(monkeypatch):
    """search_legs ran the $vectorSearch aggregate, waited for it, then ran the $search
    one — two independent round trips back to back, so MongoDB retrieval took about
    twice what Redis did. Each stub leg below waits for the OTHER to have started: run
    one after the other, the first one waits in vain."""
    from visualweaver import mongo_store
    started = {"vector": threading.Event(), "text": threading.Event()}
    saw_other = {}

    def fake_aggregate(col, pipeline):
        leg = "vector" if "$vectorSearch" in pipeline[0] else "text"
        other = "text" if leg == "vector" else "vector"
        started[leg].set()
        saw_other[leg] = started[other].wait(timeout=3)
        if leg == "vector":
            return [{"_id": "i:1", "text": "a", "source": "s", "score": 0.9}]
        return [{"_id": "i:2", "text": "b", "source": "s", "score": 3.5}]

    class _EP:
        def col(self, name):
            return object()

    monkeypatch.setattr(mongo_store, "_aggregate", fake_aggregate)
    vec, txt = mongo_store.search_legs(_EP(), "i", np.ones(4, dtype=np.float32), ["b"], 5,
                                       "embedding_4", True)
    assert saw_other == {"vector": True, "text": True}, f"the legs ran one after the other: {saw_other}"
    assert [r["_key"] for r in vec] == ["i:1"] and vec[0]["_vec_score"] == 0.8
    assert [r["_key"] for r in txt] == ["i:2"] and txt[0]["_bm25"] == 3.5


def test_a_failing_mongodb_vector_leg_still_raises_its_own_error(monkeypatch):
    from visualweaver import mongo_store
    done = []

    def fake_aggregate(col, pipeline):
        if "$vectorSearch" in pipeline[0]:
            raise RuntimeError("vector leg failed")
        time.sleep(0.2)
        done.append("text")
        raise ValueError("text leg failed too")

    class _EP:
        def col(self, name):
            return object()

    monkeypatch.setattr(mongo_store, "_aggregate", fake_aggregate)
    with pytest.raises(RuntimeError, match="vector leg failed"):
        mongo_store.search_legs(_EP(), "i", np.ones(4), ["b"], 5, "embedding_4", True)
    assert done == ["text"], "the keyword leg was abandoned while still running"


def test_a_store_the_registry_cannot_be_written_to_is_still_listed(listing_rig, monkeypatch):
    """A read-only replica rejects SADD. The listing must still show its instances (from
    the scan, as before the registry) instead of the whole connection dropping out."""
    import redis as _redis
    from visualweaver import instance_registry, rag_admin
    rig = listing_rig
    legacy = f"{rig.tag}ro"
    rig.rc.set(f"rag_meta:{legacy}", json.dumps({"color": "#6366f1"}))
    rig.rc.delete(instance_registry.REDIS_KEY)

    def readonly(self, *a, **kw):
        raise _redis.exceptions.ReadOnlyError("You can't write against a read only replica.")

    monkeypatch.setattr(_redis.Redis, "sadd", readonly)
    monkeypatch.setattr(_redis.Redis, "srem", readonly)
    assert _mine(rig, rag_admin.list_rag_instances()) == {legacy: 0}
