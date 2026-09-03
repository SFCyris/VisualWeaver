# SPDX-License-Identifier: AGPL-3.0-or-later
"""Editable Source for EVERY fenced figure, not only the rich lanes.

The rich-lane cards gained an in-place editor (Source → edit → ▶ Apply / ↺ Reset)
first; the ```plot, ```abc, ```svg and ```latex cards kept a read-only Source
pane because the editor was attached only for `rich-src` and its Apply/Reset
only knew how to re-draw a `.rich-output`. These tests pin the generalisation:

* one family table (`_SRC`) at module scope names every card's wrapper, source
  pane, Copy label AND output element;
* the editable branch is no longer gated on the rich family;
* Apply/Reset resolve whichever card they sit in, ask `_rerenderBlocked` BEFORE
  touching the source, and go through `_rerenderCard`, which dispatches per
  family and cleans up what each family leaves behind (a plot's sliders, its
  definitions block and a queued slider redraw; a score's synth, its
  AudioContext and a Play still loading);
* the SVG and LaTeX builders and their Apply share one renderer each, and the
  Apply path — which does NOT pass through renderMarkdown's sanitiser — applies
  the same gate itself.

The behaviour itself is measured in test_editable_source_browser.py.
"""
import json
import pathlib
import re

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _html() -> str:
    return _INDEX.read_text(encoding="utf-8")


def _block(html: str, start: str, end: str) -> str:
    i = html.index(start)
    return html[i:html.index(end, i)]


def _handler(html: str, act: str) -> str:
    """The `}else if(act==='<act>'){` branch of the delegated click handler."""
    i = html.index(f"}}else if(act==='{act}'){{")
    return html[i:html.index("\n  }", i + 1)]


# ── the family table ─────────────────────────────────────────────────────────
def _src_table(html: str) -> dict:
    m = re.search(r"const _SRC=\{(.*?)\};\n", html, re.S)
    assert m, "no module-scope _SRC table"
    body = "{" + m.group(1) + "}"
    body = re.sub(r"(\w+):\[", r'"\1":[', body).replace("'", '"')
    return json.loads(body)


def test_family_table_is_defined_once_at_module_scope():
    html = _html()
    assert html.count("const _SRC=") == 1, "the table must not be duplicated inside the click handler"
    assert html.index("const _SRC=") < html.index("document.addEventListener('click',function(e){")


def test_family_table_names_every_family_with_its_output_element():
    t = _src_table(_html())
    assert set(t) == {"svg", "tex", "abc", "plot", "rich"}
    for fam, row in t.items():
        assert len(row) == 4, f"{fam}: expected [wrap, pre, copy label, output], got {row}"
    assert t["svg"][3] == ".svg-render-output"
    assert t["tex"][3] == ".katex-block-output"
    assert t["abc"][3] == ".abc-render-output"
    assert t["plot"][3] == ".plot-render-output"
    assert t["rich"][3] == ".rich-output"


def test_every_wrapper_in_the_table_is_what_the_maximize_button_looks_up():
    """Maximize and the editor must agree on what a card is, or one of them
    silently ignores a family."""
    html = _html()
    t = _src_table(html)
    m = re.search(r"const wrap=btn\.closest\('([^']+)'\);\n\s+const outEl=wrap\?\.querySelector\('\.viz-output-el'\)", html)
    assert m, "openMaximize's wrapper lookup not found"
    assert set(m.group(1).split(",")) == {row[0] for row in t.values()}


def test_every_family_names_the_library_its_renderer_dereferences():
    """renderAbcBlocks bails on !window.ABCJS, renderPlotBlocks on !window.math,
    the svg helper on !DOMPurify and the tex branch on !katex||!DOMPurify; the
    rich lanes lazy-load their own."""
    html = _html()
    lib = _block(html, "const _SRC_LIB={", "};\n")
    assert "svg:()=>!!window.DOMPurify" in lib
    assert "tex:()=>!!(window.katex&&window.DOMPurify)" in lib
    assert "abc:()=>!!window.ABCJS" in lib
    assert "plot:()=>!!window.math" in lib
    assert "rich:()=>true" in lib
    assert set(_src_table(html)) == {"svg", "tex", "abc", "plot", "rich"}


# ── the editable branch ──────────────────────────────────────────────────────
def test_editable_branch_is_not_gated_on_the_rich_family():
    html = _html()
    assert "if(act==='rich-src'&&!shown){" not in html
    block = _block(html, "btn.textContent=shown?'Source':'Hide source';",
                   "pre.insertBefore(bar,pre.firstChild);")
    assert "if(!shown){" in block
    assert "code._originalSrc=code.textContent;" in block


