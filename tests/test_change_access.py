# SPDX-License-Identifier: AGPL-3.0-or-later
"""changeAccess.sh switches VISUALWEAVER_HOST in .visualweaver.env between
this-machine-only and the whole network, which scripts/common.sh then turns
into start.sh's bind address. Runs the REAL script in a throwaway repo copy.
"""
import os
import pathlib
import re
import shutil
import subprocess

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def repo(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    shutil.copy(_ROOT / "changeAccess.sh", r)
    shutil.copy(_ROOT / "scripts" / "common.sh", r / "scripts")
    (tmp_path / "data").mkdir()
    return r


def _run(repo, *args, env_host=None):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo.parent),
           "VISUALWEAVER_DATA_DIR": str(repo.parent / "data")}
    if env_host is not None:
        env["VISUALWEAVER_HOST"] = env_host
    p = subprocess.run(["bash", str(repo / "changeAccess.sh"), *args], env=env,
                       capture_output=True, text=True, timeout=30)
    return p.returncode, _ANSI.sub("", p.stdout + p.stderr)


def _env(repo):
    return (repo / ".visualweaver.env").read_text()


def _bind(repo):
    """The APP_HOST start.sh would bind, as computed by common.sh."""
    out = subprocess.run(["bash", "-c", '. scripts/common.sh; printf %s "$APP_HOST"'],
                         cwd=repo, env={"PATH": "/usr/bin:/bin", "HOME": str(repo.parent)},
                         capture_output=True, text=True, timeout=30).stdout
    return out


def test_local_creates_the_file_and_restricts_the_bind(repo):
    rc, out = _run(repo, "local")
    assert rc == 0 and "Access set to local" in out
    assert re.findall(r"(?m)^VISUALWEAVER_HOST=.*$", _env(repo)) == ["VISUALWEAVER_HOST=127.0.0.1"]
    assert _bind(repo) == "127.0.0.1"
    assert "WARNING" not in out


def test_lan_warns_and_opens_the_bind(repo):
    _run(repo, "local")
    rc, out = _run(repo, "LAN")
    assert rc == 0 and "Access set to lan" in out
    assert "NO built-in login" in out
    assert _bind(repo) == "0.0.0.0"


def test_other_settings_survive_and_duplicates_collapse(repo):
    f = repo / ".visualweaver.env"
    f.write_text('# keep me\nVISUALWEAVER_PORT=9000\n  VISUALWEAVER_HOST="0.0.0.0"\nPYTHON=/x/py\nVISUALWEAVER_HOST=0.0.0.0\n')
    f.chmod(0o644)  # mktemp makes 0600 — a rename would show
    assert _run(repo, "local")[0] == 0
    t = _env(repo)
    assert "# keep me" in t and "VISUALWEAVER_PORT=9000" in t and "PYTHON=/x/py" in t
    assert re.findall(r"(?m)^\s*VISUALWEAVER_HOST=.*$", t) == ["VISUALWEAVER_HOST=127.0.0.1"]
    assert (f.stat().st_mode & 0o777) == 0o644
    assert not [p for p in repo.iterdir() if p.name.startswith(".visualweaver.env.")]


def test_same_choice_again_changes_nothing(repo):
    _run(repo, "lan")
    before = _env(repo)
    rc, out = _run(repo, "lan")
    assert rc == 0 and "already set to lan" in out and _env(repo) == before


def test_help_flag(repo):
    rc, out = _run(repo, "-h")
    assert rc == 0 and "local" in out and "lan" in out and "NO" in out
    assert "-?" not in out
    assert not (repo / ".visualweaver.env").exists()


@pytest.mark.parametrize("args", [(), ("bogus",), ("local", "lan"), ("-?",), ("--help",)])
def test_bad_usage_exits_2_and_writes_nothing(repo, args):
    rc, out = _run(repo, *args)
    assert rc == 2 and "Usage:" in out
    assert not (repo / ".visualweaver.env").exists()


def test_no_args_reports_the_current_setting(repo):
    assert "the default" in _run(repo)[1]
    _run(repo, "local")
    assert "Current setting: local" in _run(repo)[1]


def test_exported_variable_that_overrides_the_file_is_flagged(repo):
    rc, out = _run(repo, "local", env_host="0.0.0.0")
    assert rc == 0 and "overrides the file" in out
    rc, out = _run(repo, "local", env_host="127.0.0.1")
    assert rc == 0 and "overrides the file" not in out


def test_running_app_is_told_to_restart_only_when_something_changed(repo):
    rc, out = _run(repo, "local")
    assert rc == 0 and "restart" not in out.lower()
    p = subprocess.Popen(["sleep", "60"])
    try:
        (repo.parent / "data" / "visualweaver.pid").write_text(str(p.pid))
        rc, out = _run(repo, "lan")
        assert rc == 0 and "./restart.sh" in out
        rc, out = _run(repo, "lan")                       # no change -> no restart hint
        assert rc == 0 and "nothing to change" in out and "restart" not in out.lower()
    finally:
        p.kill(); p.wait()


