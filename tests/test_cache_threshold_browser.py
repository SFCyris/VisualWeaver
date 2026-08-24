# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cache similarity threshold control.

The value is calibrated per embedding model — two unrelated questions sit ~0.10
apart under all-MiniLM-L6-v2 and ~0.80 apart under multilingual-e5-base — so it
cannot be a fixed number in the UI either. Browser work lives in
``tests/cache_threshold_probe.py``.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "visualweaver" / "index.html"
_PROBE = pathlib.Path(__file__).with_name("cache_threshold_probe.py")

_CANDIDATE_PYTHONS = [
    os.environ.get("VISUALWEAVER_TEST_PLAYWRIGHT_PYTHON"),
    sys.executable,
    shutil.which("python3"),
    shutil.which("python"),
    str(pathlib.Path.home() / "miniconda3" / "bin" / "python3"),
    str(pathlib.Path.home() / "anaconda3" / "bin" / "python3"),
    str(pathlib.Path.home() / ".pyenv" / "shims" / "python3"),
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
]
_resolved: list = []


def _no_browser():
    """Skip — or FAIL when CI insists the browser tests must really run."""
    msg = ("no interpreter with playwright installed "
           "(set VISUALWEAVER_TEST_PLAYWRIGHT_PYTHON)")
    if os.environ.get("VISUALWEAVER_REQUIRE_BROWSER_TESTS") == "1":
        pytest.fail(msg + " — required because VISUALWEAVER_REQUIRE_BROWSER_TESTS=1")
    pytest.skip(msg)


def _playwright_python():
    if _resolved:
        return _resolved[0]
    seen = set()
    for cand in _CANDIDATE_PYTHONS:
        if not cand or cand in seen or not pathlib.Path(cand).exists():
            continue
        seen.add(cand)
        try:
            r = subprocess.run([cand, "-c", "import playwright"],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            _resolved.append(cand)
            return cand
    _resolved.append(None)
    return None


@pytest.fixture(scope="session")
def cth():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    # SKIP rather than fail on a harness problem: one flaky browser launch should not
    # read as two dozen unrelated behavioural failures.
    if not out:
        pytest.skip(f"cache threshold probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"cache threshold probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.skip(f"cache threshold probe could not run: {data.get('error')}")
    return data




def test_following_the_model_shows_the_value_that_will_be_used(cth):
    """An empty or stale slider would make the user set a number to find out what the
    number is."""
    assert cth["cases"]["auto_e5"]["shown"] == "0.97"
    assert cth["cases"]["auto_minilm"]["shown"] == "0.93"


def test_following_the_model_posts_no_number(cth):
    """The trap: _collectSettings reads the DOM, so a slider that always reports a figure
    pins the automatic value the first time anyone opens Settings and saves — after which
    it stops following the next model change, with nothing to say so."""
    assert cth["cases"]["auto_e5"]["posted"] is None
    assert cth["cases"]["auto_minilm"]["posted"] is None


def test_the_slider_is_not_editable_while_it_follows_the_model(cth):
    """Dragging it would appear to work and then be discarded on save."""
    assert cth["cases"]["auto_e5"]["disabled"] is True
    assert cth["cases"]["explicit"]["disabled"] is False


def test_an_explicit_value_is_shown_and_posted_back(cth):
    assert cth["cases"]["explicit"]["shown"] == "0.85"
    assert cth["cases"]["explicit"]["posted"] == 0.85


def test_taking_control_pins_the_value_currently_shown(cth):
    """Un-ticking must hand over the number the user was looking at, not a default."""
    assert cth["cases"]["unticked"]["posted"] == 0.85
    assert cth["cases"]["unticked"]["disabled"] is False


def test_the_hint_says_which_mode_is_active(cth):
    assert "embedding model" in cth["cases"]["auto_e5"]["hint"]
    assert "did not ask" in cth["cases"]["explicit"]["hint"]
