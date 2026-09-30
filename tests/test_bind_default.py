# SPDX-License-Identifier: AGPL-3.0-or-later
"""The local runtime's bind address: all interfaces by default, with a guard.

There is no built-in authentication, so the decision to listen on 0.0.0.0 by
default comes with two promises, pinned here against the REAL scripts: every
start on all interfaces prints the LAN address and how to restrict it, and
VISUALWEAVER_HOST=127.0.0.1 (env or .visualweaver.env) restricts it.
"""
import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _host_for(tmp_path, env_line=None, real_env=None) -> str:
    """Run scripts/common.sh's override + APP_HOST logic and return APP_HOST."""
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    src = (_ROOT / "scripts" / "common.sh").read_text(encoding="utf-8")
    block = src[src.index("# ── Per-machine overrides"):src.index("APP_HOST=") ]
    block += re.search(r"^APP_HOST=.*$", src, re.M).group(0) + "\n"
    repo = tmp_path / "repo"; repo.mkdir()
    if env_line is not None:
        (repo / ".visualweaver.env").write_text(env_line + "\n")
    h = tmp_path / "h.sh"
    h.write_text(f'set -eu\nREPO_DIR="{repo}"\n' + block + 'printf "%s" "${APP_HOST}"\n')
    env = {"PATH": "/usr/bin:/bin"}
    env.update(real_env or {})
    r = subprocess.run(["bash", str(h)], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_default_is_all_interfaces(tmp_path):
    assert _host_for(tmp_path) == "0.0.0.0"


def test_env_var_restricts_to_loopback(tmp_path):
    assert _host_for(tmp_path, real_env={"VISUALWEAVER_HOST": "127.0.0.1"}) == "127.0.0.1"


def test_override_file_restricts_to_loopback(tmp_path):
    assert _host_for(tmp_path, env_line="VISUALWEAVER_HOST=127.0.0.1") == "127.0.0.1"


def test_a_real_env_var_wins_over_the_file(tmp_path):
    assert _host_for(tmp_path, env_line="VISUALWEAVER_HOST=127.0.0.1", real_env={"VISUALWEAVER_HOST": "0.0.0.0"}) == "0.0.0.0"


def test_start_banner_warns_and_says_how_to_restrict():
    """The guard that makes an open default acceptable: on all interfaces the
    banner must name the LAN reachability, the missing auth, and the exact
    restriction, on every start — not only when a LAN IP was detected."""
    s = (_ROOT / "start.sh").read_text(encoding="utf-8")
    guard = s[s.index("The guard for an unauthenticated service"):]
    assert "reachable from other devices on your network" in guard
    assert "NO built-in auth" in guard
    assert "VISUALWEAVER_HOST=127.0.0.1 ./start.sh" in guard
    assert ".visualweaver.env" in guard
    # and the loopback branch tells the user why nothing else can reach it
    assert "Only reachable from this machine" in guard


def test_docs_no_longer_promise_a_loopback_default():
    for doc in ("readme.md", "DOCS.md", "TUTORIAL.md"):
        s = (_ROOT / doc).read_text(encoding="utf-8")
        assert not re.search(r"binds to `127\.0\.0\.1`", s), f"{doc} still promises a loopback default"
        assert "VISUALWEAVER_HOST=127.0.0.1" in s, f"{doc} must say how to restrict the app"
