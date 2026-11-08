# SPDX-License-Identifier: AGPL-3.0-or-later
"""The composer's `#` mention parser (_mentionAt, _mentionMatches, _removeMention), run as
real JavaScript under node.

Typing `#` in the message box offers the knowledge bases; picking one restricts that
question to it. The parser decides when a `#` starts a mention (not in "C#", "issue#4" or a
"##" heading), which knowledge bases the typed text matches, and what is left of the
question once the typed `#query` is replaced by a chip.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _run(expr: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in ("_mentionAt", "_mentionMatches", "_removeMention"))
    r = run_node(f"{src}\nconsole.log(JSON.stringify({expr}));")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _at(text, caret=None):
    caret = len(text) if caret is None else caret
    return _run(f"_mentionAt({json.dumps(text)},{caret})")


@pytest.mark.parametrize("text,want", [
    ("#", {"start": 0, "query": ""}),
    ("#man", {"start": 0, "query": "man"}),
    ("what does #man", {"start": 10, "query": "man"}),
    ("line one\n#faq", {"start": 9, "query": "faq"}),
    ("tab\t#x", {"start": 4, "query": "x"}),
    ("über #ünï", {"start": 5, "query": "ünï"}),
])
def test_a_hash_at_the_start_or_after_a_space_starts_a_mention(text, want):
    assert _at(text) == want


@pytest.mark.parametrize("text", ["C#", "issue#4", "a#b", "##", "# heading", "#man ual", "hello", "", "#a#b"])
def test_other_hashes_are_not_mentions(text):
    assert _at(text) is None


def test_only_the_text_up_to_the_caret_counts():
    assert _at("ask #manuals now", 8) == {"start": 4, "query": "man"}
    assert _at("ask #manuals now", 16) is None          # caret after the space
    assert _at("ask #manuals now", 3) is None


def _match(q, names, exclude=(), mx=8):
    return _run(f"_mentionMatches({json.dumps(q)},{json.dumps(names)},{json.dumps(list(exclude))},{mx})")


def test_matches_are_case_insensitive_prefix_first_then_list_order():
    names = ["faq", "Manuals", "user manual", "Policies", "Man pages"]
    assert _match("man", names) == ["Manuals", "Man pages", "user manual"]
    assert _match("MAN", names) == ["Manuals", "Man pages", "user manual"]
    assert _match("", names) == names                    # just "#": every knowledge base
    assert _match("zzz", names) == []


def test_already_mentioned_ones_and_non_names_are_left_out_and_the_list_is_capped():
    assert _match("", ["a", "b", "c"], exclude=["b"]) == ["a", "c"]
    assert _match("", ["a", None, 3, "b"]) == ["a", "b"]
    assert _match("", [f"k{i}" for i in range(20)], mx=8) == [f"k{i}" for i in range(8)]


def test_names_with_markup_come_back_as_the_same_strings():
    """The menu renders them with textContent; the parser must not alter them."""
    evil = '<img src=x onerror="alert(1)">'
    assert _match("img", [evil]) == [evil]


def _rm(text, start, end):
    return _run(f"_removeMention({json.dumps(text)},{start},{end})")


def test_picking_removes_the_typed_mention_and_keeps_the_question():
    assert _rm("what is #man", 8, 12) == {"text": "what is ", "caret": 8}
    assert _rm("#man what is it", 0, 4) == {"text": "what is it", "caret": 0}
    assert _rm("what #man is it", 5, 9) == {"text": "what is it", "caret": 5}
    assert _rm("#man", 0, 4) == {"text": "", "caret": 0}
    assert _rm("a\n#faq\nb", 2, 6) == {"text": "a\n\nb", "caret": 2}   # line breaks stay
