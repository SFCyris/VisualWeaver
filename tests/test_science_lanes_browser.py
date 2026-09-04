# SPDX-License-Identifier: AGPL-3.0-or-later
"""The science / 3-D lanes, driven in a real browser.

test_science_lanes_js.py pins the pure helpers offline. This file drives
``tests/science_lanes_probe.py`` through Playwright and asserts on what each card
actually did: Plotly bars, ODE sliders, WebGL pixels and orbit, sequence cells,
tree leaves, reaction structures, circuit symbols, the ```plot extensions with
the real math.js, and the extra mermaid diagram types — plus Apply/Reset on each.

Same design as ``tests/test_fractal_interact_browser.py``.
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
_PROBE = pathlib.Path(__file__).with_name("science_lanes_probe.py")

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
                       capture_output=True, text=True, timeout=900)
    out = (r.stdout or "").strip()
    if not out:
        pytest.skip(f"science-lanes probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
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
    c = probe[name]
    assert "error" not in c, f"{name}: {c.get('error')}"
    return c


def _scene(probe):
    c = probe["scene"]
    if "error" in c and "WebGL" in c["error"]:
        pytest.skip(f"no WebGL in this browser: {c['error']}")
    assert "error" not in c, c.get("error")
    return c


def test_the_page_raised_no_errors_while_rendering(probe):
    assert not probe["errors"], probe["errors"]


# ── plotly ───────────────────────────────────────────────────────────────────
def test_plotly_draws_a_histogram_with_its_title_and_a_png_button(probe):
    c = _case(probe, "plotly")
    assert c["kind"] == "plotly" and c["plotDiv"]
    assert c["bars"] >= 3, f"a histogram of 13 values binned into {c['bars']} bars"
    assert "Response times" in c["title"]
    assert c["h"] >= 300 and c["pngBtn"]


def test_plotly_apply_swaps_the_figure_in_place_and_reset_restores_it(probe):
    c = _case(probe, "plotly")
    assert c["applied"] and c["afterBoxes"] == 1 and c["afterBars"] == 0
    assert c["plotDivs"] == 1, "Apply must replace the Plotly host, not stack a second one"
    assert c["reset"] and c["resetBars"] >= 3


def test_plotly_maximize_resizes_the_figure(probe):
    c = _case(probe, "plotly")
    assert c["maxW"] > c["w0"] + 100, c
    assert abs(c["restoredW"] - c["w0"]) <= 4, c


# ── ode ──────────────────────────────────────────────────────────────────────
def test_ode_renders_with_one_slider_strip_per_declared_params(probe):
    c = _case(probe, "ode")
    assert c["svg"] and c["traj"]
    assert c["sliders"] == 2 and c["strips"] == 1


def test_ode_sliders_reintegrate_the_portrait(probe):
    c = _case(probe, "ode")
    assert c["dChanged"], "moving the damping slider did not change the trajectory"
    assert c["stripsAfterSlide"] == 1


def test_ode_apply_does_not_stack_slider_strips(probe):
    c = _case(probe, "ode")
    assert c["applied"] and c["stripsAfterApply"] == 0, c
    assert c["arrows"] > 100 and c["horizontal"], "dx=1, dy=0 must draw a horizontal field"
    assert c["reset"] and c["stripsAfterReset"] == 1 and c["slidersAfterReset"] == 2
    assert c["applied2"] and c["stripsAfterApply2"] == 1, f"a re-applied system stacked strips: {c['stripsAfterApply2']}"
    assert c["stripsAfterError"] == 0


def test_ode_reports_an_undeclared_symbol(probe):
    c = _case(probe, "ode")
    assert c["applied3"] and "'z' is not defined" in c["undefinedMsg"], c["undefinedMsg"]


def test_ode_card_hugs_its_figure(probe):
    c = _case(probe, "ode")
    assert c["outH"] - c["svgH"] <= 40, f"dead band under the portrait: card {c['outH']} vs figure {c['svgH']}"


# ── scene ────────────────────────────────────────────────────────────────────
def test_scene_draws_webgl_pixels_that_fill_the_card(probe):
    c = _scene(probe)
    assert c["kind"] == "scene"
    assert c["ink"] > 1500, f"only {c['ink']} opaque samples — the scene did not draw"
    assert c["cssW"] >= 500 and c["cw"] == c["cssW"], c   # fills the card, backing store = display size


def test_scene_orbit_reveals_reset_and_changes_the_pixels(probe):
    c = _scene(probe)
    assert c["resetHidden0"] and c["cursor0"] == "grab"
    assert c["cursorDrag"] == "grabbing" and c["cursorAfter"] == "grab"
    assert c["orbited"] and c["interacted"] and c["resetShown"]
    assert c["pixelsChanged"], "the camera moved but the rendered pixels did not"


def test_scene_wheel_zooms_and_shift_drag_pans(probe):
    c = _scene(probe)
    assert c["zoomedOut"] and c["panned"]


def test_scene_reset_restores_the_starting_view(probe):
    c = _scene(probe)
    assert c["resetCamera"] and c["resetTarget"], c
    assert c["resetHiddenAgain"] and not c["interactedAfterReset"]


def test_scene_exports_png_and_resizes_when_maximized(probe):
    c = _scene(probe)
    assert c["pngBtn"] and c["png"]
    assert c["maxCw"] > c["cw"], f"maximize did not grow the renderer: {c['cw']} -> {c['maxCw']}"
    assert c["maxInk"] > c["ink"]
    assert c["restoredCw"] == c["cw"]


def test_scene_teardown_releases_the_gl_context(probe):
    c = _scene(probe)
    assert c["contextLost"], "destroyRichBlocks left the WebGL context alive"


def test_scene_shows_a_hint_until_the_first_interaction(probe):
    c = _scene(probe)
    assert c["hint0"], "no interaction hint on a fresh scene"
    assert c["hintHidden"], "the hint stayed after the reader orbited"


def test_scene_is_keyboard_and_screen_reader_reachable_and_does_not_trap_page_scroll(probe):
    c = _scene(probe)
    assert c["role"] == "img" and c["tabIndex"] == 0 and "rotate" in c["aria"]
    assert c["keyOrbit"], "ArrowLeft did not orbit the camera"
    assert c["touchAction"] == "pan-y", c["touchAction"]


def test_scene_follows_a_theme_change(probe):
    c = _scene(probe)
    assert c["darkHemi"] < c["hemi0"] and c["lightHemi"] == c["hemi0"], c


def test_scene_canvas_follows_its_card_width(probe):
    c = _scene(probe)
    assert c["resizedCw"] < c["cw"] - 100, f"the backing store stayed at {c['resizedCw']} after the card narrowed"
    assert c["regrownCw"] == c["cw"]
    assert abs(c["resizedAspect"] - c["resizedCw"] / c["ch"]) < 0.3 or c["resizedAspect"] < c["cw"] / c["ch"]


def test_a_lost_gl_context_pauses_the_view_and_a_restore_repaints_it(probe):
    c = _scene(probe)
    if "pausedShown" not in c:
        pytest.skip("WEBGL_lose_context not available")
    assert c["pausedShown"], "no 'paused' overlay after the context was lost"
    assert c["pausedHidden"] and c["inkAfterRestore"] > 1500, c


def test_scene_rejects_an_unknown_type_and_falls_back_on_bad_colours(probe):
    c = probe["scene_edge"]
    if "error" in c and "WebGL" in c["error"]:
        pytest.skip(c["error"])
    assert "error" not in c, c.get("error")
    assert c["scaleX"] == 2, "a numeric scale must apply to every axis"
    assert c["sphereColor"] == "ef6b6b", f"an unreadable colour must fall back to the palette, got {c['sphereColor']}"
    assert "unknown object type" in (c["unknownErr"] or "") and "pyramid" in c["unknownErr"]
    assert c["boxW"] == 2, "width/height/depth must size a box"
    assert not c["hugeErr"] and c["hugeDrawn"], f"a huge scene must still draw (grid divisions capped): {c['hugeErr']}"


# ── sequence / phylo / reaction / circuit ────────────────────────────────────
def test_sequence_draws_a_cell_per_residue_and_marks_alignment_columns(probe):
    c = _case(probe, "sequence")
    assert c["rects"] == 39 and c["svgW"] > 400 and c["pngBtn"]
    assert c["stars"] == 9 and c["dots"] == 1 and c["names"] == 2


def test_a_sequence_diagram_in_a_sequence_fence_is_drawn_as_a_diagram(probe):
    c = _case(probe, "sequence")
    assert not c["diagramErr"] and c["diagramSvg"] and c["diagramText"], c
    assert c["diagramCells"] == 0, "the diagram was coloured as residues"
    assert "not a residue" in (c["badErr"] or ""), c["badErr"]


def test_a_long_sequence_scrolls_inside_a_capped_card(probe):
    c = _case(probe, "sequence")
    assert not c["longErr"], c["longErr"]
    assert c["longH"] <= 540 and c["longScroll"] > 1000, c


def test_sequence_apply_and_reset_rerender(probe):
    c = _case(probe, "sequence")
    assert c["applied"] and c["rectsAfter"] == 8
    assert c["reset"] and c["rectsReset"] == 39


def test_phylo_draws_the_leaves_labels_and_scale_bar(probe):
    c = _case(probe, "phylo")
    assert c["leaves"] == 4 and c["svgW"] > 500
    assert {"Human", "Chimp", "Mouse", "Rat", "0.1"} <= set(c["texts"]), c["texts"]


def test_phylo_apply_and_reset_rerender(probe):
    c = _case(probe, "phylo")
    assert c["applied"] and c["leavesAfter"] == 5 and c["textsAfter"] == 5
    assert c["reset"] and c["leavesReset"] == 4


def test_reaction_shows_species_signs_and_agents(probe):
    c = _case(probe, "reaction")
    assert c["mols"] == 4 and c["drawn"] == 4, c
    assert c["signs"] == 2 and c["arrow"]
    assert c["agents"] == "[H+]"
    assert c["molMinW"] >= 120 and c["molMinH"] >= 120, c   # every structure box is laid out at size
    assert c["arrowW"] >= 24 and c["inCard"], c              # arrow drawn; nothing overflows the card


def test_reaction_rejects_a_plain_smiles_and_reset_restores(probe):
    c = _case(probe, "reaction")
    assert c["applied"] and "reactants>>products" in c["errMsg"], c["errMsg"]
    assert c["reset"] and c["molsReset"] == 4


def test_reaction_reports_an_unparseable_species(probe):
    c = _case(probe, "reaction")
    assert c["appliedBad"] and "could not parse SMILES: C(C" in c["errMsgBad"], c["errMsgBad"]


def test_reaction_structures_size_to_the_card(probe):
    c = _case(probe, "reaction")
    assert c["twoW"] >= 200, f"two species should get large structures, got {c['twoW']} px"
    assert c["twoMaxW"] >= 230 and c["twoMaxDrawn"], c
    assert c["twoBackW"] == c["twoW"]


def test_circuit_draws_symbols_labels_and_a_png_button(probe):
    c = _case(probe, "circuit")
    assert c["symbols"] == 3 and c["pngBtn"]
    assert "9 V" in c["labels"] and "R1 220 Ω" in c["labels"]
    assert c["svgW"] > 250


def test_circuit_apply_and_reset_rerender(probe):
    c = _case(probe, "circuit")
    assert c["applied"] and c["symbolsAfter"] == 4
    assert c["junctions"] == 2, "the capacitor across the rails makes two 3-way junctions"
    assert c["reset"] and c["symbolsReset"] == 3


# ── plot extensions with the real math.js ────────────────────────────────────
@pytest.mark.parametrize("name,curves,arrows,levels,words", [
    ("parametric", 1, 0, 0, ["x(t)", "y(t)"]),
    ("polar", 1, 0, 0, ["r(θ)"]),
    ("field", 0, 256, 0, ["field"]),
    ("contour", 0, 0, 8, ["contour"]),
    ("implicit", 1, 0, 0, ["implicit"]),
    ("mixed", 1, 256, 0, ["y", "field"]),
    ("slider", 1, 0, 0, ["x(t)"]),
])
def test_plot_extension_renders_and_lists_its_definition(probe, name, curves, arrows, levels, words):
    c = _case(probe, "plot")[name]
    assert c["svg"], f"{name}: {c['err']}"
    assert c["curves"] == curves and c["arrows"] == arrows and c["levels"] == levels, c
    for w in words:
        assert w in (c["defs"] or ""), f"{name}: {w!r} not in defs {c['defs']!r}"


def test_plot_defs_show_the_field_in_its_own_stroke_colour(probe):
    c = _case(probe, "plot")["field"]
    assert c["defColors"] == ["rgb(100, 116, 139)"], c["defColors"]


def test_a_parametric_plot_gets_its_param_slider(probe):
    assert _case(probe, "plot")["slider"]["sliders"] == 1


# ── the additional mermaid diagram types ─────────────────────────────────────
@pytest.mark.parametrize("name", ["mindmap", "quadrant", "xychart", "sankey", "block", "kanban"])
def test_mermaid_renders_the_additional_diagram_types(probe, name):
    c = _case(probe, "mermaid")[name]
    assert c["svg"] and c["children"] > 0 and not c["syntaxError"], c
    assert c["h"] > 60, c