# Files written by hand. Whatever form the host line takes, after `local` the
# address start.sh binds (common.sh's reading) must be 127.0.0.1, and the file
# must hold exactly one plain host line.
@pytest.mark.parametrize("content", [
    "VISUALWEAVER_HOST=0.0.0.0\nVISUALWEAVER_HOST=127.0.0.1\n",   # first line wins in common.sh
    "VISUALWEAVER_HOST=127.0.0.1\nVISUALWEAVER_HOST=0.0.0.0\n",
    "VISUALWEAVER_HOST =0.0.0.0\n",
    "export VISUALWEAVER_HOST=0.0.0.0\n",
    "VISUALWEAVER_HOST=127.0.0.1\r\n",
    "VISUALWEAVER_HOST=127.0.0.1  # this machine\n",
    "VISUALWEAVER_PORT=9000\nVISUALWEAVER_HOST=0.0.0.0",           # no trailing newline
], ids=["dup-first-lan", "dup-first-local", "spaced-key", "export", "crlf", "inline-comment", "no-newline"])
def test_hand_written_host_lines_end_up_local(repo, content):
    (repo / ".visualweaver.env").write_bytes(content.encode())
    rc, out = _run(repo, "local")
    assert rc == 0, out
    t = _env(repo)
    assert re.findall(r"(?m)^.*VISUALWEAVER_HOST.*$", t) == ["VISUALWEAVER_HOST=127.0.0.1"]
    assert _bind(repo) == "127.0.0.1"
    if "PORT" in content:
        assert "VISUALWEAVER_PORT=9000" in t


def test_duplicate_lines_report_the_value_start_sh_uses(repo):
    (repo / ".visualweaver.env").write_text("VISUALWEAVER_HOST=0.0.0.0\nVISUALWEAVER_HOST=127.0.0.1\n")
    assert "Current setting: lan" in _run(repo)[1]
    assert _bind(repo) == "0.0.0.0"


@pytest.mark.parametrize("content,expected", [
    ("VISUALWEAVER_HOST=127.0.0.1", "127.0.0.1"),                 # last line, no newline
    ("VISUALWEAVER_HOST=127.0.0.1\r\n", "127.0.0.1"),
    ("export VISUALWEAVER_HOST=127.0.0.1\n", "127.0.0.1"),
    ('VISUALWEAVER_HOST="127.0.0.1"\n', "127.0.0.1"),
    ("NOT A NAME=1\nVISUALWEAVER_HOST=127.0.0.1\n", "127.0.0.1"),
])
def test_common_sh_reads_hand_written_files(repo, content, expected):
    (repo / ".visualweaver.env").write_bytes(content.encode())
    assert _bind(repo) == expected


def test_symlinked_env_file_is_written_through(repo, tmp_path):
    real = tmp_path / "real.env"
    real.write_text("PYTHON=/x/py\nVISUALWEAVER_HOST=0.0.0.0\n")
    (repo / ".visualweaver.env").symlink_to(real)
    assert _run(repo, "local")[0] == 0
    assert (repo / ".visualweaver.env").is_symlink()
    assert real.read_text() == "PYTHON=/x/py\nVISUALWEAVER_HOST=127.0.0.1\n"
    assert not [p for p in repo.iterdir() if p.name.startswith(".visualweaver.env.")]


def test_help_goes_to_stdout_and_usage_errors_to_stderr(repo):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo.parent), "VISUALWEAVER_DATA_DIR": str(repo.parent / "data")}
    ok = subprocess.run(["bash", str(repo / "changeAccess.sh"), "-h"], env=env, capture_output=True, text=True)
    bad = subprocess.run(["bash", str(repo / "changeAccess.sh"), "nope"], env=env, capture_output=True, text=True)
    assert "Usage:" in ok.stdout and ok.returncode == 0
    assert "Usage:" in bad.stderr and bad.stdout == "" and bad.returncode == 2


@pytest.mark.parametrize("shell", ["zsh", "sh"])
def test_started_by_another_shell_hands_over_to_bash(repo, shell):
    """macOS's login shell is zsh; `zsh changeAccess.sh` must still work."""
    if not shutil.which(shell):
        pytest.skip(f"{shell} not available")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo.parent), "VISUALWEAVER_DATA_DIR": str(repo.parent / "data")}
    p = subprocess.run([shell, str(repo / "changeAccess.sh"), "local"], env=env,
                       capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    assert _bind(repo) == "127.0.0.1"
    bad = subprocess.run([shell, str(repo / "changeAccess.sh"), "nope"], env=env,
                         capture_output=True, text=True, timeout=30)
    assert bad.returncode == 2 and "Usage:" in bad.stderr
