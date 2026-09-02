# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared fixtures.

Every test runs against a throwaway DATA_DIR and a dedicated Redis logical DB, so
a test run can never read or overwrite a real install's config, sessions or
vectors. The env var is set before ``visualweaver.main`` is imported because the
module resolves DATA_DIR at import time.
"""
import os
import re
import sys
import tempfile
from pathlib import Path

import pytest

_TMP_DATA = tempfile.mkdtemp(prefix="visualweaver-tests-")
os.environ["VISUALWEAVER_DATA_DIR"] = _TMP_DATA
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Redis used by the integration tests.
#
# RediSearch refuses FT.CREATE on any db but 0, so tests cannot be isolated onto
# their own logical database — they can only be isolated onto their own SERVER.
# By default this file starts a private throwaway one (see _boot_private_redis)
# and points the suite at it, so a run cannot reach the instance the app uses.
#
# The default used to be 6390 — the app's own port. Nothing in the suite ever
# issued a FLUSHDB, but pointing tests at a live instance leaves nothing between
# the corpus and one mistyped command at any other terminal, and on 2026-08-24
# exactly that removed a 57,805-chunk corpus.
#
# Set VISUALWEAVER_TEST_REDIS_PORT to override — CI does, because it supplies its
# own Redis service. Keys are still namespaced under a per-run prefix and only
# that prefix is deleted, never FLUSHDB, so an explicit override stays as safe as
# it used to be.
REDIS_HOST = os.environ.get("VISUALWEAVER_TEST_REDIS_HOST", "127.0.0.1")
REDIS_DB = 0
KEY_PREFIX = f"__vwtest_{os.getpid()}__"

_REDIS_PORT_ENV = os.environ.get("VISUALWEAVER_TEST_REDIS_PORT")
# Rebound by pytest_configure when a private server is started. Test modules read
# this at call time (`from conftest import REDIS_PORT` inside a test body), so the
# rebinding reaches them.
try:
    REDIS_PORT = int(_REDIS_PORT_ENV) if _REDIS_PORT_ENV else 0
except ValueError:                      # a typo must not be a collection error
    raise SystemExit(
        f"VISUALWEAVER_TEST_REDIS_PORT={_REDIS_PORT_ENV!r} is not a port number")
if _REDIS_PORT_ENV and not (1 <= REDIS_PORT <= 65535):
    # The range matters as much as the type: 0, -1 and 999999 were accepted, no
    # private server was started because the variable was set, and every Redis
    # test then skipped — a green run proving nothing, which is the outcome
    # VISUALWEAVER_TEST_REDIS_REQUIRED exists to prevent.
    raise SystemExit(
        f"VISUALWEAVER_TEST_REDIS_PORT={_REDIS_PORT_ENV!r} is out of range 1-65535")
_private_redis = None          # subprocess.Popen of the throwaway server
_private_dir = None
_boot_reason = None            # why no private server, for the report header
_boot_dbsize = None            # key count the moment it came up; must be 0


def _free_port(tries: int = 5) -> int:
    """A port the kernel just handed out on the loopback.

    Inherently a race — it is closed before redis binds it — and losing it costs
    more than it looks: the boot fails, and ~38 Redis tests quietly drop to skip
    rather than anything failing. Retried, because the only realistic trigger is
    two sessions starting together and a second draw almost never collides.
    """
    import socket
    last = 0
    for _ in range(tries):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            last = s.getsockname()[1]
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", last))
            except OSError:
                continue
        return last
    # Every attempt lost the race. Returning `last` would hand back the port whose
    # probe just failed; ask the kernel once more and let the caller's own bind
    # report the failure honestly.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sweep_stale_test_redis_dirs(older_than_hours: int = 6) -> None:
    """Remove leftovers from runs that never reached pytest_unconfigure.

    Nothing makes a child redis die with its parent portably, so a SIGKILL to
    pytest strands both the server and its directory. The prefix is distinctive
    and each directory is a few KB, so sweeping old ones on the way in keeps this
    self-healing rather than accumulating.
    """
    import shutil
    import time

    cutoff = time.time() - older_than_hours * 3600
    tmp = Path(tempfile.gettempdir())
    # Both leak the same way. The data dirs had no teardown at all before this,
    # and 56 of them had accumulated.
    for pattern in ("visualweaver-test-redis-*", "visualweaver-tests-*"):
        for d in tmp.glob(pattern):
            try:
                if not d.is_dir():
                    continue
                # The DIRECTORY's mtime does not advance while redis appends to a
                # log already inside it, so a long run's own directory would look
                # stale. Take the newest mtime among its immediate children —
                # one level, so a data dir whose only churn is inside a
                # subdirectory can still read as old.
                newest = d.stat().st_mtime
                for child in d.iterdir():
                    newest = max(newest, child.stat().st_mtime)
                if newest < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def _boot_private_redis():
    """Start a throwaway redis-server with RediSearch, on its own port and dir.

    Returns the port, or None when no usable server could be started — in which
    case the fixtures skip, exactly as they already did when nothing was
    reachable. `_boot_reason` carries the explanation for the report header.

    RediSearch is verified by running FT._LIST rather than by looking for the
    module file: a plain system redis (brew, apt) starts perfectly well without
    it, and every FT.CREATE in the suite would then hard-error instead of
    skipping — which is worse than not booting at all.
    """
    global _private_redis, _private_dir, _boot_reason
    import shutil
    import socket
    import subprocess
    import time

    _sweep_stale_test_redis_dirs()

    root = Path(__file__).resolve().parents[1]
    # install.sh resolves the runtime and publishes it in .redis/redis-env.sh,
    # which start.sh already sources. Read the same file rather than hard-coding
    # the layout a second time — if install.sh ever relocates the tree, the tests
    # would otherwise fall silently back to a system redis, or to nothing.
    env_file = root / ".redis" / "redis-env.sh"
    resolved = {}
    if env_file.exists():
        for line in env_file.read_text(errors="replace").split("\n"):
            m = re.match(r'\s*(REDIS_\w+)="?([^"]*)"?\s*$', line)
            if m:
                resolved[m.group(1)] = m.group(2)
    server = Path(resolved.get("REDIS_SERVER")
                  or root / ".redis" / "redis-oss" / "bin" / "redis-server")
    module = Path(resolved.get("REDIS_MODULE_SEARCH")
                  or root / ".redis" / "redis-oss" / "lib" / "redis" / "modules" / "redisearch.so")
    vendor_dir = resolved.get("REDIS_DYLD_FALLBACK")
    if not server.exists():
        # redis-stack-server first: where both exist, the plain redis-server is
        # often the module-less binary, and conftest only knows how to add
        # --loadmodule from the vendored tree.
        found = shutil.which("redis-stack-server") or shutil.which("redis-server")
        if not found:
            _boot_reason = "no redis-server on PATH and none vendored in .redis/"
            return None
        server = Path(found)

    port = _free_port()
    workdir = tempfile.mkdtemp(prefix="visualweaver-test-redis-")
    logfile = Path(workdir) / "redis.log"
    proc = None

    def _give_up(reason):
        """Never leave the directory or the process behind on a failure path."""
        global _boot_reason
        import shutil as _sh
        _boot_reason = reason
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        _sh.rmtree(workdir, ignore_errors=True)
        return None

    cmd = [str(server), "--port", str(port),
           # 127.0.0.1 only. A no-auth server with RediSearch loaded must not be
           # reachable from the network for the length of a test run; the app's
           # own redis.conf binds the loopback too.
           "--bind", "127.0.0.1",
           "--dir", workdir, "--save", "", "--appendonly", "no",
           "--daemonize", "no", "--logfile", str(logfile)]
    if module.exists():
        cmd += ["--loadmodule", str(module)]

    env = dict(os.environ)
    vendor = Path(vendor_dir) if vendor_dir else root / ".redis" / "vendor-lib"
    if vendor.exists():
        # The vendored module links a bundled libunwind. Both names are set so
        # this works on macOS and on Linux.
        env["DYLD_FALLBACK_LIBRARY_PATH"] = str(vendor)
        env["LD_LIBRARY_PATH"] = os.pathsep.join(
            [str(vendor)] + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else []))

    try:
        proc = subprocess.Popen(cmd, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        return _give_up(f"could not start {server}: {exc}")

    for _ in range(100):                       # up to ~10s
        if proc.poll() is not None:
            tail = ""
            try:
                tail = logfile.read_text(errors="replace")[-300:].strip().replace("\n", " | ")
            except OSError:
                pass
            return _give_up(f"redis exited during startup: {tail or 'no log'}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:
        return _give_up("redis did not accept connections within 10s")

    # RediSearch is not optional for this suite.
    try:
        import redis as _redis
        _probe = _redis.Redis(host="127.0.0.1", port=port, socket_connect_timeout=5)
        try:
            _probe.execute_command("FT._LIST")
        finally:
            # An unclosed client per boot is how an FD-exhaustion incident starts,
            # and this project has already had one.
            _probe.close()
    except Exception as exc:
        return _give_up(f"server has no RediSearch ({type(exc).__name__}); "
                        "install redis-stack or run install.sh to vendor it")

    global _boot_dbsize
    try:
        import redis as _r
        _probe = _r.Redis(host="127.0.0.1", port=port)
        try:
            _boot_dbsize = _probe.dbsize()
        finally:
            # This project has already had one FD-exhaustion incident; a probe
            # client that is never closed is exactly how that starts.
            _probe.close()
    except Exception:                                           # pragma: no cover
        _boot_dbsize = None

    _private_redis, _private_dir = proc, workdir
    _boot_reason = None
    return port


def pytest_addoption(parser):
    parser.addoption(
        "--runslow", action="store_true", default=False,
        help="run tests that download embedding models (cache-threshold calibration)")


def pytest_configure(config):
    # Start the private server before anything collects or connects. Skipped when
    # the port was pinned explicitly (CI supplies its own Redis).
    global REDIS_PORT
    if not _REDIS_PORT_ENV:
        port = _boot_private_redis()
        if port:
            REDIS_PORT = port
        elif os.environ.get("VISUALWEAVER_TEST_REDIS_REQUIRED") == "1":
            # Every Redis guard skips when no server starts, and pytest exits 0
            # on an all-skipped run — so CI would go green having proved nothing.
            pytest.exit(
                "VISUALWEAVER_TEST_REDIS_REQUIRED=1 but no private Redis could "
                f"be started: {_boot_reason}", returncode=1)

    config.addinivalue_line(
        "markers",
        "network: reaches the public internet; runs only with VISUALWEAVER_TEST_NETWORK=1",
    )
    config.addinivalue_line(
        "markers",
        "slow: downloads embedding models; runs only with --runslow",
    )


def pytest_unconfigure(config):
    """Stop the throwaway server and remove its directory."""
    global _private_redis, _private_dir
    if _private_redis is not None:
        try:
            _private_redis.terminate()
            _private_redis.wait(timeout=10)
        except Exception:                                       # pragma: no cover
            try:
                _private_redis.kill()
            except Exception:
                pass
        _private_redis = None
    if _private_dir:
        import shutil
        shutil.rmtree(_private_dir, ignore_errors=True)
        _private_dir = None
    # The throwaway DATA_DIR has never been removed either; 45 of them had piled
    # up before this hook existed. Cleaning the redis directory and leaving its
    # neighbour would be odd.
    import shutil
    shutil.rmtree(_TMP_DATA, ignore_errors=True)


def pytest_report_header(config):
    """Say which Redis the run is using — the difference between a private server
    and someone's live one should never be silent."""
    if _REDIS_PORT_ENV:
        return f"redis: {REDIS_HOST}:{REDIS_PORT} db{REDIS_DB} (pinned by env)"
    if _private_redis is not None:
        return f"redis: private throwaway server on port {REDIS_PORT}"
    return f"redis: none started ({_boot_reason or 'unknown'}) — integration tests will skip"


