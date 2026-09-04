# SPDX-License-Identifier: AGPL-3.0-or-later
"""A saved base instruction that predates a visual lane is topped up at send time.

A base_instruction saved before a lane existed shadows it forever: the model is
never told the fence exists, so it draws the subject with whatever it already
knows — a circuit as a mermaid graph. ``compose_system_prompt`` now appends the
missing lanes' own bullets (and the routing rule) from the shipped default to what
the model receives, without touching the user's saved prose. These tests exercise
the real ``config`` module against a synthesised stale instruction.
"""
import pytest

from visualweaver import config, constants, state


@pytest.fixture
def cfg(monkeypatch):
    """A saved base instruction that never heard of the seven newer lanes: the
    shipped default with those bullets and the routing rule stripped out."""
    lanes = ["circuit", "scene", "ode", "plotly", "sequence", "phylo", "reaction"]
    kept = []
    for ln in constants.DEFAULT_BASE_INSTRUCTION.split("\n"):
        if ln.strip().startswith("Routing —"):
            continue
        if any(ln.startswith(f"- ```{n} —") for n in lanes):
            continue
        # the mermaid/circuit cross-references reintroduce "```circuit"; drop them
        # so the stale copy genuinely lacks every one of the seven fences
        if "```circuit" in ln or "```scene" in ln:
            ln = ln.split(" (")[0]
        kept.append(ln)
    stale = "\n".join(kept)
    monkeypatch.setitem(state._config, "base_instruction", stale)
    monkeypatch.setitem(state._config, "visual_instructions", True)
    return {"stale": stale, "lanes": lanes}


def test_a_stale_instruction_is_topped_up_with_the_missing_lane_bullets(cfg):
    missing = config.base_instruction_drift(cfg["stale"])["missing_lanes"]
    assert set(cfg["lanes"]) <= set(missing), missing   # the fixture really is stale

    prompt = config.compose_system_prompt(None)
    for lane in cfg["lanes"]:
        assert f"```{lane}" in prompt, f"the model was never told about ```{lane}"
    assert "Routing —" in prompt and "NEVER draw them as a mermaid" in prompt
    # the saved prose is preserved, the supplement is appended after it
    assert prompt.startswith(cfg["stale"][:80])
    assert prompt.index("ALSO AVAILABLE") > len(cfg["stale"]) - 200


def test_the_saved_prose_is_left_untouched(cfg):
    prompt = config.compose_system_prompt(None)
    assert cfg["stale"] in prompt, "the user's saved text must survive verbatim"
    # nothing is duplicated: a lane already present is not re-added
    assert prompt.count("```calc") == cfg["stale"].count("```calc")


def test_no_top_up_when_the_instruction_already_mentions_every_lane(monkeypatch):
    monkeypatch.setitem(state._config, "base_instruction", constants.DEFAULT_BASE_INSTRUCTION)
    monkeypatch.setitem(state._config, "visual_instructions", True)
    assert config._visual_lane_supplement(constants.DEFAULT_BASE_INSTRUCTION) == ""
    prompt = config.compose_system_prompt(None)
    # the shipped default in, the shipped default out (plus nothing)
    assert "ALSO AVAILABLE" not in prompt


def test_no_top_up_when_visual_blocks_are_switched_off(cfg, monkeypatch):
    """A text-only deployment cuts the whole visual section to save billed tokens;
    adding one lane back would contradict that."""
    monkeypatch.setitem(state._config, "visual_instructions", False)
    assert config._visual_lane_supplement(cfg["stale"]) == ""
    prompt = config.compose_system_prompt(None)
    assert "ALSO AVAILABLE" not in prompt and "```circuit" not in prompt


def test_the_client_template_still_comes_after_the_top_up(cfg):
    prompt = config.compose_system_prompt("Answer only in French.")
    assert "Answer only in French." in prompt
    assert prompt.index("ALSO AVAILABLE") < prompt.index("Answer only in French.")


def test_an_empty_saved_instruction_is_not_topped_up(monkeypatch):
    monkeypatch.setitem(state._config, "base_instruction", "")
    monkeypatch.setitem(state._config, "visual_instructions", True)
    # nothing saved: base_instruction_drift returns no missing lanes, so no supplement
    assert config._visual_lane_supplement("") == ""


def test_a_non_string_saved_instruction_does_not_crash_the_turn(monkeypatch):
    monkeypatch.setitem(state._config, "base_instruction", {"oops": 1})
    monkeypatch.setitem(state._config, "visual_instructions", True)
    prompt = config.compose_system_prompt(None)   # must not raise
    assert isinstance(prompt, str) and prompt
