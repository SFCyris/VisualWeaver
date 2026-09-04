# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the Base Instruction drift notice actually renders.

The backend half is covered by test_base_instruction_drift.py; this is the half
that decides whether a user is interrupted. It got that decision wrong once
already — gating on `differs` rather than `stale` told anyone who had customised
a CURRENT instruction that it was out of date, directly above the button that
discards their wording — and no test could see it, because the renderer had none.

Runs under node with a small DOM stub: the function only touches getElementById,
hidden, className, setAttribute and innerHTML.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"

_DOM = """
const el = {hidden:true, className:'', innerHTML:'', _attrs:{},
            setAttribute(k,v){this._attrs[k]=v;}};
const document = {getElementById: id => id === 'base-instruction-drift' ? el : null};
"""


def _render(drift):
    if not shutil.which("node"):
        pytest.skip("node not available")
    src = _INDEX.read_text(encoding="utf-8")
    js = (_DOM
          + extract_js_function(src, "escHtml") + "\n"
          + extract_js_function(src, "renderBaseInstructionDrift") + "\n"
          + f"renderBaseInstructionDrift({json.dumps(drift)});\n"
          + "console.log(JSON.stringify({hidden:el.hidden, cls:el.className,"
            " role:el._attrs.role, html:el.innerHTML,"
            " text:el.innerHTML.replace(/<[^>]*>/g,'')}));\n")
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1200]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def _drift(**over):
    d = {"differs": True, "stale": True, "missing_lanes": [], "missing_options": [],
         "missing_options_total": 0, "missing_names": [], "missing_names_total": 0,
         "stored_chars": 11893, "shipped_chars": 12563}
    d.update(over)
    return d


# ── when it must stay quiet ──────────────────────────────────────────────────
def test_nothing_is_shown_when_the_instruction_is_not_stale():
    assert _render(_drift(stale=False))["hidden"] is True


def test_a_customised_but_current_instruction_is_not_interrupted():
    """The regression. `differs` is true for any edit; only `stale` means
    something shipped is missing, and only that is worth a warning above a
    button that replaces the user's text."""
    r = _render(_drift(differs=True, stale=False))
    assert r["hidden"] is True and r["html"] == ""


def test_a_missing_field_from_an_older_server_is_not_an_error():
    assert _render(None)["hidden"] is True
    assert _render({})["hidden"] is True


# ── when it must speak ───────────────────────────────────────────────────────
def test_missing_names_are_listed_before_options():
    """A missing preset name is the difference between asking for something the
    app draws and asking for something it does not — more use than a JSON key."""
    r = _render(_drift(missing_names=["hilbert", "peano"], missing_names_total=2,
                       missing_options=["order"], missing_options_total=1))
    assert r["hidden"] is False
    t = r["text"]
    assert t.index("hilbert") < t.index("order"), t


def test_the_count_shown_is_the_true_total_not_the_capped_list():
    """The server caps the list at 40. Counting the list instead of the total
    understated it by 19."""
    r = _render(_drift(missing_options=[f"o{i}" for i in range(40)],
                       missing_options_total=59))
    assert "and 51 more" in r["text"], r["text"]


def test_it_does_not_claim_the_instruction_is_old():
    """"older than the app" is a diagnosis, and false for anyone who removed a
    section deliberately. The notice states what is absent, and that the app now
    fills it in, instead."""
    t = _render(_drift(missing_lanes=["abc"]))["text"].lower()
    assert "older than the app" not in t
    assert "predates" in t and "adds the missing ones to every turn" in t
    assert "reset to shipped default" in t


def test_it_is_marked_as_a_note_and_uses_the_shared_warning_style():
    r = _render(_drift(missing_names=["hilbert"], missing_names_total=1))
    assert r["role"] == "note"
    assert "notice-warn" in r["cls"], r["cls"]


def test_every_interpolated_value_is_escaped():
    """The lists come from the server, but they are derived from user-editable
    config text, so nothing may reach innerHTML unescaped."""
    r = _render(_drift(missing_names=["<img src=x onerror=alert(1)>"],
                       missing_names_total=1))
    assert "<img" not in r["html"], r["html"]
    assert "&lt;img" in r["html"]


def test_the_character_delta_is_omitted_when_the_copy_is_not_shorter():
    r = _render(_drift(stored_chars=13000, shipped_chars=12563,
                       missing_names=["hilbert"], missing_names_total=1))
    assert "characters shorter" not in r["text"], r["text"]
