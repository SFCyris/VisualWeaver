# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 1.16.0 review fixes to the grounding badge and citation parsing (_citeParse,
_groundingOf) and to the per-browser conversation list (_rememberSessionId), run as real
JavaScript under node.

Before these fixes: an answer in Chinese, Japanese or Thai (no spaces between words) never
had a "substantive" paragraph, so a fully cited one could not read as grounded and an
uncited one could not read as partly grounded; a search that ran and retrieved nothing was
graded a red "Uncited", as if the model had ignored its sources; a reference number quoted
from a document ("[12]" in a paper, with five sources) was struck through as a broken
citation; and the browser's list of its conversations kept only the last 200 ids, so older
and even pinned conversations dropped out of the sidebar.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _run(names, body: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in names)
    r = run_node(f"{src}\n{body}")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _parse(text, max_n):
    return _run(("_citeParse",), f"console.log(JSON.stringify(_citeParse({json.dumps(text)},{max_n})))")


def _g(raw, max_n=3, rag_used=True, not_found=False, search_failed=False):
    return _run(("_citeParse", "_groundingOf"),
                f"console.log(JSON.stringify(_groundingOf({json.dumps(raw)},{max_n},"
                f"{json.dumps(rag_used) if rag_used is not None else 'undefined'},"
                f"{json.dumps(not_found)},{json.dumps(search_failed)})))")


# ── (a) scripts written without spaces ───────────────────────────────────────

_ZH = ("Redis 是一个开源的内存数据结构存储系统，可以用作数据库和缓存[1]。\n\n"
       "它支持字符串、哈希、列表、集合和有序集合等多种数据结构[2]。")
_JA = "Redisはオープンソースのインメモリデータストアで、データベースとして使えます[1]。"
_TH = "เรดิสเป็นระบบจัดเก็บข้อมูลในหน่วยความจำแบบโอเพนซอร์ส"


def test_a_fully_cited_chinese_answer_is_grounded():
    """Old: each paragraph counted as 2 "words" (one per run of letters) — never
    substantive, so units == 0 and the state could never be judged on its paragraphs."""
    g = _g(_ZH)
    assert g == {"state": "grounded", "units": 2, "cited": 2, "valid": 2, "invalid": 0}


def test_an_uncited_chinese_paragraph_makes_it_partly_grounded():
    g = _g(_ZH + "\n\n大多数数据库也提供某种形式的日志结构来保存历史记录。")
    assert (g["state"], g["units"], g["cited"]) == ("partly", 3, 2)


def test_japanese_and_thai_sentences_are_substantive():
    assert _g(_JA)["units"] == 1
    # Thai: no citation at all, but the sentence counts as a substantive unit
    g = _g(_TH + "[1]\n\n" + _TH)
    assert (g["units"], g["cited"], g["state"]) == (2, 1, "partly")


def test_short_cjk_lead_ins_and_fragments_are_not_substantive():
    # a full-width colon ends a lead-in; four characters are not a substantive unit
    g = _g("文档中描述了以下几个重要的属性和特征：\n\n- 追加写入[1]\n- 消费者组[2]")
    assert g["units"] == 0 and g["state"] == "grounded"


# ── (b) a search that ran and retrieved nothing is neutral ───────────────────

def test_nothing_retrieved_is_its_own_neutral_state_not_uncited():
    """Old: state "uncited" (red) — the model was blamed for sources it was never given."""
    g = _g("A general answer from the model with no sources.", 0, rag_used=True)
    assert g["state"] == "noretrieval"


def test_a_failed_search_is_its_own_state():
    g = _g("Search failed: the knowledge base could not be searched.", 0, rag_used=True,
           search_failed=True)
    assert g["state"] == "searchfailed"
    # a failed search beats "not found" (the documents were never looked at)
    assert _g("x", 0, rag_used=True, not_found=True, search_failed=True)["state"] == "searchfailed"


def test_with_sources_an_uncited_answer_is_still_uncited():
    assert _g("Streams in Redis are append-only data structures for logs.", 3)["state"] == "uncited"


# ── (c) numbers far out of range are text, not broken citations ──────────────

def test_a_quoted_reference_number_far_out_of_range_is_plain_text():
    """Rule: a number beyond 2 x maxN inside a quotation, or opening a reference-list line,
    is a quoted reference (a paper's "[12]"), not a citation (see test_rereview_js)."""
    assert _parse('As the paper puts it: "shown in [12], streams scale".', 5) == [
        {"text": 'As the paper puts it: "shown in [12], streams scale".'}]
    assert _parse("[11] Smith, J. Streams.", 5) == [{"text": "[11] Smith, J. Streams."}]
    # in plain prose it is a broken citation
    assert _parse("See [10].", 5)[1] == {"cite": [{"n": 10, "ok": False}]}
    assert _parse("As shown in [12], streams scale.", 5)[1] == {"cite": [{"n": 12, "ok": False}]}
    # nothing retrieved: broken in prose, text inside a quotation
    assert _parse("A [3].", 0)[1] == {"cite": [{"n": 3, "ok": False}]}
    assert _parse('A "[6]".', 0) == [{"text": 'A "[6]".'}]


def test_a_far_number_does_not_count_against_grounding():
    g = _g('Streams in Redis are append-only data structures [1] as in "the log [42]".', 3)
    assert (g["state"], g["valid"], g["invalid"]) == ("grounded", 1, 0)


def test_a_far_number_grouped_with_a_valid_one_is_still_broken():
    assert _parse("X [2, 40].", 5)[1] == {"cite": [{"n": 2, "ok": True}, {"n": 40, "ok": False}]}


# ── the per-browser conversation list ────────────────────────────────────────

_LS = """
const _store={};
const localStorage={getItem:k=>k in _store?_store[k]:null,setItem:(k,v)=>{_store[k]=String(v);}};
const S={sessions:{},sessionsLoaded:true};
"""


def _ids(body):
    return _run(("_mySessionIds", "_rememberSessionId", "_forgetSessionId"), _LS + body)


def test_opening_a_conversation_moves_its_id_to_the_end():
    out = _ids("_rememberSessionId('a');_rememberSessionId('b');_rememberSessionId('c');"
               "_rememberSessionId('a');console.log(JSON.stringify(_mySessionIds()))")
    assert out == ["b", "c", "a"]


def test_the_list_keeps_far_more_than_200_and_never_drops_a_pinned_id():
    """Old: ids.slice(-200) — the 201st conversation pushed the oldest out of the sidebar,
    pinned or not, while it lived on (forever, with the default retention) on the server."""
    out = _ids("S.sessions['p0']={pinned:true};_rememberSessionId('p0');"
               "for(let i=1;i<=1500;i++)_rememberSessionId('c'+i);"
               "const ids=_mySessionIds();"
               "console.log(JSON.stringify({n:ids.length,p0:ids.includes('p0'),c1:ids.includes('c1'),"
               "c600:ids.includes('c600'),last:ids.at(-1)}))")
    assert out["p0"] is True and out["last"] == "c1500"
    assert out["n"] == 1001 and out["c600"] is True and out["c1"] is False


def test_before_the_server_list_is_read_nothing_is_trimmed():
    """A pinned conversation is only known as pinned once the server list is read; until
    then no id may be dropped."""
    out = _ids("S.sessionsLoaded=false;for(let i=1;i<=1100;i++)_rememberSessionId('c'+i);"
               "console.log(JSON.stringify(_mySessionIds().length))")
    assert out == 1100
