# SPDX-License-Identifier: AGPL-3.0-or-later
"""install.sh chooses a Python the pinned wheels exist for.

A fresh Homebrew/apt hands out Python 3.13/3.14, for which the pinned PyMuPDF
has no wheel; the installer used to build its venv on whatever `python3` was
first on PATH and then spend minutes compiling MuPDF from source before failing
in zlib on the macOS SDK. These tests run the REAL interpreter-selection block
of install.sh against fake interpreters on a scratch PATH.
"""
import os
import pathlib
import re
import shutil
import subprocess
import textwrap

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INSTALL = _ROOT / "install.sh"


def _block() -> str:
    t = _INSTALL.read_text(encoding="utf-8")
    start = t.index("# ── 1. Python virtual environment")
    end = t.index('c_info "Installing Python dependencies…"')
    return t[start:end]


def _fake_python(path: pathlib.Path, minor: int) -> None:
    path.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        case "$*" in
          *version_info*) echo {minor};;
          *"-m venv"*) mkdir -p "$3/bin"; cp "$0" "$3/bin/python";;
        esac
        """))
    path.chmod(0o755)


def _run(tmp_path, bins: dict, *, env=None, os_name="Linux", stale_venv_minor=None):
    if not shutil.which("bash"):
        pytest.skip("bash not available")
    bindir = tmp_path / "bin"; bindir.mkdir(exist_ok=True)
    for name, minor in bins.items():
        _fake_python(bindir / name, minor)
    repo = tmp_path / "repo"; repo.mkdir(exist_ok=True)
    if stale_venv_minor is not None:
        (repo / "venv" / "bin").mkdir(parents=True)
        _fake_python(repo / "venv" / "bin" / "python", stale_venv_minor)
    harness = tmp_path / "h.sh"
    harness.write_text(
        'set -euo pipefail\nc_info(){ echo "INFO $*"; }; c_err(){ echo "ERR $*"; }; c_warn(){ echo "WARN $*"; }\n'
        f'OS="{os_name}"; REPO_DIR="{repo}"\n' + _block() +
        'echo "CHOSEN=${SYS_PY}"; echo "VENV_MINOR=$(grep -o \'echo [0-9]*\' "${VENV}/bin/python" | cut -d" " -f2)"\n')
    # An ISOLATED PATH: only the fake interpreters plus the handful of coreutils
    # the block needs. Including /usr/bin leaked the runner's real python3.12 on
    # CI, so "only an unsupported Python" cases found a supported one and passed.
    tools = tmp_path / "tools"; tools.mkdir(exist_ok=True)
    for name in ("seq", "grep", "cut", "mkdir", "cp", "rm", "cat", "sh", "bash"):
        real = shutil.which(name)
        if real and not (tools / name).exists():
            os.symlink(real, tools / name)
    e = {"PATH": f"{bindir}:{tools}", "HOME": str(tmp_path)}
    e.update(env or {})
    r = subprocess.run(["bash", str(harness)], capture_output=True, text=True, env=e, timeout=60)
    return r.returncode, r.stdout + r.stderr, repo


def test_a_supported_python_is_preferred_over_an_unsupported_bare_python3(tmp_path):
    rc, out, _ = _run(tmp_path, {"python3": 14, "python3.12": 12})
    assert rc == 0, out
    assert re.search(r"CHOSEN=.*/python3\.12$", out, re.M), out
    assert "VENV_MINOR=12" in out, "the venv was not built on the chosen interpreter"


def test_only_an_unsupported_python_fails_fast_with_the_install_command(tmp_path):
    rc, out, repo = _run(tmp_path, {"python3": 14})
    assert rc != 0
    assert "Python 3.14" in out and "3.11–3.12" in out
    assert "PyMuPDF" in out and "compile MuPDF from source" in out, "the message must say WHY"
    assert "apt-get install -y python3.12" in out
    assert "PYTHON=/path/to/python3.12" in out
    assert not (repo / "venv").exists(), "no venv may be created on an unsupported Python"


def test_the_macos_hint_names_homebrew(tmp_path):
    rc, out, _ = _run(tmp_path, {"python3": 14}, os_name="Darwin")
    assert rc != 0 and "brew install python@3.12" in out, out


def test_an_explicit_python_override_is_honoured(tmp_path):
    rc, out, _ = _run(tmp_path, {"python3": 14, "python3.12": 12}, env={"PYTHON": str(tmp_path / "bin" / "python3.12")})
    assert rc == 0 and "VENV_MINOR=12" in out, out


def test_an_explicit_override_to_an_unsupported_python_is_rejected(tmp_path):
    rc, out, repo = _run(tmp_path, {"python3": 14, "python3.12": 12}, env={"PYTHON": str(tmp_path / "bin" / "python3")})
    assert rc != 0 and "is Python 3.14" in out, out
    assert not (repo / "venv").exists()


def test_a_venv_left_on_an_unsupported_python_is_rebuilt(tmp_path):
    """The source-build failure leaves a half-made venv on 3.14; reusing it
    would repeat the failure on the next run."""
    rc, out, _ = _run(tmp_path, {"python3": 14, "python3.12": 12}, stale_venv_minor=14)
    assert rc == 0, out
    assert "WARN" in out and "Python 3.14" in out and "recreating" in out
    assert "VENV_MINOR=12" in out


def test_a_venv_on_a_supported_python_is_kept(tmp_path):
    rc, out, _ = _run(tmp_path, {"python3": 14, "python3.12": 12}, stale_venv_minor=11)
    assert rc == 0 and "WARN" not in out and "VENV_MINOR=11" in out, out


def test_the_supported_window_matches_the_pinned_pymupdf_wheels():
    """The window in install.sh, requires-python and the docs must agree, and
    must match what the pinned PyMuPDF actually ships wheels for (3.8–3.12 at
    1.24.3). Bumping PyMuPDF to an abi3 release is the cue to widen all three."""
    t = _INSTALL.read_text(encoding="utf-8")
    lo = int(re.search(r"^PY_MIN_MINOR=(\d+)", t, re.M).group(1))
    hi = int(re.search(r"^PY_MAX_MINOR=(\d+)", t, re.M).group(1))
    assert (lo, hi) == (11, 12)
    py = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'requires-python\s*=\s*">=3\.11,<3\.13"', py), "requires-python must cap at <3.13 to match"
    assert "PyMuPDF==1.24.3" in py
    for doc in ("readme.md", "DOCS.md"):
        s = (_ROOT / doc).read_text(encoding="utf-8")
        assert "Python 3.11+" not in s, f"{doc} still promises 3.11+ which the pin cannot honour"
        assert "3.11 or 3.12" in s or "3.11 – 3.12" in s, doc
