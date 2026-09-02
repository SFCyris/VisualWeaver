# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ```table lane's number parsing and total formatting.

Two defects, one new and one that had always been there:

* The computed total was rendered as a bare number, so a column of "$3,400.75"
  totalled to "5791.35" — arithmetically right, visibly not the same kind of
  thing, and now embedded in DOCS.md directly under the sentence about sorting.
* `num()` stripped everything except ``[0-9.eE+-]`` so that exponents would
  work, which left the E of "EUR": "99,25 EUR" became "9925E" and parsed as
  9925. A European column sorted and totalled about a hundredfold wrong.

Both functions are pure, so they run under node with no DOM.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _helpers() -> str:
    """The shared cell parser and the total formatter.

    `_cellNumber` and `_cellDecimalSep` sit at module scope — the ```table lane
    and the Markdown-table sorter share them, after each carried its own parser
    and the two disagreed in opposite directions. `fmtLike` is still inside the
    lane, so both regions are taken.
    """
    html = _INDEX.read_text(encoding="utf-8")
    # fmtLike is still inside the lane and comes FIRST in the file; the shared
    # parser was hoisted to module scope, next to the Markdown-table sorter.
    c = html.index("const fmtLike=")
    d = html.index("function view(){", c)
    a = html.index("// Shared by the ```table lane and the Markdown-table sorter")
    b = html.index("const _RR_NUM=", a)
    return html[a:b] + "\n" + html[c:d] + "\nconst num=_cellNumber;\n"


def _val(expr: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = _helpers() + f"\nconsole.log(JSON.stringify({expr}));\n"
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1200]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── parsing ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cell,expected", [
    ("7", 7), ("120", 120), ("3.5", 3.5), ("-4.25", -4.25),
    ("$3,400.75", 3400.75), ("$980.00", 980.0), ("45%", 45),
    # THREE decimals and more. Their absence is why a rule that only recognised
    # one or two trailing digits shipped: it deleted the decimal point instead,
    # making every such value a thousandfold too large in sorting and in totals.
    ("3.142", 3.142), ("0.125", 0.125), ("0.001", 0.001), ("$3.459", 3.459),
    ("12.3456", 12.3456), ("1,234.567", 1234.567), ("0.9", 0.9),
    # a lone separator with exactly three digits after it is the ambiguous case
    ("1.234", 1.234), ("12,34,567", 1234567), ("1.234.567", 1234567),
    ("12 345", 12345), ("1,234", 1234),
    # European: comma decimal, dot grouping — and a currency word whose E used
    # to be read as an exponent.
    ("1.234,50 EUR", 1234.5), ("99,25 EUR", 99.25), ("2.000,25", 2000.25),
    # accounting parentheses
    ("(50.25)", -50.25),
    # not numbers
    ("n/a", None), ("", None), ("—", None),
])
def test_num_parses_a_cell(cell, expected):
    assert _val(f"num({json.dumps(cell)})") == expected


def test_a_currency_word_is_not_read_as_an_exponent():
    """The regression this file is named for: "99,25 EUR" must not be 9925."""
    assert _val('num("99,25 EUR")') == 99.25
    assert _val('num("100 EUR")') == 100
    assert _val('num("5 exabytes")') == 5


def test_a_genuine_exponent_still_parses():
    """Dropping [eE] entirely would have been a quiet feature removal."""
    assert _val('num("1e3")') == 1000
    assert _val('num("-2.5E-2")') == -0.025


# ── formatting ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("column,value,expected", [
    (["$3,400.75", "$980.00"], 5791.35, "$5,791.35"),
    # grouping is taken from ANY cell that shows it, so a total that crosses
    # 1000 is not the only ungrouped number in a grouped column
    (["$980.00", "$1,210.10"], 1000,    "$1,000.00"),
    (["7", "43"],              258,     "258"),
    (["45%", "12%"],           60,      "60%"),
    (["1.234,50 EUR", "99,25 EUR"], 3334, "3.334,00 EUR"),
    (["12 345", "250"],        13595,   "13 595"),
    # decimals at the column's MAXIMUM: a sum of 1- and 2-decimal values can
    # need 2, and truncating to the first cell's 1 would hide value
    (["3.5", "3.75"],          7.25,    "7.25"),
    (["-4.25", "1.00"],        -8.5,    "-8.50"),
])
def test_the_total_is_formatted_like_its_column(column, value, expected):
    assert _val(f"fmtLike({json.dumps(column)},{value})") == expected


def test_a_column_with_no_numeric_sample_falls_back_to_the_plain_number():
    assert _val("fmtLike([],42)") == "42"
    assert _val('fmtLike(["n/a","-"],42)') == "42"
    assert _val("fmtLike(undefined,42)") == "42"


def test_formatting_never_invents_precision():
    """A column of whole numbers must not gain decimals, and a column of three
    must not lose them — the earlier rule capped decimals at two and silently
    fell back to zero above that, rendering "3.129.25" for a 7.375 total."""
    assert _val('fmtLike(["7","43"],258)') == "258"
    assert _val('fmtLike(["1.5","2.75","3.125"],7.375)') == "7.375"
    assert _val('fmtLike(["0.001","0.005"],0.006)') == "0.006"


