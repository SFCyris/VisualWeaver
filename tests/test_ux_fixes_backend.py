# SPDX-License-Identifier: AGPL-3.0-or-later
"""Backend half of the usability fixes: a second connection to the same Redis server must
not list the first connection's instances as its own.

RediSearch indexes are server-wide — FT._LIST answers the same on every logical database —
so a connection to db 1 of the built-in server used to list every built-in instance a
second time, and the Remove-connection dialog named instances that connection did not hold.
"""
import json

import pytest

from conftest import KEY_PREFIX, REDIS_DB


@pytest.fixture
def second_db(app_module, cfg, clean_redis):
    import conftest
    from visualweaver import rag_admin, redis_store, state
    import redis
    host, port = conftest.REDIS_HOST, conftest.REDIS_PORT
    db1 = redis.Redis(host=host, port=port, db=1)
    inst = f"{KEY_PREFIX}phantom"
    only1 = f"{KEY_PREFIX}onlyindb1"
    cfg["redis"] = {"host": host, "port": port, "db": REDIS_DB, "password": "", "ssl": False}
    cfg["storage"] = {"default_type": "redis", "app": "default"}
    cfg["redis_endpoints"] = [{"name": "samehost-db1", "type": "redis", "host": host, "port": port, "db": 1},
                              # the same server and database under another name for the host
                              {"name": "alias-db0", "type": "redis", "host": "localhost" if host == "127.0.0.1" else host,
                               "port": port, "db": REDIS_DB}]
    saved = dict(state._endpoint_health)
    state._endpoint_health.pop("default", None)
    state._endpoint_health.pop("samehost-db1", None)
    state._endpoint_health.pop("alias-db0", None)
    redis_store.invalidate_redis_clients()
    rag_admin.invalidate_rag_meta()
    clean_redis.execute_command("FT.CREATE", f"rag:{inst}:idx", "ON", "HASH", "PREFIX", "1",
                                f"rag:{inst}:chunk:", "SCHEMA", "text", "TEXT")
    clean_redis.set(f"rag_meta:{inst}", json.dumps({"color": "#123456", "redis_endpoint": "default"}))
    clean_redis.hset(f"rag:{inst}:chunk:1", mapping={"text": "hello"})
    # an instance whose metadata lives in db 1 only (created there, never indexed)
    db1.set(f"rag_meta:{only1}", json.dumps({"color": "#654321", "redis_endpoint": "samehost-db1"}))
    try:
        yield {"inst": inst, "only1": only1}
    finally:
        db1.delete(f"rag_meta:{only1}")
        db1.close()
        state._endpoint_health.clear()
        state._endpoint_health.update(saved)
        redis_store.invalidate_redis_clients()
        rag_admin.invalidate_rag_meta()


def test_an_index_is_listed_once_not_once_per_database_of_its_server(second_db):
    """Neither another database of the server nor another name for the same server and
    database lists the instance again — not even as a zero-chunk copy from its metadata."""
    from visualweaver import rag_admin
    rows = [r for r in rag_admin.list_rag_instances() if KEY_PREFIX in r["name"]]
    phantom = [r for r in rows if r["name"] == second_db["inst"]]
    assert len(phantom) == 1, rows
    assert phantom[0]["redis_endpoint"] == "default" and phantom[0]["chunks"] == 1


def test_metadata_kept_in_the_other_database_is_still_listed_there(second_db):
    from visualweaver import rag_admin
    rows = [r for r in rag_admin.list_rag_instances() if r["name"] == second_db["only1"]]
    assert [r["redis_endpoint"] for r in rows] == ["samehost-db1"]
