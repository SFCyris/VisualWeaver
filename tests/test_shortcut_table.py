# SPDX-License-Identifier: AGPL-3.0-or-later
"""The in-app keyboard shortcut table, checked against the bindings it describes.

Settings → General lists the shortcuts. It is the only place in the app that says what
they are, and it drifted: ``⌘/Ctrl+F`` was rebound to ``Shift+⌘/Ctrl+F`` so the browser
could keep plain find, the release notes said so, and the table kept advertising the old
combination for two releases. Nothing could catch it — the table is markup and the
binding is a keydown handler, and no test looked at both.

These parse each from the real source and compare them, in both directions: every
documented shortcut must exist, and every modifier-key binding must be documented.
"""
import pathlib
import re

import pytest

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _table() -> dict[str, str]:
    """{"Shift+⌘/Ctrl+F": "Search conversations", ...} from the Settings table."""
    html = _html()
    start = html.index("Keyboard Shortcuts")
    block = html[start:html.index("</table>", start)]
    rows = re.findall(r'<span class="kbd">([^<]+)</span></td><td>([^<]+)</td>', block)
    assert rows, "the shortcut table is gone or its markup changed"
    return {k.strip(): v.strip() for k, v in rows}


def _fn(name: str) -> str:
    html = _html()
    i = html.index(f"function {name}(")
    depth, j, start = 0, html.index("{", i), html.index("{", i)
    while j < len(html):
        if html[j] == "{":
            depth += 1
        elif html[j] == "}":
            depth -= 1
            if depth == 0:
                return html[start:j + 1]
        j += 1
    raise AssertionError(f"could not slice {name}")


def _mod_bindings() -> dict[str, bool]:
    """{key: requires_shift} for every ⌘/Ctrl binding in setupKeyboardShortcuts."""
    src = _fn("setupKeyboardShortcuts")
    out = {}
    for line in src.splitlines():
        if "mod&&" not in line:
            continue
        keys = re.findall(r"e\.key==='([A-Za-z])'", line)
        if not keys:
            continue
        out[keys[0].lower()] = "shiftKey" in line
    assert out, "no modifier bindings found — the handler moved"
    return out


# ── the documented combination must be the bound one ─────────────────────────
def test_search_is_documented_with_its_shift_modifier():
    """Plain ⌘/Ctrl+F is deliberately left to the browser's own find. The table said
    otherwise for two releases."""
    doc = _table()
    assert "⌘/Ctrl+Shift+F" in doc, doc
    assert _mod_bindings()["f"] is True, "the search binding no longer requires Shift"


def test_plain_ctrl_f_is_listed_as_the_browsers_own():
    """Listing it is the point: the app deliberately gives that key back, and a reader
    who only sees the Shift variant cannot tell whether plain find still works."""
    doc = _table()
    assert "⌘/Ctrl+F" in doc, doc
    assert "not intercept" in doc["⌘/Ctrl+F"], doc["⌘/Ctrl+F"]


def test_the_in_app_table_matches_the_one_in_DOCS():
    """These are the only two places the shortcuts are written down, and they drifted:
    DOCS was right about Shift+⌘/Ctrl+F while the app advertised the old combination."""
    import re
    docs = pathlib.Path(__file__).resolve().parents[1] / "DOCS.md"
    md = docs.read_text(encoding="utf-8")
    block = md[md.index("## Keyboard Shortcuts"):]
    # not block.index("---"): the table's own |---|---| separator matches first and
    # truncates the block before a single data row
    block = block[:block.index("\n---\n")]
    rows = dict(re.findall(r"^\| `([^`]+)` \| (.+?) \|$", block, re.M))
    assert rows, "the DOCS shortcut table is gone or its markup changed"
    assert set(rows) == set(_table()), (
        f"only in DOCS: {set(rows) - set(_table())}; only in app: {set(_table()) - set(rows)}")


def test_settings_is_documented_without_a_shift_modifier():
    doc = _table()
    assert "⌘/Ctrl+K" in doc
    assert _mod_bindings()["k"] is False, "the settings binding now requires Shift"


@pytest.mark.parametrize("combo,fragment", [
    ("Enter", "e.key==='Enter'&&!e.shiftKey"),
    ("Shift+Enter", "e.key==='Enter'&&!e.shiftKey"),
    ("⌘/Ctrl+Enter", "(e.metaKey||e.ctrlKey)&&e.key==='Enter'"),
])
def test_the_composer_keys_are_documented_and_bound(combo, fragment):
    """Enter sends and Shift+Enter breaks the line — the two most-used keys in the app,
    and neither was in the table."""
    assert combo in _table(), f"{combo} is not documented"
    assert fragment in _fn("handleKey").replace(" ", ""), f"{combo} is not bound"


# ── and nothing bound may go undocumented ────────────────────────────────────
def test_every_modifier_binding_appears_in_the_table():
    """The direction that would have caught the drift: a binding that exists but is not
    described. Rebinding search without touching the table left it silently wrong."""
    doc = " ".join(_table())
    missing = []
    for key, needs_shift in _mod_bindings().items():
        combo = "⌘/Ctrl+" + ("Shift+" if needs_shift else "") + key.upper()
        if combo not in doc:
            missing.append(combo)
    assert not missing, f"bound but undocumented: {missing}"


def test_escape_is_documented():
    assert "Esc" in _table()
    assert "e.key==='Escape'" in _fn("setupKeyboardShortcuts").replace(" ", "")
