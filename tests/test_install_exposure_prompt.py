# SPDX-License-Identifier: AGPL-3.0-or-later
"""install.sh asks, once, whether the web UI is for this machine or the LAN.

There is no built-in authentication, so the choice is made consciously at
install time and persisted in the gitignored .visualweaver.env. These tests run
the REAL prompt block of install.sh with a pseudo-terminal for the interactive
cases and a closed stdin for the non-interactive one.
"""
import os
import pathlib
import pty
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL = _ROOT / "install.sh"


def _block() -> str:
    t = _INSTALL.read_text(encoding="utf-8")
    return t[t.index("# ── 6. Network exposure"):t.index('c_ok "══ Install complete ══"')]


def _harness(tmp_path, existing=None) -> tuple:
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    if existing is not None:
        (repo / ".visualweaver.env").write_text(existing)
    h = tmp_path / "h.sh"
    h.write_text('set -euo pipefail\nc_info(){ echo "INFO $*"; }; c_ok(){ echo "OK $*"; }; c_warn(){ echo "WARN $*"; }\n'
                 f'REPO_DIR="{repo}"; APP_HOST="0.0.0.0"; APP_PORT=8420\n' + _block() + 'echo "APP_HOST=${APP_HOST}"\n')
    return h, repo


def _run_tty(tmp_path, answer: str, existing=None):
    """Run under a pty so `[ -t 0 ]` is true and `read </dev/tty` sees the answer."""
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    h, repo = _harness(tmp_path, existing)
    pid, fd = pty.fork()
    if pid == 0:  # child
        os.execvpe("bash", ["bash", str(h)], {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    out = b""
    try:
        if answer is not None:
            os.write(fd, (answer + "\n").encode())
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    finally:
        os.waitpid(pid, 0)
    return out.decode(errors="replace"), repo


def _run_no_tty(tmp_path, existing=None):
    h, repo = _harness(tmp_path, existing)
    r = subprocess.run(["bash", str(h)], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout, repo


def _host_in(repo) -> str:
    return re.search(r"^VISUALWEAVER_HOST=(\S+)", (repo / ".visualweaver.env").read_text(), re.M).group(1)


def test_choosing_lan_persists_all_interfaces(tmp_path):
    out, repo = _run_tty(tmp_path, "2")
    assert "APP_HOST=0.0.0.0" in out and _host_in(repo) == "0.0.0.0"
    assert "reachable from your network" in out


def test_choosing_local_persists_loopback(tmp_path):
    out, repo = _run_tty(tmp_path, "1")
    assert "APP_HOST=127.0.0.1" in out and _host_in(repo) == "127.0.0.1"


def test_just_pressing_enter_is_the_safe_choice(tmp_path):
    """No auth: the default answer must be the restrictive one."""
    out, repo = _run_tty(tmp_path, "")
    assert "APP_HOST=127.0.0.1" in out and _host_in(repo) == "127.0.0.1"


def test_a_recorded_choice_is_respected_and_not_asked_again(tmp_path):
    out, repo = _run_tty(tmp_path, "2", existing="VISUALWEAVER_HOST=127.0.0.1\n")
    assert "already set in .visualweaver.env" in out
    assert "Choose [1/2]" not in out, "must not re-prompt when a choice is recorded"
    assert "APP_HOST=127.0.0.1" in out and _host_in(repo) == "127.0.0.1"


def test_non_interactive_runs_are_not_asked_and_keep_the_default(tmp_path):
    out, repo = _run_no_tty(tmp_path)
    assert "No terminal" in out and "APP_HOST=0.0.0.0" in out
    assert not (repo / ".visualweaver.env").exists(), "nothing must be written without a decision"


def test_the_prompt_states_the_missing_auth_and_both_options(tmp_path):
    out, _ = _run_tty(tmp_path, "1")
    assert "NO built-in authentication" in out
    assert "Only this machine" in out and "Any device on my network" in out and "trusted networks only" in out
