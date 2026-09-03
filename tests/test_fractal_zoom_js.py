# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ratio-locked box-zoom maths for escape-time fractals, as real JavaScript.

Box-zoom lets the user drag a rectangle on a Mandelbrot/Julia card to zoom into
it. Two pure helpers carry the geometry, so it can be checked without a browser:

* ``_fracLockBox`` forces the drawn rectangle to the CANVAS aspect ratio (so the
  zoom introduces no distortion — "keeps the ratio of the box") and clamps it to
  stay on-canvas;
* ``_fracBoxView`` turns that rectangle into the new {cx, cy, zoom} that makes it
  fill the frame, clamped to the render's zoom limits.

The drag/redraw wiring and the per-family iteration control are exercised in a
real browser by tests/test_fractal_interact_browser.py.
"""
import json
import math
import pathlib

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _run(call_js: str):
    src = extract_js_function(_html(), "_fracLockBox") + "\n" \
        + extract_js_function(_html(), "_fracBoxView") + "\n" \
        + "console.log(JSON.stringify((()=>{" + call_js + "})()));"
    r = run_node(src)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── _fracLockBox ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("rw,rh", [(560, 372), (372, 560), (400, 400)])
@pytest.mark.parametrize("mx,my", [(500, 60), (60, 300), (520, 350), (10, 10)])
def test_the_box_is_locked_to_the_canvas_aspect_ratio(rw, rh, mx, my):
    box = _run(f"return _fracLockBox(200,180,{mx},{my},{rw},{rh});")
    if box is None:
        return
    assert box["bw"] > 0 and box["bh"] > 0
    assert abs((box["bw"] / box["bh"]) - (rw / rh)) < 1e-6, \
        f"box ratio {box['bw']/box['bh']} != canvas ratio {rw/rh}"


@pytest.mark.parametrize("mx,my", [(2000, 40), (40, 2000), (-500, -500), (900, 900)])
def test_the_box_never_leaves_the_canvas(mx, my):
    rw, rh = 560, 372
    box = _run(f"return _fracLockBox(300,200,{mx},{my},{rw},{rh});")
    assert box is not None
    assert box["bx"] >= -1e-6 and box["by"] >= -1e-6
    assert box["bx"] + box["bw"] <= rw + 1e-6, (box, rw)
    assert box["by"] + box["bh"] <= rh + 1e-6, (box, rh)
    # still ratio-locked after the clamp
    assert abs((box["bw"] / box["bh"]) - (rw / rh)) < 1e-6


def test_the_anchor_is_a_corner_and_the_box_grows_toward_the_pointer():
    # pointer up-and-left of the anchor → box sits up-and-left, anchor bottom-right
    box = _run("return _fracLockBox(400,300,120,120,560,372);")
    assert box["bx"] < 400 and box["by"] < 300
    assert abs(box["bx"] + box["bw"] - 400) < 1e-6, "anchor x must be the box's right edge"


def test_a_zero_area_drag_returns_null():
    assert _run("return _fracLockBox(100,100,100,100,560,372);") is None


# ── _fracBoxView ─────────────────────────────────────────────────────────────
def test_zooming_a_box_multiplies_zoom_by_the_inverse_width_fraction():
    # a box a quarter of the canvas wide → 4x the zoom
    v = _run("return _fracBoxView({cx:-0.5,cy:0,zoom:1},210,139,140,93,560,372,560,372);")
    assert abs(v["zoom"] - 560 / 140) < 1e-6, v


def test_a_centered_box_keeps_the_center():
    # box centered on the canvas center → center unchanged, only zoom rises
    v = _run("return _fracBoxView({cx:-0.5,cy:0.25,zoom:2},210,139.5,140,93,560,372,560,372);")
    assert abs(v["cx"] - (-0.5)) < 1e-9, v
    assert abs(v["cy"] - 0.25) < 1e-9, v
    assert v["zoom"] > 2


def test_an_offcenter_box_moves_the_center_the_right_way():
    # a box in the upper-right quadrant → center moves +x, +y (screen y is flipped)
    v = _run("return _fracBoxView({cx:0,cy:0,zoom:1},300,40,140,93,560,372,560,372);")
    assert v["cx"] > 0, v
    assert v["cy"] > 0, "a box above the middle must raise the imaginary center"


def test_zoom_is_clamped_to_the_render_ceiling():
    # a 1px-wide box at an already deep zoom must not exceed 1e12
    v = _run("return _fracBoxView({cx:0,cy:0,zoom:1e11},279,185,2,1.33,560,372,560,372);")
    assert v["zoom"] == 1e12, v


def test_zoom_never_drops_below_the_floor():
    v = _run("return _fracBoxView({cx:0,cy:0,zoom:1e-3},0,0,560,372,560,372,560,372);")
    assert v["zoom"] >= 1e-3, v


def test_box_view_math_matches_the_render_sampling():
    """The center of the selection box must map to the SAME complex point the
    escape-time renderer samples there — the renderer uses span=3.2/zoom and
    sy=span*H/W (H,W the BACKING store), sampling pixel (px,row) at
    (x0+px/W*span, y0-row/H*sy)."""
    view = {"cx": -0.5, "cy": 0.1, "zoom": 2.0}
    rw, rh = 560.0, 372.0
    cw, ch = 1120.0, 744.0                        # backing = 2x dpr, same aspect
    bx, by, bw, bh = 140.0, 93.0, 210.0, 139.5
    v = _run(f"return _fracBoxView({json.dumps(view)},{bx},{by},{bw},{bh},{rw},{rh},{cw},{ch});")
    span = 3.2 / view["zoom"]
    sy = span * ch / cw
    x0 = view["cx"] - span / 2
    y0 = view["cy"] + sy / 2
    fcx, fcy = (bx + bw / 2) / rw, (by + bh / 2) / rh
    assert math.isclose(v["cx"], x0 + fcx * span, rel_tol=0, abs_tol=1e-9)
    assert math.isclose(v["cy"], y0 - fcy * sy, rel_tol=0, abs_tol=1e-9)


def test_box_view_uses_the_backing_aspect_not_the_display_aspect():
    """After a resize the fractal is not re-rendered synchronously, so the CSS
    display aspect can drift from the backing store the renderer sampled. The
    vertical mapping must follow the BACKING aspect (what is actually drawn), the
    way the click-zoom path does — otherwise a box-zoom mis-centers vertically."""
    view = {"cx": -0.5, "cy": 0.1, "zoom": 2.0}
    rw, rh = 296.0, 352.0        # stretched display after a horizontal resize
    cw, ch = 248.0, 352.0        # backing store still at the original aspect
    bx, by, bw, bh = 40.0, 20.0, 120.0, 143.0
    v = _run(f"return _fracBoxView({json.dumps(view)},{bx},{by},{bw},{bh},{rw},{rh},{cw},{ch});")
    span = 3.2 / view["zoom"]
    fcy = (by + bh / 2) / rh
    cy_backing = view["cy"] - (fcy - 0.5) * span * ch / cw
    cy_display = view["cy"] - (fcy - 0.5) * span * rh / rw
    assert math.isclose(v["cy"], cy_backing, rel_tol=0, abs_tol=1e-9), v
    assert abs(cy_backing - cy_display) > 1e-3, "the two aspects must actually differ here"
    assert abs(v["cy"] - cy_display) > 1e-3, "must NOT use the display aspect"
