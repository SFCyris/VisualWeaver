# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply / Reset on the ```plot, ```abc, ```svg and ```latex cards, measured.

A ```table card rides along as the rich-lane reference the request pointed at.

test_editable_source_js.py pins the wiring in the page source. This file drives
``tests/editable_probe.py`` through Playwright and asserts on what the cards
actually held after each click — pixel boxes, note counts, slider counts — so a
handler that is wired but re-renders nothing (or renders a hostile edit) fails
here rather than in the user's browser. The scenarios beyond the plain round
trip (real keystrokes, a real Maximize, a queued slider redraw, a missing
renderer, a Play still loading when Apply lands) each reproduce a defect that
the round trip alone could not see.

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
_PROBE = pathlib.Path(__file__).with_name("editable_probe.py")

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
FAMILIES = ["svg", "tex", "abc", "plot", "rich"]


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
        pytest.skip(f"editable probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"editable probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.fail(f"editable probe ran but did not complete: {data.get('error')}")
    return data


def _case(probe, fam):
    c = probe["cases"][fam]
    assert "error" not in c, f"{fam}: {c.get('error')}"
    return c


def _scenario(probe, name):
    s = probe[name]
    assert "error" not in s, f"{name}: {s.get('error')}"
    return s


def test_the_page_raised_no_errors_while_editing(probe):
    assert not probe["errors"], probe["errors"]


# ── the plain round trip, per family ─────────────────────────────────────────
@pytest.mark.parametrize("fam", FAMILIES)
def test_opening_source_makes_it_editable_with_a_visible_apply_bar(probe, fam):
    o = _case(probe, fam)["afterOpen"]
    assert o["bar"], f"{fam}: no Apply bar was attached"
    assert o["editable"] == "plaintext-only", f"{fam}: source is not editable ({o['editable']!r})"
    assert o["isEditable"], f"{fam}: the browser does not treat the source as editable"
    assert o["preShown"], f"{fam}: the Source pane is not displayed"
    assert o["applyPx"] > 0 and o["resetPx"] > 0, \
        f"{fam}: Apply/Reset have no height on screen ({o['applyPx']}px / {o['resetPx']}px)"
    assert o["snapshotMatches"], f"{fam}: the pristine source was not snapshotted before editing"
    assert o["srcLabel"] == "Hide source"


@pytest.mark.parametrize("fam", FAMILIES)
def test_the_apply_bar_does_not_collide_with_the_copy_overlay(probe, fam):
    """Under the production `.msg-bubble.ai pre` rule the per-<pre> Copy button
    is absolutely positioned in the pane's top-right corner; the bar must sit
    clear of it and the code must start below the bar."""
    o = _case(probe, fam)["afterOpen"]
    assert o["copyBox"] is not None, f"{fam}: no Copy overlay in the pane"
    assert not o["copyOverlapsApply"], f"{fam}: Copy overlaps Apply: {o['copyBox']} vs {o['applyBox']}"
    assert not o["copyOverlapsReset"], f"{fam}: Copy overlaps Reset: {o['copyBox']} vs {o['resetBox']}"
    assert o["codeBelowBar"], f"{fam}: the code starts above the bar: {o['codeBox']} vs {o['applyBox']}"


@pytest.mark.parametrize("fam", FAMILIES)
def test_apply_and_reset_report_on_their_buttons(probe, fam):
    c = _case(probe, fam)
    assert c["applyLabel"] == "✓ Applied", f"{fam}: {c['applyLabel']!r}"
    assert c["resetLabel"] == "✓ Reset", f"{fam}: {c['resetLabel']!r}"
    assert c["restored"], f"{fam}: Reset did not put the original text back"


def test_svg_apply_redraws_at_the_new_size_and_reset_returns(probe):
    c = _case(probe, "svg")
    b, a, r = c["before"], c["after"], c["reset"]
    assert b["svgW"] == 120 and b["svgH"] == 60, (b["svgW"], b["svgH"])
    assert a["svgW"] == 240 and a["svgH"] == 120, \
        f"Apply did not redraw the SVG at its new size: {a['svgW']}×{a['svgH']}px"
    assert (r["svgW"], r["svgH"]) == (120, 60), (r["svgW"], r["svgH"])


def test_svg_apply_is_sanitised_like_the_original_render(probe):
    """The edit carried <style> and <script>: neither may reach the DOM, and the
    script must not have run."""
    a = _case(probe, "svg")["after"]
    assert not a["hasScript"], "a <script> from the edited SVG landed in the DOM"
    assert not a["hasStyle"], "a <style> from the edited SVG landed in the DOM"
    assert not a["pwned"], "the edited SVG's script RAN"


def test_latex_apply_typesets_the_new_formula_and_reset_returns(probe):
    c = _case(probe, "tex")
    b, a, r = c["before"], c["after"], c["reset"]
    assert not a["katexError"], f"KaTeX rejected the edit instead of typesetting it: {a['text']!r}"
    assert not b["hasSum"] and a["hasSum"], (b["text"], a["text"])
    assert a["htmlLen"] != b["htmlLen"]
    assert a["h"] > 0 and a["w"] > 0
    assert r["text"] == b["text"] and r["htmlLen"] == b["htmlLen"], (r["text"], b["text"])


def test_latex_apply_is_sanitised_like_the_original_render(probe):
    """The edit carried an <img onerror> inside \\text and a javascript: \\href:
    KaTeX with trust:false plus the DOMPurify gate must let neither through."""
    a = _case(probe, "tex")["after"]
    assert not a["hasImg"], "an <img> from the edited LaTeX landed in the DOM"
    assert not a["jsHref"], "a javascript: link from the edited LaTeX landed in the DOM"
    assert not a["pwnedTex"], "the edited LaTeX's payload RAN"


def test_abc_apply_engraves_the_new_score_and_reset_returns(probe):
    c = _case(probe, "abc")
    b, a, r = c["before"], c["after"], c["reset"]
    assert b["notes"] == 4, b["notes"]
    assert a["notes"] == 16, f"Apply did not engrave the new score: {a['notes']} notes"
    assert a["tune"], "the parsed tune for ▶ Play was not replaced"
    assert a["rendered"] == "1"
    assert r["notes"] == 4, r["notes"]


def test_plot_apply_rebuilds_sliders_and_the_definitions_block_once(probe):
    """The definitions block sits in FRONT of the card and the sliders under it;
    both are derived from the spec, so Apply must replace them — never leave the
    old ones and add new ones beside them."""
    c = _case(probe, "plot")
    b, a, r = c["before"], c["after"], c["reset"]
    assert b["sliders"] == 0 and b["defs"] == 1, (b["sliders"], b["defs"])
    assert a["sliders"] == 1, f"the `param:` line did not become a slider ({a['sliders']})"
    assert a["defs"] == 1, f"{a['defs']} definition blocks in front of the card after Apply"
    assert "sin" in (a["defsText"] or ""), a["defsText"]
    assert "sin" not in (b["defsText"] or ""), b["defsText"]
    assert a["svgW"] > 0 and a["htmlLen"] != b["htmlLen"]
    assert r["sliders"] == 0 and r["defs"] == 1, (r["sliders"], r["defs"])
    assert "sin" not in (r["defsText"] or ""), r["defsText"]


# ── the scenarios a click round trip cannot see ──────────────────────────────
def test_typed_multiline_source_applies_as_typed(probe):
    """Real keystrokes with Enter: whatever nodes the browser made for the line
    break, Apply must see a two-line spec — a param line and a function."""
    t = _scenario(probe, "typed")
    assert t["focused"], "the source did not take focus"
    assert t["sliders"] == 1, f"the typed `param:` line did not become a slider: {t}"
    assert "sin" in (t["defsText"] or ""), t
    assert t["applied"].count("\n") == 1, f"the applied text is not two lines: {t['applied']!r}"


def test_reset_while_maximized_declines_without_touching_the_source(probe):
    """A Reset that restored the text but could not redraw would leave the pane
    and the card disagreeing, and 'Already the original' would then block the
    retry. After restore, Reset must work normally."""
    m = _scenario(probe, "maximized")
    assert m["appliedW"] == 300, m
    assert m["inOverlay"] and m["overlayW"] > 0, m
    assert m["blockedWhy"] == "Restore the maximized card first", m["blockedWhy"]
    assert m["textUntouched"], "Reset overwrote the source while the output was maximized"
    assert m["overlayWAfterReset"] == m["overlayW"], m
    assert m["resetLabelWhileMax"] == "↺ Reset", m["resetLabelWhileMax"]
    assert m["backW"] == 300, f"restore did not bring the applied drawing back: {m['backW']}"
    assert m["restored"] and m["finalW"] == 120, m


def test_a_slider_redraw_queued_before_apply_cannot_repaint_the_old_spec(probe):
    p = _scenario(probe, "pending")
    assert p["sliders"] == 0, p
    assert "1000" in (p["defsText"] or ""), p
    assert p["has1000"], f"the old spec's redraw landed on top of the new one: {p['plotText']!r}"


def test_apply_declines_and_keeps_the_card_when_the_renderer_is_missing(probe):
    n = _scenario(probe, "nolib")
    assert n["err"] is None, n["err"]
    assert n["direct"] is False, n
    assert "not loaded" in n["why"], n["why"]
    assert n["label"] == "▶ Apply", f"the button claimed success: {n['label']!r}"
    assert n["htmlAfter"] == n["htmlBefore"] > 0, n
    assert n["rendered"] == "1" and n["defs"] == 1, n


def test_a_play_still_loading_when_apply_lands_is_discarded(probe):
    """▶ Play awaits the soundfont before it stores its synth on the card. An
    Apply in that window replaces the score; the synth built from the OLD tune
    must be dropped, not handed back to the card."""
    s = _scenario(probe, "inflight")
    assert s["loading"]["label"] == "… loading", s["loading"]
    assert s["afterApply"]["notes"] == 16 and not s["afterApply"]["synth"], s["afterApply"]
    assert s["loading"]["disabled"], s["loading"]
    assert s["afterApply"]["label"] == "▶ Play" and not s["afterApply"]["disabled"], \
        f"the re-rendered card's Play button is still held by the superseded load: {s['afterApply']}"
    st = s["settled"]
    assert not st["synth"], "the old tune's synth was stored on the re-rendered card"
    assert not st["playing"] and st["started"] == 0, st
    assert st["inits"] == 1 and st["label"] == "▶ Play" and not st["disabled"], st
    assert st["notes"] == 16


def test_rich_lane_apply_and_reset_still_redraw_the_reference_card(probe):
    """The lane the request pointed at ("like you can in the fractal") must keep
    working through the generalised handlers."""
    c = _case(probe, "rich")
    b, a, r = c["before"], c["after"], c["reset"]
    assert b["ths"] == 2, b
    assert a["ths"] == 3, f"Apply did not redraw the table with its new column: {a}"
    assert a["rendered"] == "1"
    assert r["ths"] == 2, r


def test_the_end_of_tune_timer_does_not_clear_a_later_playback(probe):
    """Each Play arms a timer that flips the button back at the end of the tune.
    Stop-then-Play and Apply-then-Play both leave an earlier timer alive; it must
    not end the playback that replaced the one it was armed for."""
    s = _scenario(probe, "stoptimer")
    assert s["s1"]["label"] == "⏸ Stop" and s["s1"]["playing"], s["s1"]
    assert s["s2"]["label"] == "▶ Play" and not s["s2"]["playing"] and s["s2"]["stopped"] == 1, s["s2"]
    assert s["s3"]["label"] == "⏸ Stop" and s["s3"]["started"] == 2, s["s3"]
    assert s["mid"]["label"] == "⏸ Stop" and s["mid"]["playing"], \
        f"the first start's timer ended the second playback: {s['mid']}"
    assert s["ended"]["label"] == "▶ Play" and not s["ended"]["playing"], s["ended"]
    assert s["afterApply"]["label"] == "▶ Play" and not s["afterApply"]["playing"], s["afterApply"]
    assert s["s4"]["label"] == "⏸ Stop" and s["s4"]["started"] == 4, s["s4"]
    assert s["mid2"]["label"] == "⏸ Stop" and s["mid2"]["playing"], \
        f"the old score's timer ended the new score's playback: {s['mid2']}"


def test_apply_on_one_card_leaves_its_siblings_alone(probe):
    """A sibling that failed to render carries rendered='' — exactly the state a
    message-wide render pass picks up. Its Source was edited but never applied;
    Apply on the OTHER card must not commit that edit."""
    s = _scenario(probe, "siblings")
    assert s["before"]["B"]["rendered"] == "" and "Plot error" in s["before"]["B"]["text"], s["before"]["B"]
    assert s["after"]["A"]["text"] != s["before"]["A"]["text"], "A's own Apply did nothing"
    assert s["after"]["B"] == s["before"]["B"], \
        f"Apply on A re-rendered B from B's un-applied edit: {s['after']['B']}"
