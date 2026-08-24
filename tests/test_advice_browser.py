# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RAG advice panel, measured in a real browser.

The whole feature is a UI: which lines appear, what colour they are, and whether a
preset moves the controls it names. The backend has 55 tests and 13 mutations behind it;
none of them can see any of that. The defect that shipped here — a preset writing only
the values that differed from the SAVED config, so a control the user had already
dragged stayed where it was while the toast said the preset had been applied — was
invisible to every one of them.

Browser work lives in ``tests/advice_probe.py``, run once per session under whichever
interpreter has Playwright. Same design as ``test_ux_browser.py``.
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
_PROBE = pathlib.Path(__file__).with_name("advice_probe.py")

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
def adv():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    # SKIP rather than fail on a harness problem: one flaky browser launch should not
    # read as two dozen unrelated behavioural failures.
    if not out:
        pytest.skip(f"advice probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"advice probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.skip(f"advice probe could not run: {data.get('error')}")
    return data




# ── only the lines that carry a finding ──────────────────────────────────────
def test_settings_no_measurement_can_speak_to_show_no_verdict(adv):
    """Three controls rendering an identical "Not measurable here" drowned the two that
    had something to say. The ⓘ still offers the explanation."""
    for key in ("chunk_size", "chunk_overlap"):
        line = adv["lines"][key]
        assert line["hidden"] and line["height"] == 0, (key, line)


def test_a_toggle_merely_restating_its_own_state_is_suppressed(adv):
    """"Hybrid Search: On" under a switch already reading On is noise."""
    assert adv["lines"]["hybrid_search"]["hidden"]


def test_the_lines_with_a_finding_are_shown(adv):
    for key in ("similarity_threshold", "top_k", "history_max_tokens"):
        assert not adv["lines"][key]["hidden"], key
        assert adv["lines"][key]["height"] > 0, key


# ── the colour coding, measured in pixels ────────────────────────────────────
def test_each_severity_renders_in_its_own_colour(adv):
    """The point of the feature. Reading the class name proves nothing — the rule could
    be missing and every line would still render in the inherited grey."""
    seen = {adv["lines"][k]["cls"].split()[-1]: adv["lines"][k]["color"]
            for k in ("similarity_threshold", "top_k", "history_max_tokens")}
    assert set(seen) == {"adv-bad", "adv-warn", "adv-unknown"}
    assert len(set(seen.values())) == 3, seen


def test_every_verdict_clears_the_contrast_floor(adv):
    """Measured against the composited backdrop, not against body: the panel sits on its
    own near-white surface, and comparing to body reported 4.33:1 for text that is 4.76."""
    for key in ("similarity_threshold", "top_k", "history_max_tokens"):
        line = adv["lines"][key]
        assert line["fontSize"] == "11px"
        assert line["contrast"] >= 4.5, (key, line["contrast"], line["color"])


def test_the_severity_is_carried_by_a_glyph_as_well_as_a_colour(adv):
    """A red dot alone fails for anyone who cannot separate it from the green one."""
    glyphs = {k: adv["lines"][k]["glyph"]
              for k in ("similarity_threshold", "top_k", "history_max_tokens")}
    assert all(glyphs.values()), glyphs
    assert len(set(glyphs.values())) == 3, glyphs


def test_a_screen_reader_is_told_the_severity(adv):
    """The headlines do not encode it: "Off" is a warning and "On" is fine."""
    assert adv["lines"]["similarity_threshold"]["sr"].strip() == "Problem:"
    assert adv["lines"]["top_k"]["sr"].strip() == "Warning:"
    assert adv["lines"]["history_max_tokens"]["sr"] == ""


# ── the apply affordance ─────────────────────────────────────────────────────
def test_a_numeric_suggestion_offers_its_value(adv):
    assert adv["lines"]["similarity_threshold"]["applyLabel"] == "use 0.37"
    assert adv["lines"]["top_k"]["applyLabel"] == "use 6"


def test_a_boolean_suggestion_reads_as_an_instruction_not_a_value(adv):
    """"use true" reads as a bug."""
    assert adv["apply_button_bool"]["label"] == "turn on"


def test_a_line_with_nothing_to_suggest_offers_no_button(adv):
    assert adv["lines"]["history_max_tokens"]["applyLabel"] is None


def test_the_apply_button_writes_the_control_it_names(adv):
    assert adv["apply_button"] == {"before": "0.9", "after": "0.37"}
    assert adv["apply_button_bool"]["after"] is True


# ── a preset applies everything it names ─────────────────────────────────────
def test_a_preset_writes_every_value_it_names_not_only_its_diff(adv):
    """The shipped defect: values are diffed against the SAVED config, so a control the
    user had already dragged was skipped. Drag Top-K to 18, apply a preset naming 5, and
    it stayed at 18 while the toast said the preset had been applied.

    hybrid_search is the sharper case — it is in the preset's values and NOT in its
    changes, so a diff-only apply leaves it false with nothing on screen to say so.
    """
    before, after = adv["preset_apply"]["before"], adv["preset_apply"]["after"]
    assert before["top_k"] == 18 and after["top_k"] == 5
    assert before["hybrid"] is False and after["hybrid"] is True
    assert before["topn"] == 3 and after["topn"] == 5
    assert after["thr"] == 0.48 and after["rerank"] is True


def test_applying_a_preset_stages_it_and_saves_nothing(adv):
    """It writes six controls at once. Doing that AND persisting, from one click on a
    button labelled with a single word, is not a decision the user has made."""
    assert adv["preset_apply"]["writes"] == []


def test_the_preview_names_every_change_before_anything_moves(adv):
    m = adv["preview_with_changes"]
    assert "Precision" in m["title"]
    assert m["rows"] == ["Top-K Chunks20 → 5", "Rerank retrieved chunksnot set → true"]
    assert m["buttons"] == ["Cancel", "Apply Precision"]


def test_a_setting_with_no_current_value_reads_as_not_set(adv):
    """String(undefined) renders "undefined" to the user."""
    assert any("not set →" in r for r in adv["preview_with_changes"]["rows"])


def test_the_preview_says_the_threshold_was_left_alone_and_why(adv):
    """Silently omitting it reads as "this preset does not change the threshold" rather
    than "there is not enough data to choose one"."""
    t = adv["preview_with_note"]["text"]
    assert "Similarity Threshold is left unchanged" in t
    assert "not enough to choose one" in t


def test_the_note_is_printed_as_written_not_prefixed(adv):
    """The modal used to prepend "Similarity Threshold is left unchanged" to EVERY note.
    A conflict note describes a threshold that WAS set, so that sentence appeared directly
    beside a diff row showing the threshold changing."""
    t = adv["preview_with_note"]["text"]
    assert t.count("Similarity Threshold is left unchanged") == 1, t


def test_the_preview_says_when_it_would_change_nothing(adv):
    m = adv["preview_no_changes"]
    assert "nothing to change" in m["text"]
    assert m["buttons"] == ["Close"], "an Apply button here does nothing and implies it does"


def test_the_preview_says_that_nothing_is_saved_yet(adv):
    assert "Save Settings" in adv["preview_with_changes"]["text"]


# ── the ⓘ ────────────────────────────────────────────────────────────────────
def test_the_info_popup_carries_the_measurement_and_names_it_as_local(adv):
    t = adv["info_modal"]["text"]
    assert "Across 340 queries" in t
    assert "0.37" in t
    assert "not a universal default" in t, "a suggestion read as a global recommendation"


def test_the_info_popup_claims_no_suggestion_when_there_is_none(adv):
    assert "Suggested for this deployment" not in adv["info_modal_nosuggest"]["text"]


def test_the_info_button_stays_out_of_the_controls_accessible_name(adv):
    """The ⓘ inside a <label> makes the field announce as "Top-K Chunks i"."""
    for key, name in adv["names"].items():
        if key.startswith("_"):
            continue
        assert name and not name.rstrip().endswith("i"), (key, name)
        assert "About" not in name, (key, name)


def test_every_advice_control_has_an_explanation(adv):
    assert adv["names"]["_info_count"] == 7


def test_the_hit_targets_are_large_enough_to_press(adv):
    """WCAG 2.5.8 asks 24x24. The ⓘ is 20 with margin around it; the apply button, which
    changes a setting, is the one that must clear it outright."""
    assert adv["names"]["_apply_min"] >= 24
    assert adv["names"]["_info_min"] >= 20


# ── _setByPath across the control types ──────────────────────────────────────
def test_a_value_reaches_a_checkbox_a_number_and_a_slider(adv):
    s = adv["set_by_path"]
    assert s["checkbox"] is True
    assert s["number"] == "7"
    assert s["range"] == "0.44"
    # setRange also maintains the number printed beside the slider. Assigning .value
    # moves the thumb and leaves that readout showing the old figure, so the panel
    # states two different thresholds at once.
    assert s["rangeReadout"] == "0.44", s["rangeReadout"]


def test_a_path_this_build_does_not_expose_is_ignored_quietly(adv):
    """A preset naming a control an older build lacks must not abort the rest of it."""
    assert adv["set_by_path"]["unknownPathThrew"] is False


# ── the server's strings are data ────────────────────────────────────────────
def test_a_headline_from_the_server_cannot_execute(adv):
    """Instance names reach these strings, and the advice is built from them."""
    e = adv["escaping"]
    assert e["xss"] is False and e["xss_after_wait"] is False
    assert e["imgs"] == 0, "markup from a headline was parsed as markup"
    assert e["rendered"] == "<img src=x onerror=window.__XSS=1>", "shown verbatim as text"


# ── how much of the traffic these numbers speak for ──────────────────────────
def test_the_panel_says_when_most_questions_were_never_measured(adv):
    """Retrieval does not run on a cache hit, so a replayed question contributes no
    score. At a 90% hit rate it takes 200 questions to reach the 20 the verdict needs,
    and without this the user cannot tell a quiet deployment from a well-cached one."""
    c = adv["coverage"]
    assert c and "340 of 1,420" in c["text"]
    assert "answered from cache" in c["text"]


def test_coverage_is_provenance_and_carries_no_verdict_colour(adv):
    """A colour implies a direction, and there is no measured basis for calling a cache
    hit rate too high or too low — that would be the table of good values this feature
    exists to avoid."""
    assert adv["coverage"]["colour"] == "rgb(110, 110, 115)"


def test_coverage_is_read_before_the_numbers_it_qualifies(adv):
    assert adv["coverage"]["beforePresets"] is True


# ── the per-corpus table ─────────────────────────────────────────────────────
def test_the_info_panel_lists_every_corpus_not_just_the_two_named(adv):
    """The headline can only name the two corpora that define a disagreement. Everything
    else — including the ones that do not vote — has to be visible somewhere."""
    t = adv["corpus_table"]["text"]
    for name in ("Ubuntu", "saved-answers", "default"):
        assert name in t, name


def test_each_corpus_shows_what_it_gets_now_and_what_it_needs(adv):
    t = adv["corpus_table"]["text"]
    assert "2%" in t and "86%" in t          # clearing now
    assert "0.30" in t and "0.62" in t       # needs


def test_a_corpus_that_cannot_vote_says_why(adv):
    t = adv["corpus_table"]["text"]
    assert "never matched" in t              # default: no vector candidate, ever
    assert "not enough data yet" in t        # the thin one


def test_only_the_per_corpus_setting_gets_a_per_corpus_table(adv):
    """top_k is judged on merged ranks, so a per-corpus table there would be reporting
    the merge order rather than the corpus."""
    assert "adv-corpora" not in adv["corpus_table_topk"]["html"]


def test_a_corpus_name_cannot_inject_markup(adv):
    """Instance names are user-chosen and reach this table verbatim."""
    e = adv["escaping"]
    assert e["xss"] is False and e["xss_after_wait"] is False


# ── the evidence, drawn ──────────────────────────────────────────────────────
def test_the_score_distribution_is_shown_not_just_summarised(adv):
    """"Only 12% of queries clear this" pasted into a ticket transmits a mood. A
    histogram with a marker is something the reader can check. raw_hist was computed,
    persisted and shipped on every tab entry, and rendered nowhere."""
    h = adv["hist"]
    assert h and h["bars"] == 20
    assert sum(h["heights"]) > 0
    assert max(h["heights"]) > min(x for x in h["heights"] if x >= 0)


def test_each_bar_names_its_bucket_and_its_count(adv):
    for t in adv["hist"]["titles"]:
        assert "searches" in t and "–" in t


def test_the_evidence_is_read_before_the_verdict_drawn_from_it(adv):
    assert adv["hist"]["drawnBeforeVerdict"] is True


def test_nothing_is_drawn_when_nothing_was_measured(adv):
    """An empty axis is not evidence; it is decoration that implies a measurement."""
    assert adv["hist_empty"] == 0


# ── the slider reads live ────────────────────────────────────────────────────
def test_the_marker_follows_the_slider(adv):
    at = adv["hist"]["at"]
    assert at["0.1"]["left"] == "10%"
    assert at["0.5"]["left"] == "50%"
    assert at["0.9"]["left"] == "90%"


def test_the_slider_says_what_the_value_under_it_costs(adv):
    """Before this, clicking "use 0.47" moved the slider and left the line reading "Only
    16% of queries clear this / use 0.47" byte-identical — the word "this" had no
    referent on screen."""
    at = adv["hist"]["at"]
    pcts = [int(at[k]["now"].split("—")[1].split("%")[0]) for k in ("0.1", "0.5", "0.9")]
    assert pcts == sorted(pcts, reverse=True), pcts
    assert pcts[0] > pcts[-1], "the readout does not change as the slider moves"


def test_the_readout_names_the_corpus_it_speaks_for(adv):
    assert "on Ubuntu" in adv["hist"]["at"]["0.5"]["now"]


# ── a diagnosis someone else can read ────────────────────────────────────────
def test_the_diagnosis_carries_the_numbers_not_a_screenshot(adv):
    t = adv["diagnosis"]["copied"]
    assert t and "VisualWeaver RAG diagnosis" in t
    for token in ("similarity_threshold", "top_k", "suggested:", "Per corpus:",
                  "Score distribution"):
        assert token in t, token


def test_the_diagnosis_states_what_it_does_not_cover(adv):
    """Without it the reader assumes the numbers describe all traffic."""
    assert "answered from cache" in adv["diagnosis"]["copied"]


def test_the_diagnosis_names_every_corpus_including_the_silent_ones(adv):
    t = adv["diagnosis"]["copied"]
    assert "no_signal" in t and "thin" in t


def test_settings_with_nothing_measurable_are_left_out_of_the_diagnosis(adv):
    """chunk_size and chunk_overlap carry "Not measurable here" — repeating that three
    times in a pasted report is noise."""
    t = adv["diagnosis"]["copied"]
    assert "chunk_size" not in t and "chunk_overlap" not in t


def test_the_copy_button_survives_the_advice_loading(adv):
    """It shares the .preset-btn class, and _renderRagPresets clears those — so it
    vanished the first time the advice arrived."""
    assert adv["diagnosis"]["hasButton"] is True
    assert adv["diagnosis"]["buttonKeepsPresetsFirst"] is True


# ── acting on the advice changes what the panel shows ────────────────────────
def test_taking_the_advice_moves_the_chart_with_the_slider(adv):
    """setRange assigns .value and dispatches nothing, so the marker and the cost
    readout — both driven by the slider's input event — stayed on the pre-click value.
    Clicking "use 0.37" moved the slider to 0.37 and left the chart saying 0.62."""
    b, a = adv["after_apply"]["before"], adv["after_apply"]["after"]
    assert b["slider"] != a["slider"]
    assert b["mark"] != a["mark"]
    assert a["mark"] == f"{round(float(a['slider']) * 100)}%"
    assert a["now"].startswith(f"{float(a['slider']):.2f}")


def test_taking_the_advice_marks_the_verdicts_as_measured_before_it(adv):
    """They describe the SAVED configuration. Leaving them at full strength meant a red
    "Only 12% of queries clear this" stood beside a slider the user had just moved on
    that very advice."""
    assert not any(adv["after_apply"]["before"]["stale"])
    assert all(adv["after_apply"]["after"]["stale"])


def test_editing_an_advised_control_by_hand_stales_it_too(adv):
    assert adv["after_manual_edit"] == {"before": False, "after": True}


# ── the template selection survives the list changing under it ───────────────
def test_deleting_a_template_does_not_rebind_the_chat_to_another_one(adv):
    """Option values are array indices and a delete re-assigns them, so restoring the
    index silently swapped the chat to whichever template slid into that slot — a
    different system prompt and a different cache scope, with nothing on screen to say
    so. Before the restore existed this reset to Default: visibly wrong, so the user
    re-picked. Keeping the index made it invisible instead."""
    r = adv["template_rebind"]
    assert r["before"] == r["after"] == "Medical", r
