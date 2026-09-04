# SPDX-License-Identifier: AGPL-3.0-or-later
"""A saved Base Instruction shadows the shipped one, permanently and silently.

`compose_system_prompt` sends `config["base_instruction"]`. The moment Settings is
saved, that stored copy replaces the shipped default for good — so every block,
option and preset added afterwards stops reaching the model, while answers carry
on rendering and nothing reports a problem. The instance this was written on had
a copy 670 characters short of the shipped one, missing the `order` and `plane`
options for the fractal lane, weeks after those shipped.

The hard part is not detecting a difference — it is detecting only differences
worth interrupting for. Customising this field is its documented purpose, and the
remedy the notice points at (Reset to shipped default) DISCARDS that
customisation. A notice that fires on any edit is therefore worse than none:
`stale` must mean "shipped capability is missing", never merely "not identical".
"""
import pathlib
import re

import pytest

from visualweaver import config, constants, state


def _shipped() -> str:
    return constants.DEFAULT_BASE_INSTRUCTION


# ── the flag the UI actually reads ───────────────────────────────────────────

def test_a_customised_but_current_instruction_is_not_stale():
    """The regression this file exists for.

    An earlier version asserted only that the two MISSING lists were empty, and
    passed — while `differs` was True and the UI, which reads `differs`, told the
    user their current instruction was "older than the app" and advised replacing
    it. Assert the field the UI branches on.
    """
    d = config.base_instruction_drift(_shipped() + "\n\nAlways answer in British English.")
    assert d["differs"] is True, "it genuinely is not byte-identical"
    assert d["stale"] is False, "but nothing shipped is missing from it"
    assert d["missing_lanes"] == [] and d["missing_options"] == []


def test_stale_is_true_only_when_something_shipped_is_absent():
    assert config.base_instruction_drift(_shipped())["stale"] is False
    assert config.base_instruction_drift("")["stale"] is False
    assert config.base_instruction_drift("Be terse.")["stale"] is True


# ── what it reports ──────────────────────────────────────────────────────────

def test_a_missing_block_is_named():
    stripped = "\n".join(l for l in _shipped().split("\n") if "```fractal" not in l)
    d = config.base_instruction_drift(stripped)
    # dropping every line that mentions ```fractal also drops the ```geometry
    # bullet, which cross-references it — assert the exact set, not membership,
    # so a change in what the strip removes is visible.
    assert d["missing_lanes"] == ["fractal", "geometry"], d["missing_lanes"]
    assert d["stale"] is True


def test_a_missing_option_is_named_even_when_the_block_is_present():
    """The subtle case, and the one that actually happened: the lane is still
    described, but options added to it are not, so the model can only reach them
    by guessing."""
    stale = _shipped().replace('"plane":"xy"|"xz"|"yz"', "")
    d = config.base_instruction_drift(stale)
    assert d["missing_lanes"] == [], "the lane itself is still described"
    assert d["missing_options"] == ["plane"], d["missing_options"]


def test_deleting_the_preset_names_is_detected():
    """The headline case, and the one an earlier version of this function missed
    entirely.

    Removing all fourteen fractal presets added in 1.11 left `stale` False,
    because the names are neither fence names nor JSON keys — they are an
    enumerated vocabulary. That is exactly the incident the feature exists for:
    the lane could draw a Hilbert curve, the saved instruction never said so, and
    nothing anywhere reported it.
    """
    gone = _shipped()
    added = ("hilbert", "peano", "moore", "gosper", "levy", "arrowhead", "tree",
             "carpet", "lorenz", "rossler", "thomas", "halvorsen", "clifford", "dejong")
    for n in added:
        gone = re.sub(rf"\b{n}\b,?\s*", "", gone)

    d = config.base_instruction_drift(gone)
    assert d["stale"] is True, "deleting every new preset name must register"
    assert set(d["missing_names"]) == set(added), sorted(d["missing_names"])
    # neither of the older signals sees this at all — that is why it was added
    assert d["missing_lanes"] == [] and d["missing_options"] == []


def test_enumerated_names_exclude_parenthetical_asides():
    """"koch (Koch snowflake)" contributes `koch`, not `snowflake`; the geometry
    list's "(centre+radius)" asides are not element types."""
    d = config.base_instruction_drift("Be terse.")
    for noise in ("snowflake", "centre", "radii", "accepted"):
        assert noise not in d["missing_names"], f"{noise!r} is prose, not a capability name"
    for real in ("hilbert", "lorenz", "mandelbrot", "polygon", "segment"):
        assert real in d["missing_names"], f"{real!r} is an enumerated name and must be reported"


def test_the_reported_name_count_is_the_true_total():
    d = config.base_instruction_drift("Be terse.")
    assert len(d["missing_names"]) <= config._DRIFT_LIST_CAP
    assert d["missing_names_total"] >= len(d["missing_names"])