@pytest.fixture(scope="session")
def app_module():
    """The imported application module, pointed at the test data dir."""
    import visualweaver.main as m
    return m


@pytest.fixture
def cfg(app_module):
    """Restore the global config after any test that mutates it."""
    import copy
    original = copy.deepcopy(app_module._config)
    yield app_module._config
    app_module._config.clear()
    app_module._config.update(original)


@pytest.fixture(scope="session")
def redis_client():
    """A client on the test DB, or skip the test when no server is reachable."""
    import redis
    rc = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
    try:
        rc.ping()
    except Exception as e:                                    # pragma: no cover
        pytest.skip(f"no Redis at {REDIS_HOST}:{REDIS_PORT} ({e})")
    return rc


def _purge(rc):
    """Delete only this run's namespaced keys and indexes.

    Matches the prefix ANYWHERE in the name, not just at the start. A test that builds a
    RAG instance called ``<prefix>kept`` produces keys named ``rag:<prefix>kept:chunk:1``
    and an index called ``rag:<prefix>kept:idx`` — both begin with ``rag:``, so a
    startswith test matched neither and every run left its indexes and chunks behind in
    the shared database. Containment is still exact enough to be safe: the prefix carries
    the pid and cannot occur in real data.
    """
    for pattern in (f"{KEY_PREFIX}*", f"*{KEY_PREFIX}*"):
        for key in rc.scan_iter(pattern, count=500):
            rc.delete(key)
    try:
        for name in rc.execute_command("FT._LIST"):
            name = name.decode() if isinstance(name, bytes) else name
            if KEY_PREFIX in name:
                rc.execute_command("FT.DROPINDEX", name)
    except Exception:
        pass


@pytest.fixture
def clean_redis(redis_client):
    """A client plus a namespace. NEVER flushes — db 0 holds the user's real data.

    Tests must build keys with the ``key(...)`` helper so cleanup can find them.
    """
    _purge(redis_client)
    redis_client.key = lambda suffix: f"{KEY_PREFIX}{suffix}"
    yield redis_client
    _purge(redis_client)


@pytest.fixture
def data_dir():
    """An isolated directory for tests that write config or session files."""
    with tempfile.TemporaryDirectory(prefix="visualweaver-case-") as d:
        yield Path(d)
