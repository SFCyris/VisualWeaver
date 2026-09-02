# SPDX-License-Identifier: AGPL-3.0-or-later
"""The model only knows a rich block exists because the prompt says so.

`constants.DEFAULT_BASE_INSTRUCTION` is the sole place the model learns which
fenced blocks it may write. Nothing ties that prose to `RICH_LANES` in
index.html, so the two drift, and the drift is invisible until a user sees a red
error box in a rendered answer. It has gone wrong in both directions:

* Renderer ahead of the prompt — the fractal lane could draw Hilbert and Peano
  curves and Lorenz attractors long before the bullet named any of them, so the
  model reached for the lane, guessed the vocabulary, and got
  "unknown fractal type".
* Prompt ahead of the renderer — a rewrite of that bullet advertised a `d`
  coefficient for the Rossler attractor, which has none, and dropped julia's
  `c`, which exists.

These tests pin the coarse half of that: every lane the app registers is taught,
and the prompt promises no block the app cannot draw. Per-lane option names are
still only guarded for the fractal lane (test_fractal_js.py).
"""
import pathlib
import re

import pytest

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"

# Fenced names the prompt teaches that are deliberately NOT RICH_LANES entries —
# each is rendered by its own pass in appendMessage, not by the lane table.
# constants.RENDERABLE_FENCES is the union of these and the lane keys; the test
# below asserts that, so the two cannot drift into separate sources of truth.
_ALTERNATE_RENDERERS = {
    "abc":      "renderAbcBlocks",     # sheet music, abcjs
    "plot":     "renderPlotBlocks",    # function graphs, math.js
    "latex":    "renderMath",          # KaTeX
    "svg":      None,                  # sanitised inline markup, no dedicated pass
}
# "```language" is the prose placeholder for an ordinary code fence. It is not a
# block, which is why it is not in RENDERABLE_FENCES either.
_PROSE_PLACEHOLDERS = {"language"}


def _rich_lanes() -> set[str]:
    """Top-level keys of RICH_LANES, via the shared token-aware scanner.

    The brace counter this used to carry was duplicated verbatim in
    test_base_instruction_drift.py and worked by luck: the literal holds 139
    braces inside strings, regex literals and comments that happen to balance.
    """
    from _jsrun import rich_lane_names

    lanes = set(rich_lane_names(_INDEX))
    assert {"chart", "mermaid", "fractal", "table"} <= lanes, \
        f"RICH_LANES extraction looks wrong: {sorted(lanes)}"
    assert 10 <= len(lanes) <= 60, f"implausible lane count {len(lanes)}"
    return lanes


def _prompt() -> str:
    from visualweaver import constants
    return constants.DEFAULT_BASE_INSTRUCTION


def test_every_registered_lane_has_its_own_bullet_in_the_prompt():
    """A lane the model is never told about can only be reached by luck."""
    bi = _prompt()
    missing = [l for l in sorted(_rich_lanes())
               if not re.search(r"(?m)^-\s*```" + re.escape(l) + r"\s*[—-]", bi)]
    assert not missing, (
        "these blocks are registered in RICH_LANES but have no bullet teaching "
        f"the model to write them: {missing}")


def test_the_prompt_promises_no_block_the_app_cannot_render():
    """The other direction: a fence the model is invited to write and nothing
    draws produces a raw code block, or a red box, in the answer."""
    lanes = _rich_lanes()
    taught = set(re.findall(r"```([a-z][a-z0-9]*)", _prompt()))
    from visualweaver import constants

    known = lanes | constants.RENDERABLE_FENCES | _PROSE_PLACEHOLDERS
    unrenderable = sorted(taught - known)
    assert not unrenderable, (
        "the prompt tells the model to write these fences, but nothing renders "
        f"them: {unrenderable}")


@pytest.mark.parametrize("fence,handler",
                         [(f, h) for f, h in _ALTERNATE_RENDERERS.items() if h])
def test_the_alternate_renderers_the_prompt_relies_on_still_exist(fence, handler):
    """These are taught by the prompt but rendered outside RICH_LANES, so
    the lane test above cannot see them. Deleting one would leave the prompt
    advertising a block with no renderer at all."""
    src = _INDEX.read_text(encoding="utf-8")
    assert f"function {handler}(" in src, \
        f"```{fence} is taught by the prompt but {handler}() is gone"
    assert f"```{fence}" in _prompt()


def test_the_alternate_renderer_list_has_not_gone_stale():
    """If a fence in the allowlist stops being taught, the entry is dead weight
    and the next reader will trust it."""
    bi = _prompt()
    dead = [f for f in _ALTERNATE_RENDERERS if f"```{f}" not in bi]
    assert not dead, f"allowlisted but no longer taught: {dead}"


