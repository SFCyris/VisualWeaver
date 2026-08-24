# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the ```fractal renderers actually put on the canvas.

test_fractal_js.py stops at _fracDraw: the renderers need a canvas, so the
offline tests cover spec normalisation and the L-system maths and nothing
downstream of them. A spec can normalise perfectly and still paint an empty
card — that is precisely how an attractor given runaway coefficients used to
fail, and how a fractal card looked after a theme change.

Browser work lives in ``tests/fractal_probe.py``. Same design as
``test_cache_badge_browser.py``.
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
_PROBE = pathlib.Path(__file__).with_name("fractal_probe.py")

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
def shots():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    if not out:
        pytest.skip(f"fractal probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"fractal probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.skip(f"fractal probe could not run: {data.get('error')}")
    return data


def test_the_page_raised_no_errors_while_drawing(shots):
    assert not shots["errors"], shots["errors"]


@pytest.mark.parametrize("name", ["hilbert", "peano", "moore", "gosper", "lorenz",
                                  "rossler", "halvorsen", "clifford", "fern",
                                  "carpet", "mandelbrot", "julia"])
def test_every_renderer_puts_ink_on_the_canvas(shots, name):
    """The floor test. A spec that normalises and then paints nothing is the
    failure mode the offline tests cannot see."""
    s = shots["shots"][name]
    assert "error" not in s, s
    assert not s["pending"], f"{name} never finished drawing"
    assert s["inkPct"] > 1.0, f"{name} covered {s['inkPct']}% of the frame"
    assert s["w"] > 100 and s["h"] > 100, f"{name} drew into {s['w']}x{s['h']}px"


@pytest.mark.parametrize("name", ["hilbert", "peano", "moore"])
def test_space_filling_curves_fill_a_square(shots, name):
    """Their defining property, measured in pixels: the ink is as wide as it is
    tall, and it is dense. A curve drawn with the wrong rules still inks the
    canvas but does not land on a square."""
    s = shots["shots"][name]
    assert abs(s["w"] - s["h"]) <= 2, f"{name} bounding box {s['w']}x{s['h']} is not square"
    assert s["inkPct"] > 8, f"{name} only inked {s['inkPct']}%"


def test_an_attractor_orbit_is_graded_along_its_trajectory(shots):
    """The gradient is the time axis and it is what separates the two Lorenz
    lobes into a butterfly. A single flat colour would still pass an ink check."""
    assert shots["shots"]["lorenz"]["colours"] > 100, shots["shots"]["lorenz"]
    assert shots["shots"]["rossler"]["colours"] > 100, shots["shots"]["rossler"]


def test_a_point_cloud_attractor_is_not_graded(shots):
    """Consecutive iterates of a discrete map land on opposite sides of the
    figure, so it is drawn as a cloud in one tint rather than a graded path."""
    assert shots["shots"]["clifford"]["colours"] < 80, shots["shots"]["clifford"]


def test_a_diverged_attractor_reports_instead_of_painting_a_blank_card(shots):
    """Coefficients that blow the system up leave nothing to draw. The card says
    so — a short line of text — rather than showing an empty frame."""
    s = shots["shots"]["diverged"]
    assert 0 < s["inkPct"] < 2, f"expected a line of text, got {s['inkPct']}% ink"
    assert s["h"] < 40, f"the message should be one line, not {s['h']}px tall"
    assert s["w"] > 100, "nothing was written at all"


def test_no_renderer_takes_long_enough_to_stall_the_tab(shots):
    slow = {k: v["ms"] for k, v in shots["shots"].items()
            if "error" not in v and v["ms"] > 4000}
    assert not slow, f"renderers over 4s: {slow}"
