# SPDX-License-Identifier: AGPL-3.0-or-later
"""The suite's own cleanup, which had no coverage and therefore leaked for months.

Tests run on their own throwaway server (conftest boots one), but they still
share db 0 within it — RediSearch refuses FT.CREATE on any
other db — so they namespace every key under a per-pid prefix and delete only that.
The delete matched the prefix only at the START of a name, and a test that builds a
RAG instance called ``<prefix>kept`` produces ``rag:<prefix>kept:chunk:1`` and an
index ``rag:<prefix>kept:idx``. Both begin with ``rag:``, so neither was matched:
every run left its instances, chunks and indexes behind in the live database.

These assert on real Redis state, because the defect was invisible to every other
kind of check — the suite was fully green while it accumulated 38 stray instances.
"""
import pytest

from conftest import KEY_PREFIX, _purge


@pytest.fixture
def rc(redis_client):
    _purge(redis_client)
    yield redis_client
    _purge(redis_client)


def _names(rc):
    try:
        return [n.decode() if isinstance(n, bytes) else n
                for n in rc.execute_command("FT._LIST")]
    except Exception:                                    # pragma: no cover
        pytest.skip("RediSearch not available")


def test_purge_removes_keys_that_only_contain_the_prefix(rc):
    """The shape the tests actually create: the namespace is in the MIDDLE."""
    rc.set(f"rag_meta:{KEY_PREFIX}kept", "{}")
    rc.set(f"rag:{KEY_PREFIX}kept:chunk:1", "x")
    rc.set(f"{KEY_PREFIX}plain", "x")
    _purge(rc)
    assert rc.exists(f"rag_meta:{KEY_PREFIX}kept") == 0
    assert rc.exists(f"rag:{KEY_PREFIX}kept:chunk:1") == 0
    assert rc.exists(f"{KEY_PREFIX}plain") == 0


def test_purge_drops_indexes_whose_name_only_contains_the_prefix(rc):
    """`rag:<prefix>kept:idx` begins with "rag:", so a startswith test never saw it —
    which is exactly how the leftover indexes accumulated."""
    idx = f"rag:{KEY_PREFIX}dropme:idx"
    rc.execute_command("FT.CREATE", idx, "PREFIX", "1",
                       f"rag:{KEY_PREFIX}dropme:chunk:", "SCHEMA", "text", "TEXT")
    assert idx in _names(rc)
    _purge(rc)
    assert idx not in _names(rc), "the index survived cleanup"


def test_purge_leaves_everything_outside_the_namespace_alone(rc):
    """The prefix carries the pid, but the blast radius still has to be checked: this
    could run against a shared database whenever the port is overridden."""
    guard = "rag_meta:NotATestInstance"
    pre_existing = rc.exists(guard)
    if not pre_existing:
        rc.set(guard, "keep me")
    try:
        rc.set(f"rag_meta:{KEY_PREFIX}kept", "{}")
        _purge(rc)
        assert rc.exists(guard) == 1, "cleanup deleted a key outside its namespace"
    finally:
        if not pre_existing:
            rc.delete(guard)


def test_a_real_looking_index_is_not_dropped(rc):
    """FT.DROPINDEX on an index the purge does not own would take out a real
    instance's search — the risk that remains whenever the port is overridden."""
    idx = f"rag:{KEY_PREFIX}scoped:idx"
    keep = "rag:NotATestInstance:idx"
    existed = keep in _names(rc)
    if not existed:
        rc.execute_command("FT.CREATE", keep, "PREFIX", "1",
                           "rag:NotATestInstance:chunk:", "SCHEMA", "text", "TEXT")
    try:
        rc.execute_command("FT.CREATE", idx, "PREFIX", "1",
                           f"rag:{KEY_PREFIX}scoped:chunk:", "SCHEMA", "text", "TEXT")
        _purge(rc)
        names = _names(rc)
        assert idx not in names
        assert keep in names, "cleanup dropped an index outside its namespace"
    finally:
        if not existed:
            try:
                rc.execute_command("FT.DROPINDEX", keep)
            except Exception:
                pass