def test_the_bullet_guard_fails_when_a_lane_has_no_bullet(tmp_path):
    """Mutates the INPUT and asserts the real guard goes red.

    The earlier version re-wrote the same comprehension inline and asserted on
    its own copy — it proved that a regex fails to match a made-up word, which is
    true regardless of the code under test. It could not detect the realistic
    failure (an extraction that silently drops lanes).
    """
    from _jsrun import rich_lane_names

    src = _INDEX.read_text(encoding="utf-8")
    mutated = tmp_path / "index.html"
    mutated.write_text(
        src.replace("\n  truth:{", "\n  quantumsparkline:{label:'x'},\n  truth:{", 1),
        encoding="utf-8")

    lanes = set(rich_lane_names(mutated))
    assert "quantumsparkline" in lanes, "the extractor did not even see the new lane"

    bi = _prompt()
    missing = [l for l in sorted(lanes)
               if not re.search(r"(?m)^-\s*```" + re.escape(l) + r"\s*[—-]", bi)]
    assert missing == ["quantumsparkline"], (
        f"the guard would not have reported the unbulleted lane: {missing}")


def test_the_fence_allowlist_has_exactly_one_source():
    """RENDERABLE_FENCES must be the lane keys plus the alternate renderers —
    nothing more, nothing less. Two hand-maintained lists of the same fact is the
    bug class this whole file exists to guard."""
    from visualweaver import constants

    expected = _rich_lanes() | set(_ALTERNATE_RENDERERS)
    assert set(constants.RENDERABLE_FENCES) == expected, (
        f"only in constants: {sorted(set(constants.RENDERABLE_FENCES) - expected)}; "
        f"only in the code: {sorted(expected - set(constants.RENDERABLE_FENCES))}")


# Option names the prompt advertises that the app never handles itself, because a
# third-party library does. Each is real, and each is passed straight through.
_LIBRARY_HANDLED = {
    "coordinates": "geojson — Leaflet reads the GeoJSON geometry",
    "features":    "geojson — Leaflet reads the FeatureCollection",
    "group":       "network — vis-network groups nodes",
}


def _render_region(src: str) -> str:
    """From the fractal helpers to renderRichBlocks: the lane table and the
    helpers defined alongside it. Searching the whole file matched CSS
    declarations and every property access in the UI, which let almost any
    plausible word pass.

    Not everything a lane reaches for is inside it — the shared cell parser and
    enhanceTables sit further down the file — so a name used only there would be
    reported. None is today; if one appears, widen the region rather than
    allowlisting it.
    """
    i = src.index("const RICH_LANES={")
    j = src.index("function renderRichBlocks(")
    marker = "// ── ```fractal helpers"
    start = src.index(marker) if marker in src else i
    return src[start:j]


def _implemented_in(region: str, key: str) -> bool:
    """A quoted literal, or a property access. Deliberately narrow."""
    quoted = "['\"`]" + re.escape(key) + "['\"`]"
    prop = r"\." + re.escape(key) + r"\b"
    return re.search(quoted + "|" + prop, region) is not None


def test_the_prompt_advertises_no_option_nothing_implements():
    """The other half of the drift, and the half that bit hardest.

    A rewrite of the fractal bullet advertised a `d` coefficient for the Rossler
    attractor, which has none, so a model that used it had the value silently
    dropped. Neither the fence checks above nor base_instruction_drift can see an
    option-level change.

    Limits, so a green run is not read for more than it carries: this is a
    substring search over the rendering code, so a short or common name (`a`,
    `x`, `width`) passes whether or not the lane reads it — about half of a list
    of plausible-but-fictional names does. What it catches reliably is a
    DISTINCTIVE invented option, which is the realistic failure. It also only
    walks prompt to code: an option that exists and stopped being documented,
    julia's `c`, is invisible here and is covered by
    test_the_prompt_still_shows_julia_how_to_set_c instead.
    """
    region = _render_region(_INDEX.read_text(encoding="utf-8"))
    bi = _prompt()

    advertised: dict[str, set] = {}
    for line in bi.split("\n"):
        m = re.match(r"-\s*```([a-z][a-z0-9]*)\s*[—-]", line)
        if not m:
            continue
        for key in set(re.findall(r'"([a-z][a-z0-9_]*)"\s*:', line)):
            advertised.setdefault(key, set()).add(m.group(1))

    assert len(advertised) > 30, f"only {len(advertised)} options parsed — did the format change?"

    unimplemented = {k: sorted(v) for k, v in advertised.items()
                     if not _implemented_in(region, k) and k not in _LIBRARY_HANDLED}
    assert not unimplemented, (
        "the prompt tells the model to write these, and nothing in the rendering "
        f"code reads them: {unimplemented}")


def test_that_guard_would_catch_a_distinctive_invented_option():
    """Pins what the check is actually worth: a made-up name that does not look
    like a CSS property or a loop variable must not pass as implemented."""
    region = _render_region(_INDEX.read_text(encoding="utf-8"))
    for invented in ("zzfictional", "quantumsparkline", "stacked", "tooltip",
                     "animate", "density", "smoothing"):
        assert not _implemented_in(region, invented), f"{invented!r} passes as implemented"


def test_the_library_handled_allowlist_has_not_gone_stale():
    """If one of these starts being handled in app code, or stops being
    advertised, the entry is dead weight and the next reader will trust it."""
    src = _INDEX.read_text(encoding="utf-8")
    bi = _prompt()
    for key, why in _LIBRARY_HANDLED.items():
        assert f'"{key}"' in bi, f"{key!r} is allowlisted but no longer advertised ({why})"
        assert f'"{key}"' not in src, \
            f"{key!r} is handled in app code now — drop it from the allowlist ({why})"
