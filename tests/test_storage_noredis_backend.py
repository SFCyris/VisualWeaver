# SPDX-License-Identifier: AGPL-3.0-or-later
"""A MongoDB-only installation: the app boots and runs with no Redis at all.

The app is booted through its real startup (TestClient runs it) from a config.json
whose built-in "default" connection is the throwaway MongoDB (storage.default_type =
"mongodb") and whose Redis section points at a closed port. Every Redis connection
attempt in the process is recorded; there must be none, the answer cache included.
"""
import copy
import io
import json
import os
import socket
import time

import pytest

from conftest import KEY_PREFIX


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def mongo_only(app_module, cfg, mongo_uri, monkeypatch, tmp_path):
    import redis
    from visualweaver import constants, mongo_store, rag_admin, redis_store, state
    for v in ("VISUALWEAVER_STORAGE", "VISUALWEAVER_MONGODB_URI", "VISUALWEAVER_REDIS_HOST",
              "VISUALWEAVER_REDIS_PORT"):
        monkeypatch.delenv(v, raising=False)
    uri, user, pw = mongo_store.split_uri_credentials(mongo_uri)
    db = f"vwtest_nr_{os.getpid()}_{int(time.time() * 1000) % 10**8}"
    conf = {
        "storage": {"default_type": "mongodb", "app": "default"},
        "mongodb": {"type": "mongodb", "uri": uri, "username": user, "password": pw,
                    "database": db, "builtin": True},
        "redis": {"host": "127.0.0.1", "port": _closed_port(), "db": 0, "password": "", "ssl": False},
        "redis_endpoints": [],
        "sessions": {"persist": True, "ttl": 3600},
        # The model the rest of the suite uses, so nothing new is downloaded.
        "embedding": copy.deepcopy(state._config.get("embedding") or constants.DEFAULT_CONFIG["embedding"]),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(conf))
    monkeypatch.setattr(constants, "CONFIG_PATH", path)

    attempts: list[str] = []
    real_connect = redis.connection.AbstractConnection.connect

    def _recording_connect(self, *a, **k):
        import traceback
        attempts.append("".join(traceback.format_stack(limit=12)))
        return real_connect(self, *a, **k)
    monkeypatch.setattr(redis.connection.AbstractConnection, "connect", _recording_connect)

    saved = (copy.deepcopy(state._rag_stats), dict(state._endpoint_health), dict(state._search_available),
             set(state._rag_stats_dirty), dict(state._sessions))
    monkeypatch.setattr(rag_admin, "_stats_loaded", False)
    state._endpoint_health.clear()
    state._search_available.clear()
    redis_store.invalidate_redis_clients()
    rag_admin.invalidate_rag_meta()
    yield {"db": db, "password": pw, "attempts": attempts, "redis_port": conf["redis"]["port"]}
    try:
        from pymongo import MongoClient
        c = MongoClient(mongo_uri, directConnection=True, serverSelectionTimeoutMS=5000)
        c.drop_database(db)
        c.close()
    finally:
        state._rag_stats.clear(); state._rag_stats.update(saved[0])
        state._endpoint_health.clear(); state._endpoint_health.update(saved[1])
        state._search_available.clear(); state._search_available.update(saved[2])
        state._rag_stats_dirty.clear(); state._rag_stats_dirty.update(saved[3])
        state._sessions.clear(); state._sessions.update(saved[4])
        redis_store.invalidate_redis_clients()
        rag_admin.invalidate_rag_meta()


