# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stopped Redis is reported as down quickly.

The health probe pinged through the app's redis.Redis client, which retries a refused
connection three times with backoff (~5.7 s per call), and /api/status/redis then asked
the dead server again for INFO, so the status poll took 8–16 s to say "down" and the
chat's status strip lagged ~40 s behind a stopped server.
"""
import socket
import time


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _point_default_at(cfg, port):
    from visualweaver import state
    cfg["redis"] = {"host": "127.0.0.1", "port": port, "db": 0}
    cfg.setdefault("storage", {})["default_type"] = "redis"
    cfg["redis_endpoints"] = []
    state._redis_clients.clear()
    if hasattr(state, "_redis"):
        state._redis = None


def test_a_stopped_redis_is_reported_down_within_a_second(cfg):
    from visualweaver import redis_store, routes_settings, state
    _point_default_at(cfg, _closed_port())
    try:
        t = time.monotonic()
        out = routes_settings.api_redis_status()
        took = time.monotonic() - t
        assert out["ok"] is False
        assert out["endpoint_health"]["default"] is False
        assert "refused" in out["error"].lower() or "connect" in out["error"].lower(), out
        assert took < 1.0, f"status took {took:.2f} s for a stopped Redis"
    finally:
        state._redis_clients.clear()


def test_the_probe_still_sees_a_live_redis(cfg, redis_client):
    from visualweaver import redis_store, state
    kw = redis_client.connection_pool.connection_kwargs
    _point_default_at(cfg, kw["port"])
    try:
        assert redis_store._probe_endpoint("default") is True
        assert state._endpoint_health["default"] is True
    finally:
        state._redis_clients.clear()


def _count_connects(monkeypatch):
    """Every TCP connect redis-py attempts, refused at once: the count is the retries."""
    import redis.connection
    calls = []

    def refuse(self, *a, **k):
        calls.append(1)
        raise ConnectionRefusedError(61, "Connection refused")
    monkeypatch.setattr(redis.connection.Connection, "_connect", refuse)
    return calls


def test_the_component_probe_tries_a_dead_redis_once(monkeypatch):
    from visualweaver import routes_storage
    calls = _count_connects(monkeypatch)
    out = routes_storage.component_info("dead", {"host": "127.0.0.1", "port": _closed_port(),
                                                 "type": "redis"}, max_age=0)
    assert out["ok"] is False
    assert len(calls) == 1, f"{len(calls)} connection attempts for one probe"


def test_the_storage_probe_tries_a_dead_redis_once(cfg, monkeypatch):
    from visualweaver import routes_storage, state
    _point_default_at(cfg, _closed_port())
    cfg["redis_endpoints"] = [{"name": "dead", "type": "redis", "host": "127.0.0.1",
                               "port": _closed_port(), "db": 0}]
    calls = _count_connects(monkeypatch)
    try:
        ok, vs, err = routes_storage._storage_probe("dead", cfg["redis_endpoints"][0])
        assert ok is False and vs is None and err
        assert len(calls) == 1, f"{len(calls)} connection attempts for one probe"
    finally:
        state._redis_clients.clear()


def test_the_status_poll_tries_a_dead_redis_once(cfg, monkeypatch):
    """One connection attempt for the whole /api/status/redis poll: the health probe
    without retries, and no second request to a server it just found dead."""
    from visualweaver import routes_settings, state
    _point_default_at(cfg, _closed_port())
    calls = _count_connects(monkeypatch)
    try:
        out = routes_settings.api_redis_status()
        assert out["ok"] is False
        assert len(calls) == 1, f"{len(calls)} connection attempts for one status poll"
    finally:
        state._redis_clients.clear()
