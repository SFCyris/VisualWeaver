# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second review of 1.16.0: the grounding badge and citation parsing (_citeParse,
_groundingOf), run as real JavaScript under node.

Before these fixes: an answer citing [1][2] when nothing at all was retrieved got the
neutral "– Nothing retrieved" badge (the made-up numbers only in its tooltip), and any
number more than 5 past the last source — "[9]" with 3 sources — was taken for a reference
quoted from a document and left as plain text, so a made-up citation was hidden.
"""
from test_uxfix_js import _g, _parse


# ── citing sources when nothing was retrieved ────────────────────────────────

def test_citing_sources_when_nothing_was_retrieved_is_not_neutral():
    g = _g("Streams keep their entries in insertion order [1] and trim them [2].", 0, rag_used=True)
    assert g["state"] == "missing" and g["invalid"] == 2
    assert _g("Streams keep their entries in insertion order [1][2].", 0, rag_used=True)["state"] == "missing"


def test_nothing_retrieved_and_nothing_cited_stays_neutral():
    assert _g("A general answer from the model with no sources.", 0, rag_used=True)["state"] == "noretrieval"


def test_a_quoted_reference_with_nothing_retrieved_does_not_count():
    g = _g('The paper says "streams scale linearly [12]" in its summary of results.', 0, rag_used=True)
    assert g["state"] == "noretrieval" and g["invalid"] == 0


# ── which far numbers are quoted references ──────────────────────────────────

def test_a_made_up_number_a_few_past_the_sources_is_a_broken_citation():
    """Old: [9] with 3 sources was plain text (more than 5 past the last one)."""
    assert _parse("Streams scale [9].", 3)[1] == {"cite": [{"n": 9, "ok": False}]}
    g = _g("Streams in Redis are append-only data structures [1] and scale well [9].", 3)
    assert (g["state"], g["invalid"]) == ("partly", 1)


def test_a_far_number_in_prose_is_a_broken_citation():
    assert _parse("As shown in [12], streams scale.", 5)[1] == {"cite": [{"n": 12, "ok": False}]}
    assert _parse("A [6].", 0)[1] == {"cite": [{"n": 6, "ok": False}]}


def test_a_far_number_inside_a_quotation_is_a_quoted_reference():
    for text in ('The paper: "as shown in [12], streams scale".', "It says “as shown in [12]”.",
                 "Il dit « voir [40] »."):
        assert _parse(text, 5) == [{"text": text}], text


def test_a_far_number_opening_a_reference_list_line_is_a_quoted_reference():
    text = "References:\n[12] Smith, J. Streams at scale. 2021."
    assert _parse(text, 5) == [{"text": text}]
    assert _parse("[12] Smith, J. Streams at scale.", 5) == [{"text": "[12] Smith, J. Streams at scale."}]


def test_a_number_not_beyond_twice_the_sources_is_never_a_quoted_reference():
    """Inside a quotation too: [7] with 5 sources is too close to be a document's own."""
    assert _parse('He said "see [7]".', 5)[1] == {"cite": [{"n": 7, "ok": False}]}
    assert _parse("[10] Smith, J.", 5)[0] == {"cite": [{"n": 10, "ok": False}]}


def test_after_a_closed_quotation_a_far_number_is_broken_again():
    assert _parse('He said "streams scale" and then [12].', 5)[1] == {"cite": [{"n": 12, "ok": False}]}
    # an apostrophe is not a quotation mark
    assert _parse("It's shown in [12].", 5)[1] == {"cite": [{"n": 12, "ok": False}]}


def test_a_far_number_grouped_with_a_valid_one_is_still_broken():
    assert _parse('"X [2, 40]".', 5)[1] == {"cite": [{"n": 2, "ok": True}, {"n": 40, "ok": False}]}