def _wait(pred, timeout=60.0, step=0.25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return pred()


DOC = ("The pressure relief valve on the QX-7731 boiler opens at 3 bar. Replace the valve "
       "every five years; the older QX-7700 part is discontinued.")


def test_the_app_runs_on_mongodb_with_no_redis(mongo_only):
    from fastapi.testclient import TestClient
    from visualweaver import appcore, appstore, rag, rag_admin, redis_store, sessions, state, ws

    inst = f"{KEY_PREFIX}nored"
    with TestClient(appcore.app) as client:
        # ── boot ───────────────────────────────────────────────────────────
        assert redis_store.default_type() == "mongodb"
        assert _wait(lambda: rag_admin._stats_loaded), "background init never restored the stats"
        assert state._endpoint_health.get("default") is True
        assert appstore.current().kind == "mongodb"
        assert client.get("/api/health").json()["status"] == "ok"

        # ── status / monitor ───────────────────────────────────────────────
        st = client.get("/api/status/redis").json()
        assert (st["ok"], st["type"], st["search_available"]) == (True, "mongodb", True), st
        assert st["version"] == "9.0.4" and st["endpoint_health"] == {"default": True}
        assert client.get("/api/status/redis/default").json()["search_available"] is True
        mem = client.get("/api/redis/memory").json()
        assert mem["type"] == "mongodb" and isinstance(mem["used"], int) and "error" not in mem, mem
        allst = client.get("/api/redis/all-stats").json()
        assert [(s["name"], s.get("type"), s["ok"], s["builtin"]) for s in allst] == \
            [("default", "mongodb", True, True)], allst
        eps = client.get("/api/redis/endpoints").json()
        assert [(e["name"], e["type"], e["builtin"]) for e in eps] == [("default", "mongodb", True)]
        storage = client.get("/api/storage").json()
        assert storage["default_type"] == "mongodb" and storage["app"] == "default"
        d = storage["connections"][0]
        assert (d["label"], d["ok"], d["vector_search"], d["is_app"], d["version"]) == \
            ("Built-in MongoDB", True, True, True, "9.0.4"), d
        comps = client.get("/api/components").json()
        assert comps["storage"][0]["search"] == "MongoDB Search 1.78.0"
        exported = client.get("/api/config/export").text
        assert mongo_only["password"] not in exported

        # ── a RAG instance on the default: create, ingest, search ─────────
        r = client.post("/api/rag/instances", json={"name": inst})
        assert r.status_code == 200, r.text
        assert r.json()["redis_endpoint"] == "default"
        r = client.post(f"/api/rag/{inst}/ingest/text", json={"text": DOC, "source": "boiler.txt"})
        assert r.status_code == 200 and r.json().get("chunks", 0) >= 1, r.text
        listed = {i["name"]: i for i in client.get("/api/rag/instances").json()}
        assert listed[inst]["endpoint_type"] == "mongodb" and listed[inst]["chunks"] >= 1
        hits = _wait(lambda: rag.search_rag(inst, "When does the relief valve open?", top_k=3,
                                            threshold=0.0, rc=rag_admin.rc_for_instance(inst)), timeout=60)
        assert hits and "3 bar" in hits[0]["text"], hits
        docs = client.get(f"/api/rag/{inst}/documents").json()
        assert [x["source"] for x in docs["documents"]] == ["boiler.txt"], docs
        z = client.get(f"/api/rag/{inst}/export")
        assert z.status_code == 200 and len(z.content) > 100
        r = client.post(f"/api/rag/{inst}copy/import",
                        files={"file": ("e.zip", io.BytesIO(z.content), "application/zip")})
        assert r.status_code == 200, r.text
        assert {i["name"] for i in client.get("/api/rag/instances").json()} >= {inst, f"{inst}copy"}

        # ── conversations ──────────────────────────────────────────────────
        sid = f"{KEY_PREFIX}conv"
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello there"}]
        sessions.save_session(sid, msgs)
        assert any(s["id"] == sid and s["preview"] == "hello there" for s in client.get("/api/sessions").json())
        state._sessions.pop(sid, None)
        assert client.get(f"/api/sessions/{sid}").json() == msgs
        assert 3500 <= appstore.current().ttl(sessions._session_key(sid)) <= 3600
        assert client.delete(f"/api/sessions/{sid}").json() == {"ok": True}
        assert sessions.load_session(sid) == []

        # ── usage tally ────────────────────────────────────────────────────
        client.delete("/api/usage")
        sessions.record_usage("ollama", "qwen2.5:7b", {"prompt": 30, "completion": 7, "cached": 2})
        sessions.record_usage("ollama", "qwen2.5:7b", {"prompt": 10, "completion": 3})
        assert client.get("/api/usage").json() == {"ollama:qwen2.5:7b": {"in": 40, "out": 10, "cached": 2}}

        # ── retrieval stats persist and load ───────────────────────────────
        row = dict(state.RAG_STATS_TEMPLATE)
        row.update(queries=9, hits=9, scored=9, raw_hist=[0] * state.RAW_HIST_BUCKETS,
                   cited_hist=[0] * state.CITED_HIST_RANKS)
        row["raw_hist"][4] = 9
        state._rag_stats[inst] = row
        state._rag_stats_dirty.add(inst)
        rag_admin.save_rag_stats()
        state._rag_stats.pop(inst)
        rag_admin.load_rag_stats()
        assert state._rag_stats[inst]["queries"] == 9

        # ── watched-folder signatures ──────────────────────────────────────
        from pathlib import Path
        root = Path("/tmp/vw-watch-root")
        a, b = root / "a.txt", root / "b.txt"
        appstore.current().hset(ws._WATCH_SEEN_KEY, {f"{inst}\x00{a}": "1:1", f"{inst}\x00{b}": "2:2"})
        seen, stale = ws._watch_seen_sync(inst, [(a, "1:1")], root)     # b.txt is gone
        assert seen[f"{inst}\x00{a}"] == "1:1" and stale == [f"{inst}\x00{b}"]
        assert appstore.current().hgetall(ws._WATCH_SEEN_KEY) == {f"{inst}\x00{a}": "1:1"}

        # ── answer cache on the MongoDB built-in ───────────────────────────
        from visualweaver import cache
        assert _wait(lambda: state._semantic_cache_ready, timeout=120), "the cache never warmed up"
        q, scope = "At what pressure does the QX-7731 relief valve open?", f"{inst}-scope"
        cache.cache_store(q, "At 3 bar.", scope=scope)
        hit = _wait(lambda: cache.cache_lookup(q, threshold=0.9, scope=scope), timeout=60, step=0.5)
        assert hit and hit["response"] == "At 3 bar.", hit
        assert getattr(state._semantic_cache, "kind", "") == "mongodb"
        assert cache.cache_lookup(q, threshold=0.9, scope=f"{inst}-other") is None
        stats = client.get("/api/cache/stats").json()
        assert stats["count"] >= 1 and any(e["query"] for e in stats["entries"]), stats

        # ── delete ─────────────────────────────────────────────────────────
        for name in (inst, f"{inst}copy"):
            assert client.delete(f"/api/rag/instances/{name}").json() == {"ok": True}
        assert inst not in {i["name"] for i in client.get("/api/rag/instances").json()}

    # Nothing — the answer cache included — tries Redis on a MongoDB-only install.
    assert mongo_only["attempts"] == [], "\n----\n".join(mongo_only["attempts"][:2])
    # The recorder itself works: a real Redis call is seen.
    import redis
    before = len(mongo_only["attempts"])
    with pytest.raises(redis.exceptions.ConnectionError):
        redis.Redis(port=mongo_only["redis_port"], socket_connect_timeout=1).ping()
    assert len(mongo_only["attempts"]) > before
