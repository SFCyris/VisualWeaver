# SPDX-License-Identifier: AGPL-3.0-or-later
"""A conversation keeps its title and its place in the list across a reload: the title
is stored with the conversation and the list is sorted by the last turn, not by the
store's arbitrary key order."""
import json

from conftest import KEY_PREFIX, REDIS_DB, REDIS_HOST, REDIS_PORT


def _turns(q, a, ts, title=None):
    user = {"role": "user", "content": q, "meta": {"ts": ts}}
    if title:
        user["meta"]["title"] = title
    return [user, {"role": "assistant", "content": a, "meta": {"ts": ts + 1}}]


def test_titles_and_last_turn_order_survive_a_reload(app_module, cfg, clean_redis):
    from visualweaver import appstore, redis_store, routes_misc, sessions, state
    cfg["redis"] = {"host": REDIS_HOST, "port": REDIS_PORT, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["storage"] = {"default_type": "redis", "app": "default"}
    redis_store.invalidate_redis_clients()
    p = f"{KEY_PREFIX}t"
    stored = {f"{p}old": _turns("q1", "a1", 1_000, "Boiler valve pressure"),
              f"{p}new": _turns("q2", "a2", 3_000) + _turns("q3", "a3", 3_100),
              f"{p}mid": _turns("q4", "a4", 2_000, "Cache settings")}
    for sid, msgs in stored.items():
        appstore.current().set(f"session:{sid}", json.dumps(msgs), ttl=600)
    try:
        rows = [r for r in routes_misc.api_sessions() if r["id"].startswith(p)]
        assert [r["id"] for r in rows] == [f"{p}new", f"{p}mid", f"{p}old"], "newest first"
        by = {r["id"]: r for r in rows}
        assert by[f"{p}old"]["title"] == "Boiler valve pressure" and by[f"{p}old"]["updated"] == 1_001
        assert by[f"{p}new"]["title"] is None and by[f"{p}new"]["preview"] == "a3"
        # a conversation open in memory is merged into the same order
        state._sessions[f"{p}live"] = _turns("q5", "a5", 2_500, "Live one")
        rows = [r["id"] for r in routes_misc.api_sessions() if r["id"].startswith(p)]
        assert rows == [f"{p}new", f"{p}live", f"{p}mid", f"{p}old"]
        # set_title stores the title on the first user turn, which save_session persists
        state._sessions[f"{p}live2"] = _turns("q6", "a6", 2_600)
        assert sessions.set_title(f"{p}live2", "Generated title")
        sessions.save_session(f"{p}live2", state._sessions.pop(f"{p}live2"))
        row = next(r for r in routes_misc.api_sessions() if r["id"] == f"{p}live2")
        assert row["title"] == "Generated title"
    finally:
        for sid in list(stored) + [f"{p}live", f"{p}live2"]:
            state._sessions.pop(sid, None)
            appstore.current().delete(f"session:{sid}")
