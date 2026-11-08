# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citation parsing (_citeParse) and the per-answer grounding rule (_groundingOf), run as
real JavaScript under node.

Before 1.16.0 an answer said nothing about how well it was backed by its sources: a reply
that cited every paragraph and one that cited nothing looked the same, and a citation
number with no source behind it ([7] when five chunks were retrieved) was silently left
as plain text — so a fabricated reference read exactly like an ordinary bracket.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _run(body: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in ("_citeParse", "_groundingOf"))
    r = run_node(f"{src}\nconst P=_citeParse,G=_groundingOf;\n{body}")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _parse(text, max_n):
    return _run(f"console.log(JSON.stringify(P({json.dumps(text)},{max_n})))")


def _g(raw, max_n=3, rag_used=True, not_found=False):
    return _run(f"console.log(JSON.stringify(G({json.dumps(raw)},{max_n},"
                f"{json.dumps(rag_used) if rag_used is not None else 'undefined'},"
                f"{json.dumps(not_found)})))")


# ── parsing ──────────────────────────────────────────────────────────────────

def test_valid_and_grouped_markers_are_citations():
    assert _parse("A [1]. B [2, 3].", 3) == [
        {"text": "A "}, {"cite": [{"n": 1, "ok": True}]}, {"text": ". B "},
        {"cite": [{"n": 2, "ok": True}, {"n": 3, "ok": True}]}, {"text": "."}]


def test_a_number_with_no_source_is_a_broken_citation_not_text():
    """Old: [7] with five sources was left as plain text, indistinguishable from prose."""
    assert _parse("Claim [7].", 5) == [{"text": "Claim "}, {"cite": [{"n": 7, "ok": False}]},
                                        {"text": "."}]
    assert _parse("Mixed [2, 9].", 5)[1] == {"cite": [{"n": 2, "ok": True}, {"n": 9, "ok": False}]}
    # nothing retrieved at all: every number is broken
    assert _parse("Claim [1].", 0)[1] == {"cite": [{"n": 1, "ok": False}]}


def test_code_like_brackets_links_and_zero_are_not_citations():
    assert _parse("use argv[7] here", 5) == [{"text": "use argv[7] here"}]
    assert _parse("m[2][9] and f(x)[8]", 1) == [{"text": "m[2][9] and f(x)[8]"}]
    assert _parse("see [0] and [2](http://x)", 5) == [{"text": "see [0] and [2](http://x)"}]
    # a VALID number right after a word is still a citation, as it always was
    assert _parse("append-only[2].", 3)[1] == {"cite": [{"n": 2, "ok": True}]}


# ── grounding ────────────────────────────────────────────────────────────────

_GROUNDED = ("Streams in Redis are append-only data structures [1].\n\n"
             "Each entry gets a unique identifier based on time [2].")


def test_every_substantive_paragraph_cited_is_grounded():
    g = _g(_GROUNDED)
    assert g == {"state": "grounded", "units": 2, "cited": 2, "valid": 2, "invalid": 0}


def test_an_uncited_substantive_paragraph_makes_it_partly():
    g = _g(_GROUNDED + "\n\nIn general, most databases also offer some kind of log structure.")
    assert (g["state"], g["units"], g["cited"]) == ("partly", 3, 2)


def test_no_valid_citation_is_uncited():
    assert _g("Streams in Redis are append-only data structures for logs.")["state"] == "uncited"
    # citing only numbers that do not exist is not grounding
    g = _g("Streams in Redis are append-only data structures for logs [8].")
    assert (g["state"], g["valid"], g["invalid"]) == ("uncited", 0, 1)


def test_an_invalid_number_counts_against_an_otherwise_cited_answer():
    """Every paragraph cites, but one number points nowhere: not 'grounded'."""
    g = _g("Streams in Redis are append-only data structures [1].\n\n"
           "Each entry gets a unique identifier based on time [2, 7].")
    assert (g["state"], g["cited"], g["units"], g["invalid"]) == ("partly", 2, 2, 1)


def test_list_items_and_table_rows_are_units_and_headings_and_lead_ins_are_not():
    raw = ("## Summary of the findings in the documents\n\n"
           "The documents describe the following three properties:\n\n"
           "- Entries are appended at the end of the stream [1]\n"
           "- Consumers can read the stream in groups together [2]\n"
           "- Old entries can be trimmed by length or by identifier\n\n"
           "| Command | What it does in the stream |\n|---|---|\n"
           "| XADD | appends a brand new entry to the stream [3] |\n")
    g = _g(raw)
    # 3 list items + 1 table body row; the heading, the lead-in and the header row do not count
    assert (g["units"], g["cited"], g["state"]) == (4, 3, "partly")


def test_code_and_maths_do_not_count_either_way():
    raw = ("Use the XADD command to append an entry to a stream [1].\n\n"
           "```python\nr.xadd('s', {'a': 1})  # see [9], argv[1]\nprint('this is a long uncited line of code')\n```\n\n"
           "$$\\sum_i x_i = [2]$$\n\nInline `x[5]` is fine.")
    g = _g(raw)
    assert g == {"state": "grounded", "units": 1, "cited": 1, "valid": 1, "invalid": 0}


def test_a_short_cited_answer_is_grounded():
    assert _g("Yes [1].")["state"] == "grounded"


def test_distinct_states_without_retrieval_and_for_not_found():
    assert _g("Anything at all here.", 0, rag_used=False)["state"] == "norag"
    assert _g("**Not found in your documents.**", 0, rag_used=True, not_found=True)["state"] == "notfound"
    # searched, nothing retrieved: nothing could be cited — a neutral state, not "uncited"
    assert _g("A general answer from the model with no sources.", 0, rag_used=True)["state"] == "noretrieval"
    # not known (a turn stored before rag_used was recorded, with no chunks): no verdict
    assert _g("Old answer.", 0, rag_used=None) is None