def test_accounting_parentheses_are_a_sign_not_decoration():
    """Taking them as prefix and suffix wrapped a POSITIVE total in brackets,
    which in the one notation where brackets carry meaning reads as negative."""
    assert _val('fmtLike(["(50.00)","100.00"],50)') == "50.00"
    assert _val('fmtLike(["(50.00)","(20.00)"],-70)') == "(70.00)"


def test_an_affix_survives_a_row_that_omits_it():
    """Taking the plain majority let one unformatted row strip the currency, and
    made the answer depend on row order."""
    assert _val('fmtLike(["980","$1,000","1,020"],3000)') == "$3,000"
    assert _val('fmtLike(["$1,000","980","1,020"],3000)') == "$3,000"


# ── the Markdown-table sorter shares this parser ─────────────────────────────
def _rr_helpers() -> str:
    html = _INDEX.read_text(encoding="utf-8")
    a = html.index("// Shared by the ```table lane and the Markdown-table sorter")
    b = html.index("function _rrTableCsv(", a)
    return html[a:b]


def _sorts_as(cell):
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = _rr_helpers() + (
        f"\nconsole.log(JSON.stringify(_rrCellVal({{textContent:{json.dumps(cell)}}})));\n")
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1200]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("cell,expected", [
    # These are what separate the shared parser from the one this path used to
    # carry. Its own version accepted only $/£/€ and %, then stripped to
    # [0-9.-]: the first sorted as 9925 and the rest fell through to a TEXT
    # sort, while DOCS.md promised currency sorted by value.
    ("99,25", 99.25),
    ("99,25 EUR", 99.25),
    ("1.234,50 EUR", 1234.5),
    ("(50.25)", -50.25),
    ("12 345", 12345),
    ("3.142", 3.142),
    ("$3,400.75", 3400.75),
])
def test_a_markdown_table_sorts_the_same_way_the_table_lane_does(cell, expected):
    got = _sorts_as(cell)
    assert isinstance(got, (int, float)) and not isinstance(got, bool), \
        f"{cell!r} sorted as text ({got!r}), so the column orders alphabetically"
    assert abs(got - expected) < 1e-9, f"{cell!r} -> {got!r}"


@pytest.mark.parametrize("cell", ["2026-07-22", "AB123", "n/a", "hello", ""])
def test_the_markdown_sorter_still_leaves_non_numbers_alone(cell):
    """Widening what counts as a number must not swallow dates or identifiers."""
    got = _sorts_as(cell)
    if cell.startswith("2026"):
        assert isinstance(got, (int, float)) and got > 1e11, f"{cell!r} lost its date sort"
    else:
        assert isinstance(got, str), f"{cell!r} was read as a number ({got!r})"


@pytest.mark.parametrize("cells", [
    # Every cell of a column must land on the SAME side of the number/text line.
    # A gate that accepted "3 days" (4 letters) and rejected "3 weeks" (5) left
    # the comparator with no total order — number-vs-string compares as NaN both
    # ways — so the column sorted arbitrarily where it used to sort alphabetically.
    ["3 days", "3 weeks", "10 days"],
    ["5 min", "5 hours", "30 min"],
    ["0xFF", "0x1F", "0xA0"],
    ["1st", "2nd", "3rd"],
    ["Q1 2026", "Q2 2026"],
    # and the numeric side stays numeric together
    ["99,25 EUR", "1.234,50 EUR"],
    ["$3,400.75", "$980.00"],
    ["(50.25)", "100.00"],
])
def test_a_column_never_splits_across_the_number_text_line(cells):
    kinds = {isinstance(_sorts_as(c), str) for c in cells}
    assert len(kinds) == 1, (
        f"{cells} sorts as a mix of numbers and text, so it has no total order")


@pytest.mark.parametrize("version", ["1.2.3", "1.9.0", "2.0.0", "1.10.0", "1.0.10"])
def test_version_strings_are_not_read_as_numbers(version):
    """Guessing that the last separator is a decimal concatenated the leading
    segments: 1.9.0 became 19 and sorted BETWEEN 1.2.3 and 1.10.0, and 1.0.1 and
    1.0.10 both became 10.1. Text at least gives a total order."""
    assert isinstance(_sorts_as(version), str), f"{version!r} was read as a number"


@pytest.mark.parametrize("cell", ["10.01.2024", "2024.03.15"])
def test_a_dotted_date_reaches_the_date_branch(cell):
    """_rrCellVal tests the numeric gate BEFORE Date.parse, so a dotted date read
    as a number never reached the date branch — "10.01.2024" sorted as 1001.2024,
    which orders by day and ignores the year."""
    got = _sorts_as(cell)
    assert isinstance(got, (int, float)) and got > 1e11, \
        f"{cell!r} sorted as {got!r}, not as a date"