def test_only_json_keys_count_as_options_not_example_values():
    """Matching any quoted word swept up the example VALUES — "bar" from the
    chart example, "api"/"client"/"svc" from the network one, "indefinite" from
    an SVG attribute. Listed under "options it never names" those are noise, and
    a notice that cries wolf gets dismissed."""
    d = config.base_instruction_drift("Be terse.")
    for value in ("bar", "api", "client", "svc", "indefinite"):
        assert value not in d["missing_options"], f"{value!r} is an example value, not an option"
    for key in ("order", "plane", "palette", "iter"):
        assert key in d["missing_options"], f"{key!r} is a real option and must be reported"


def test_language_is_never_reported_as_a_missing_block():
    """"```language" appears in the prose as the placeholder for an ordinary code
    fence. Reporting it named a capability that has never existed."""
    d = config.base_instruction_drift("Be terse.")
    assert "language" not in d["missing_lanes"]
    assert set(d["missing_lanes"]) <= set(constants.RENDERABLE_FENCES)


def test_the_reported_option_count_is_the_true_total_not_the_capped_list():
    """The UI says "and N more" from this number. Reporting the capped length
    would have understated it by 19."""
    d = config.base_instruction_drift("Be terse.")
    assert len(d["missing_options"]) <= config._DRIFT_LIST_CAP
    assert d["missing_options_total"] >= len(d["missing_options"])
    assert d["missing_options_total"] > config._DRIFT_LIST_CAP, \
        "this input should exceed the cap, otherwise the test proves nothing"


# ── inputs that must not break the settings screen ───────────────────────────

@pytest.mark.parametrize("bad", [42, 3.5, {"a": 1}, ["x"], True, None])
def test_a_non_string_base_instruction_cannot_break_the_config_route(bad, cfg):
    """base_instruction_drift runs inside GET /api/config, the route the whole UI
    bootstraps from. A hand-edited config.json holding a non-string here used to
    raise AttributeError on .strip() and take the settings screen down."""
    state._config["base_instruction"] = bad
    d = config.base_instruction_drift() if bad is None else config.base_instruction_drift(bad)
    assert d["stale"] is False and d["differs"] is False


def test_declining_the_visual_section_is_not_the_same_as_missing_it(cfg):
    """With the toggle off, everything after the marker is cut before sending, so
    it is declined rather than absent. Comparing against the full shipped text
    produced a permanent, undismissable notice listing 25 blocks."""
    pre = _shipped()[: _shipped().index(constants.VISUAL_SECTION_MARKER)]
    state._config["visual_instructions"] = False
    assert config.base_instruction_drift(pre)["stale"] is False
    state._config["visual_instructions"] = True
    assert config.base_instruction_drift(pre)["stale"] is True


# ── the route really returns it ──────────────────────────────────────────────

def test_the_config_route_returns_the_drift_with_the_shape_the_ui_reads():
    """An earlier version asserted the identifier appeared in the module source,
    which would pass on a comment or on dead code and says nothing about the
    response."""
    from visualweaver import routes_settings

    body = routes_settings.api_get_config()
    assert "base_instruction_drift" in body
    d = body["base_instruction_drift"]
    assert set(d) == {"differs", "stale", "missing_lanes",
                      "missing_options", "missing_options_total",
                      "missing_names", "missing_names_total",
                      "stored_chars", "shipped_chars"}, sorted(d)
    assert isinstance(d["stale"], bool) and isinstance(d["missing_lanes"], list)


def test_the_renderable_fence_list_matches_the_frontend():
    """constants.RENDERABLE_FENCES is what separates a real block from a word
    that looks like one. If a lane is added to RICH_LANES and not here, the
    notice stops being able to report it as missing."""
    from _jsrun import rich_lane_names

    index = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"
    lanes = set(rich_lane_names(index))
    assert lanes, "RICH_LANES extraction failed"
    missing = sorted(lanes - constants.RENDERABLE_FENCES)
    assert not missing, f"registered lanes absent from RENDERABLE_FENCES: {missing}"


@pytest.mark.parametrize("bad", [42, 3.5, {"a": 1}, ["x"], True])
def test_a_non_string_base_instruction_cannot_break_a_chat_turn(bad, cfg):
    """compose_system_prompt runs on EVERY turn, and was left unguarded when the
    config route was hardened — which moved the symptom from "Settings will not
    open" to "the app answers nothing", with the pointer to the cause removed."""
    from visualweaver import config, state

    state._config["base_instruction"] = bad
    prompt = config.compose_system_prompt(None)
    assert isinstance(prompt, str) and prompt.strip(), "a turn must still get a system prompt"
