# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model loads come from the local cache first and happen once.

After a restart with DNS down, the embedding model was loaded BY REPO NAME, which
asks the Hub whether every cached file is current — five retries with backoff per
file — and a request thread that needed the model started a SECOND load beside
the warmup's. The page sat blank for over a minute. Both fixed in
``embeddings._load_model`` / the shared ``_load_lock``; pinned here with fakes, so
no model is loaded and no network is touched.
"""
import sys
import threading
import time
import types

import pytest

from visualweaver import embeddings, state


class _Fake:
    """Records what it was constructed with; optionally fails on a given arg."""
    calls: list = []
    fail_on: set = set()
    delay = 0.0

    def __init__(self, arg, *a, **kw):
        _Fake.calls.append(arg)
        if _Fake.delay:
            time.sleep(_Fake.delay)
        if arg in _Fake.fail_on:
            raise OSError(f"cannot load {arg}")
        self.arg = arg
        self.max_seq_length = 512

    def get_sentence_embedding_dimension(self):
        return 8


@pytest.fixture
def loader(monkeypatch):
    _Fake.calls = []
    _Fake.fail_on = set()
    _Fake.delay = 0.0
    saved = (state._embed_model, state._embed_model_name, state._reranker, state._reranker_model_name)
    state._embed_model = state._embed_model_name = None
    state._reranker = state._reranker_model_name = None
    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = _Fake
    fake_mod.CrossEncoder = _Fake
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)
    monkeypatch.setattr(embeddings, "CrossEncoder", _Fake, raising=False)
    monkeypatch.setattr(embeddings, "HAS_CROSSENCODER", True, raising=False)
    monkeypatch.setattr(embeddings, "_warn_if_chunk_size_exceeds_model", lambda m, n: None)
    monkeypatch.setitem(state._config, "embedding", {"model": "org/cached-model"})
    monkeypatch.setitem(state._config, "reranker", {"enabled": True, "model": "org/cached-reranker"})
    snapshots = {"org/cached-model": "/cache/snap-embed", "org/cached-reranker": "/cache/snap-rerank"}
    monkeypatch.setattr(embeddings, "_local_snapshot", lambda repo: snapshots.get(repo))
    yield snapshots
    (state._embed_model, state._embed_model_name, state._reranker, state._reranker_model_name) = saved


def test_a_cached_model_is_loaded_from_its_snapshot_directory_not_by_name(loader):
    m = embeddings.get_embed_model()
    assert _Fake.calls == ["/cache/snap-embed"], _Fake.calls
    assert m.arg == "/cache/snap-embed" and state._embed_model_name == "org/cached-model"
    assert embeddings.get_embed_model() is m and _Fake.calls == ["/cache/snap-embed"], "a second call must not reload"


def test_an_uncached_model_is_fetched_by_name(loader):
    m = embeddings.get_embed_model("org/new-model")
    assert _Fake.calls == ["org/new-model"] and m.arg == "org/new-model"


def test_a_broken_cached_copy_falls_back_to_the_hub(loader):
    _Fake.fail_on = {"/cache/snap-embed"}
    m = embeddings.get_embed_model()
    assert _Fake.calls == ["/cache/snap-embed", "org/cached-model"], _Fake.calls
    assert m.arg == "org/cached-model"


def test_concurrent_callers_load_the_model_once(loader):
    """The warmup thread and a request thread both needing the model must share one
    load: the log after the restart showed two full loads side by side."""
    _Fake.delay = 0.25
    got = []
    threads = [threading.Thread(target=lambda: got.append(embeddings.get_embed_model())) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5)
    assert len(_Fake.calls) == 1, f"the model was constructed {len(_Fake.calls)} times"
    assert len(got) == 4 and all(g is got[0] for g in got)


def test_the_reranker_takes_the_same_cached_path_and_single_flight(loader):
    _Fake.delay = 0.2
    got = []
    threads = [threading.Thread(target=lambda: got.append(embeddings.get_reranker())) for _ in range(3)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=5)
    assert _Fake.calls == ["/cache/snap-rerank"], _Fake.calls
    assert len(got) == 3 and all(g is got[0] for g in got) and got[0] is not None


def test_local_snapshot_never_touches_the_network(monkeypatch, tmp_path):
    """A directory path is returned as is; a repo that is not cached comes back None
    at once (huggingface_hub is asked with local_files_only) — no retries, no DNS."""
    assert embeddings._local_snapshot(str(tmp_path)) == str(tmp_path)
    t0 = time.time()
    assert embeddings._local_snapshot("org/definitely-not-cached-anywhere") is None
    assert time.time() - t0 < 2.0
