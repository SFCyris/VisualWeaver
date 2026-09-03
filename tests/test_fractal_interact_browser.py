# SPDX-License-Identifier: AGPL-3.0-or-later
"""Box-zoom and the iteration/depth control, driven in a real browser.

test_fractal_zoom_js.py pins the box-zoom geometry offline. This file drives
``tests/fractal_interact_probe.py`` through Playwright and asserts on what the
card actually did: the zoom overlay's ratio, the view after a drag, the Reset
button, and each family's depth control re-rendering in place.

Same design as ``tests/test_fractal_browser.py``.
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
_PROBE = pathlib.Path(__file__).with_name("fractal_interact_probe.py")

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
ESCAPE = ["mandelbrot", "julia"]
ALL = ["mandelbrot", "julia", "lsystem", "ifs_chaos", "ifs_depth", "attractor"]


def _no_browser():
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
def probe():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    if not out:
        pytest.skip(f"fractal-interact probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.fail(f"probe ran but did not complete: {data.get('error')}")
    return data


def _case(probe, name):
    c = probe["cases"][name]
    assert "error" not in c, f"{name}: {c.get('error')}"
    return c


def test_the_page_raised_no_errors_while_navigating(probe):
    assert not probe["errors"], probe["errors"]


# ── box-zoom (escape-time only) ──────────────────────────────────────────────
@pytest.mark.parametrize("name", ESCAPE)
def test_the_zoom_box_is_shown_locked_to_the_canvas_ratio(probe, name):
    c = _case(probe, name)
    assert c["cursor"] == "crosshair", c["cursor"]
    assert c["boxShown"], f"{name}: no selection rectangle appeared during the drag"
    assert abs(c["boxRatio"] - c["canvasRatio"]) < 0.02, \
        f"{name}: box ratio {c['boxRatio']} != canvas ratio {c['canvasRatio']}"
    assert c["boxInside"], f"{name}: the selection box left the canvas"


@pytest.mark.parametrize("name", ESCAPE)
def test_dragging_a_box_zooms_in_recenters_and_shows_reset(probe, name):
    c = _case(probe, name)
    assert c["boxGone"], f"{name}: the overlay was not removed on release"
    assert c["zoomedIn"], f"{name}: {c['view0']} -> {c['view1']} did not zoom in"
    assert c["recentred"], f"{name}: the center did not move to the box"
    assert c["genGrew"], f"{name}: the card did not re-render"
    assert c["resetShown"], f"{name}: Reset zoom stayed hidden after a box-zoom"


@pytest.mark.parametrize("name", ESCAPE)
def test_plain_click_still_zooms_in_and_shift_click_out(probe, name):
    c = _case(probe, name)
    assert c["clickZoomIn"], f"{name}: a plain click did not zoom in"
    assert c["shiftZoomOut"], f"{name}: shift-click did not zoom out"


def test_only_escape_time_cards_take_box_zoom(probe):
    for name in ("lsystem", "ifs_chaos", "ifs_depth", "attractor"):
        c = _case(probe, name)
        assert "boxShown" not in c, f"{name}: box-zoom was wired to a non-escape-time card"
        assert c["cursor"] != "crosshair", f"{name}: got the box-zoom cursor"


# ── the iteration / depth / detail control (every family) ────────────────────
@pytest.mark.parametrize("name,label,field", [
    ("mandelbrot", "Iterations", "iter"),
    ("julia", "Iterations", "iter"),
    ("lsystem", "Depth", "depth"),
    ("ifs_chaos", "Iterations", "points"),   # fern: point-cloud chaos game
    ("ifs_depth", "Depth", "depth"),         # sierpinski: deterministic tiling
    ("attractor", "Steps", "steps"),
])
def test_every_family_has_the_right_control(probe, name, label, field):
    c = _case(probe, name)
    assert c["ctl"], f"{name}: no control strip"
    assert c["ctlLabel"] == label, f"{name}: {c['ctlLabel']!r} != {label!r}"
    assert c["field"] == field


@pytest.mark.parametrize("name", ALL)
def test_moving_the_control_changes_the_spec_and_rerenders(probe, name):
    c = _case(probe, name)
    assert c["after"] != c["before"], f"{name}: the spec field did not change"
    assert str(c["after"]) == str(c["outText"]), f"{name}: readout {c['outText']!r} != value {c['after']}"
    assert c["ctlGenGrew"] or c["ctlSigChanged"], f"{name}: the card did not re-render"


@pytest.mark.parametrize("name", ESCAPE)
def test_changing_iterations_keeps_the_current_pan_and_zoom(probe, name):
    """Deepening iterations must not throw away a box-zoom the user already did."""
    c = _case(probe, name)
    assert c["viewKept"], f"{name}: changing iterations reset the zoom"


# ── two behaviours a click round-trip cannot see ─────────────────────────────
def test_a_steps_change_reintegrates_the_attractor_orbit(probe):
    a = probe["attractor_reintegrate"]
    assert a["steps1"] == 40000, a
    assert a["orbitN1"] > a["orbitN0"], \
        f"the orbit was not re-integrated on a Steps change: {a['orbitN0']} -> {a['orbitN1']}"


def test_an_over_deep_lsystem_depth_reverts_instead_of_blanking(probe):
    r = probe["lsystem_revert"]
    assert r["after"] == r["before"], f"the spec depth did not revert: {r}"
    assert r["inpVal"] == r["before"] and str(r["outText"]) == str(r["before"]), r
    assert r["sigRestored"], \
        "the last good drawing was not restored (the card went blank) after an over-deep depth"


def test_fractal_control_strip_is_not_duplicated_on_rerender(probe):
    """Apply/Reset rebuild the card; the control strip is a sibling of the
    output, so a rebuild that did not clean it up would stack a new slider under
    the card each time."""
    d = probe["rerender_dedupe"]
    assert d["first"] == 1, f"a fresh fractal card should have exactly one control strip, got {d['first']}"
    assert d["after"] == 1, f"re-rendering left {d['after']} control strips stacked under the card"


def test_a_resized_fractal_re_renders_crisply_at_the_new_size(probe):
    """A canvas is a fixed raster; without a re-render on resize it stretches
    blurry and box-zoom maps onto the wrong region. The backing store must
    follow the container and stay square with the display."""
    r = probe["resize_rerender"]
    assert r["resized"], f"the backing store did not follow the resize: {r}"
    assert r["w1"] < r["w0"], f"the canvas did not shrink with the card: {r}"
    assert abs(r["backingAspect"] - r["displayAspect"]) < 0.05, \
        f"backing {r['backingAspect']} and display {r['displayAspect']} aspects diverged"


def test_a_pending_control_redraw_is_cancelled_when_the_card_is_torn_down(probe):
    """A slider input schedules a debounced redraw; if the card is re-rendered
    (Apply/Reset) inside that window, the stale timer must be cleared so it does
    not fire apply() on the detached instance (a spurious error toast)."""
    s = probe["stale_timer"]
    assert s["hadTimer"], "the input did not schedule a debounced redraw"
    assert s["timerCleared"], "tearing down the card left the debounce timer live"
    assert not s["spuriousToast"], "the stale timer fired on the detached instance"


def test_a_self_tiling_ifs_builds_structure_with_depth(probe):
    """The Sierpinski gasket is drawn as nested triangle OUTLINES: each depth adds
    a level of smaller triangles, so the total stroke grows sharply with depth and
    spreads across the whole figure. A broken MRCM that collapses every copy onto a
    few positions would stroke roughly the same amount at any depth and cluster it
    into a corner (auto-fit still stretches those few triangles across the frame,
    so the bounding box alone cannot catch it — the growth and spread do)."""
    s = probe["ifs_structure"]
    lo, mid = s["low"], s["mid"]
    assert lo["depth"] == 2 and mid["depth"] == 6, s
    assert mid["ink"] > 0 and lo["ink"] > 0, s
    assert mid["bboxW"] > 0.5 * mid["W"], f"depth-6 Sierpinski collapsed: bbox {mid['bboxW']}/{mid['W']}"
    assert mid["bboxH"] > 0.4 * mid["H"], s
    # depth 2 → 6 is 9 → 729 triangles; the stroke total grows several-fold
    # (measured ~4.1x). A collapse leaves it roughly flat.
    assert mid["ink"] > 2.5 * lo["ink"], \
        f"nesting did not add triangles with depth: d2 ink {lo['ink']} -> d6 ink {mid['ink']}"
    # and the triangles spread across the frame, not into a few clustered copies
    assert mid["gridCells"] >= 24, f"depth-6 Sierpinski clustered instead of tiling ({mid['gridCells']}/64 cells)"


def test_the_sierpinski_gasket_is_drawn_as_triangles_not_squares(probe):
    """The user asked for nested triangles: the gasket's seed must be a triangle,
    so its silhouette tapers to an apex (top band far narrower than the base). A
    square seed would give a flat, full-width top (ratio ~1)."""
    s = probe["ifs_seed"]["sierpinski"]
    assert s["botW"] > 0, s
    # triangle apex ~0.10; a square seed measures ~0.25, so 0.18 separates them.
    assert s["ratio"] < 0.18, f"Sierpinski top/bottom width {s['ratio']:.2f} — not a triangular apex (square seed?)"