def test_apply_resolves_any_card_family_and_uses_the_family_aware_rerender():
    h = _handler(_html(), "rich-apply")
    assert "btn.closest(_SRC_WRAPS)" in h
    assert "_rerenderCard(wrap)" in h
    assert "_rerenderRichCard(" not in h


def test_apply_asks_why_it_is_blocked_before_rendering():
    h = _handler(_html(), "rich-apply")
    assert "const why=_rerenderBlocked(wrap);" in h
    assert "if(why){toast(why,'info');return;}" in h
    assert h.index("_rerenderBlocked(wrap)") < h.index("_rerenderCard(wrap)")


def test_reset_resolves_any_card_family_and_its_own_source_element():
    h = _handler(_html(), "rich-reset")
    assert "btn.closest(_SRC_WRAPS)" in h
    assert "const code=_srcCode(wrap);" in h
    assert "_rerenderCard(wrap)" in h


def test_reset_checks_it_can_redraw_before_it_restores_the_text():
    """Restoring the source and then failing to redraw leaves the pane and the
    card disagreeing — and 'Already the original' then blocks the retry."""
    h = _handler(_html(), "rich-reset")
    assert "const why=_rerenderBlocked(wrap);" in h
    assert h.index("_rerenderBlocked(wrap)") < h.index("code.textContent=code._originalSrc;")


def test_blocked_reasons_cover_maximized_and_missing_renderer():
    html = _html()
    b = _block(html, "function _rerenderBlocked(wrap){", "\n}\n")
    assert "return 'Restore the maximized card first';" in b
    assert "not loaded" in b
    assert "_SRC[fam][3]" in b and "_SRC[fam][1]+' code'" in b


# ── the family-aware re-render ───────────────────────────────────────────────
def _rerender(html: str) -> str:
    return _block(html, "function _rerenderCard(wrap){", "\n}\n")


def test_rerender_dispatches_every_family():
    r = _rerender(_html())
    assert "if(fam==='rich')return _rerenderRichCard(wrap);" in r
    for fam in ("svg", "tex", "abc", "plot"):
        assert f"fam==='{fam}'" in r, fam


def test_rerender_is_a_noop_while_the_output_is_maximized():
    r = _rerender(_html())
    assert "if(!fam||_rerenderBlocked(wrap))return false;" in r


def test_rerender_canonicalises_contenteditable_newlines_like_the_rich_path():
    r = _rerender(_html())
    assert "code.textContent=code.innerText" in r


def test_plot_rerender_drops_the_old_sliders_and_definitions_block():
    r = _rerender(_html())
    plot = r[r.index("fam==='plot'"):]
    assert "const pp=wrap.querySelector('.plot-params');" in plot
    assert "if(pp){ clearTimeout(pp._ppTimer); pp.remove(); }" in plot
    assert "classList.contains('plot-defs-block'))defs.remove();" in plot
    assert "delete out.dataset.rendered" in plot
    assert "renderPlotBlocks(" in plot


def test_slider_bar_stashes_its_redraw_timer_where_the_rerender_can_cancel_it():
    html = _html()
    ap = _block(html, "function _attachPlotParams(wrap,out,spec,params){", "\n}\n")
    assert "bar._ppTimer=raf;" in ap
    assert ap.index("},16);") < ap.index("bar._ppTimer=raf;")


def test_abc_rerender_stops_and_forgets_the_synth_primed_on_the_old_tune():
    r = _rerender(_html())
    abc = r[r.index("fam==='abc'"):r.index("fam==='plot'")]
    assert "out._abcSynth.stop()" in abc
    assert "if(out._abcCtx)out._abcCtx.close();" in abc
    assert "out._abcGen=(out._abcGen||0)+1;" in abc
    assert "out._abcSynth=null; out._abcPlaying=false; out._abcTune=null; out._abcCtx=null;" in abc
    assert "play.textContent='▶ Play'" in abc
    assert "delete out.dataset.rendered" in abc
    assert "renderAbcBlocks(" in abc


def test_play_drops_a_synth_that_an_apply_superseded_while_it_loaded():
    html = _html()
    play = _block(html, "async function _abcPlay(btn){", "\n}\n")
    assert "const gen=out._abcGen||0;" in play
    assert play.index("const gen=out._abcGen||0;") < play.index("await synth.init(")
    guard = "if((out._abcGen||0)!==gen){ try{ ac.close(); }catch(e){} return; }"
    assert guard in play
    assert play.index("await synth.prime();") < play.index(guard) < play.index("out._abcSynth=synth; out._abcCtx=ac;")