def test_no_test_hardcodes_the_key_prefix():
    """Every test must build namespaced keys from conftest.KEY_PREFIX.

    Six sites in test_usage_tracking.py duplicated the literal instead. The
    purge deletes only what matches the CURRENT prefix, so any rename would have
    left those tests writing keys nothing cleaned up — and they land in db 0,
    which holds the user's own data.
    """
    import pathlib
    import re

    here = pathlib.Path(__file__).resolve().parent
    offenders = []
    for f in sorted(here.glob("*.py")):
        if f.name in ("conftest.py", __file__.rsplit("/", 1)[-1]):
            continue
        text = f.read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r'__\w*test_\{?\s*os\.getpid', line):
                offenders.append(f"{f.name}:{n}: {line.strip()}")
    assert not offenders, (
        "these build a test key prefix by hand instead of importing "
        "conftest.KEY_PREFIX:\n  " + "\n  ".join(offenders))


def test_the_suite_does_not_default_to_the_app_s_own_redis():
    """Without an explicit override the run must be on its own server.

    RediSearch refuses FT.CREATE outside db 0, so tests cannot be isolated by
    database — only by server. The default used to be 6390, the port the app
    listens on, which left nothing between a live corpus and one mistyped command
    at any terminal. conftest now boots a private throwaway instance instead.
    """
    import conftest

    if conftest._REDIS_PORT_ENV:
        pytest.skip("port pinned by VISUALWEAVER_TEST_REDIS_PORT (CI supplies its own)")
    # Never conditional: whatever happened, the suite must not be on the app's port.
    assert conftest.REDIS_PORT != 6390, \
        "the suite is pointed at the port the application uses"
    if conftest._private_redis is None:
        # A bare checkout with no redis-server is a supported state — the promise
        # is that those tests SKIP, not that they fail, and pytest_report_header
        # says why. Note the assertion above is vacuous in this state (REDIS_PORT
        # is 0 when no server booted); the real guard is
        # test_the_suite_never_lands_on_a_port_the_application_could_be_using.
        pytest.skip(f"no private Redis could be started ({conftest._boot_reason})")


def test_the_private_redis_came_up_empty():
    """A throwaway server holds nothing before this run puts something there.

    The earlier version of this test listed indexes and asserted every one
    carried the test prefix — on a fresh server FT._LIST is empty, so the loop
    body never ran and it asserted nothing at all. It also could not fail for the
    reason its name gave: a server full of a real corpus's KEYS but holding no
    index would have passed.

    conftest records DBSIZE the instant the server accepts connections, which is
    the only moment the claim is checkable.
    """
    import conftest

    if conftest._REDIS_PORT_ENV:
        pytest.skip("port pinned by env; the server is not ours to characterise")
    if conftest._private_redis is None:
        pytest.skip(f"no private Redis was started ({conftest._boot_reason})")
    assert conftest._boot_dbsize == 0, (
        f"the test server came up holding {conftest._boot_dbsize} keys — "
        "it is not a throwaway instance")


def test_the_suite_never_lands_on_a_port_the_application_could_be_using():
    """Behavioural rather than textual.

    All three of these skip when VISUALWEAVER_TEST_REDIS_PORT is set, and the
    pinned CI job sets it; what arms them in CI is the second workflow step,
    which runs with no pin. This one is the useful assertion of the three
    because it compares against the ports the application actually uses.

    Checking for the literal 6390 was too narrow in one direction and too wide in
    the other: a regression to 6379 (the app's own configured default, and where
    most real Redis instances listen) would have passed, while a docstring line
    documenting the override would have failed it. Compare against the ports the
    application actually uses instead.
    """
    import conftest

    from visualweaver import constants, state

    if conftest._REDIS_PORT_ENV:
        pytest.skip("port pinned by env; the operator chose the target")
    if conftest._private_redis is None:
        pytest.skip(f"no private Redis could be started ({conftest._boot_reason})")

    app_ports = {6390, 6379}
    default_redis = (constants.DEFAULT_CONFIG.get("redis") or {}).get("port")
    if isinstance(default_redis, int):
        app_ports.add(default_redis)
    configured = (state._config.get("redis") or {}).get("port") if isinstance(
        state._config.get("redis"), dict) else None
    if isinstance(configured, int):
        app_ports.add(configured)

    assert conftest.REDIS_PORT not in app_ports, (
        f"the suite is on port {conftest.REDIS_PORT}, which the application "
        f"uses or defaults to ({sorted(app_ports)})")