# ── one renderer per family, shared by the builder and Apply ────────────────
def test_svg_card_builder_and_apply_share_one_sanitiser():
    html = _html()
    assert "const safeSvg=_svgCardHtml(raw);" in html, "the card builder must use the shared helper"
    assert "out.innerHTML=_svgCardHtml(src);" in _rerender(html)
    helper = _block(html, "function _svgCardHtml(raw){", "\n}\n")
    assert "USE_PROFILES:{svg:true,svgFilters:true}" in helper
    assert "FORBID_TAGS:['style']" in helper, \
        "renderMarkdown forbids <style>; an Apply re-render bypasses it and must forbid it too"


def test_latex_card_builder_and_apply_share_one_renderer():
    html = _html()
    assert "const rendered=_katexBlockHtml(src);" in html, "the card builder must use the shared helper"
    assert html.count("katex.renderToString(src,{displayMode:true,throwOnError:false,trust:false})") == 1
    assert "DOMPurify.sanitize(_katexBlockHtml(src),{FORBID_TAGS:['style']})" in _rerender(html)


def test_katex_error_text_is_escaped_before_it_becomes_html():
    """The error branch interpolates the exception message into markup. On the
    Apply path that markup lands in the DOM straight from user-edited source."""
    html = _html()
    js = extract_js_function(html, "escHtml") + "\n" + extract_js_function(html, "_katexBlockHtml") + r"""
const out=[];
globalThis.katex={renderToString(){ throw new Error('<img src=x onerror="alert(1)"> & done'); }};
out.push(_katexBlockHtml('x'));
globalThis.katex={renderToString(s,o){ return '<span class="katex">'+s+'|'+JSON.stringify(o)+'</span>'; }};
out.push(_katexBlockHtml('a+b'));
console.log(JSON.stringify(out));
"""
    r = run_node(js)
    assert r.returncode == 0, r.stderr
    err, ok = json.loads(r.stdout.strip().splitlines()[-1])
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; done" in err, err
    assert "<img" not in err
    assert err.startswith('<span style="color:var(--red);font-size:12px">LaTeX error: ')
    assert ok == '<span class="katex">a+b|{"displayMode":true,"throwOnError":false,"trust":false}</span>'


# ── styling ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("pre", [".rich-src-pre", ".plot-src-pre", ".abc-src-pre",
                                 ".svg-src-pre", ".katex-src-pre"])
def test_editable_focus_styling_covers_every_family(pre):
    html = _html()
    focus = re.search(r"\n([^\n{]*code\[contenteditable\]:focus[^\n{]*)\{", html)
    assert focus, "no :focus rule for editable source"
    assert f"{pre} code[contenteditable]:focus" in focus.group(1), pre


# ── scoping and playback state ───────────────────────────────────────────────
def test_render_passes_accept_a_single_card_so_apply_cannot_touch_siblings():
    html = _html()
    assert "function _cardsIn(el,sel){ return (el.matches&&el.matches(sel))?[el]:el.querySelectorAll(sel); }" in html
    for sel in (".rich-wrap", ".abc-render-wrap", ".plot-render-wrap"):
        assert f"_cardsIn(el,'{sel}').forEach(" in html, sel
    rich = _block(html, "function _rerenderRichCard(wrap){", "\n}\n")
    assert "renderRichBlocks(wrap);" in rich and "parentElement" not in rich
    r = _rerender(html)
    assert "renderAbcBlocks(wrap);" in r and "renderPlotBlocks(wrap);" in r
    assert "parentElement" not in r


def test_end_of_tune_timer_only_clears_the_playback_it_was_armed_for():
    play = _block(_html(), "async function _abcPlay(btn){", "\n}\n")
    assert "const playId=out._abcPlayId=(out._abcPlayId||0)+1;" in play
    assert "if(out._abcPlayId===playId&&out._abcPlaying){out._abcPlaying=false;btn.textContent='▶ Play';}" in play


def test_play_closes_a_context_the_card_never_adopted():
    play = _block(_html(), "async function _abcPlay(btn){", "\n}\n")
    assert "let ac=null;" in play and "ac=new (window.AudioContext||window.webkitAudioContext)();" in play
    catch = play[play.index("}catch(e){"):]
    assert "if(ac&&out._abcCtx!==ac){ try{ ac.close(); }catch(e2){} }" in catch


def test_rerender_reenables_a_play_button_held_by_a_superseded_load():
    r = _rerender(_html())
    abc = r[r.index("fam==='abc'"):r.index("fam==='plot'")]
    assert "if(play){ play.textContent='▶ Play'; play.disabled=false; }" in abc


def test_geometry_interact_goes_through_the_same_gate():
    h = _handler(_html(), "geo-interact")
    assert "const why=_rerenderBlocked(wrap); if(why){toast(why,'info');return;}" in h


def test_slider_timer_stash_is_cleared_when_the_timer_fires():
    ap = _block(_html(), "function _attachPlotParams(wrap,out,spec,params){", "\n}\n")
    assert "raf=setTimeout(()=>{ raf=0; bar._ppTimer=0;" in ap
