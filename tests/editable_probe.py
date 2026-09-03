# SPDX-License-Identifier: AGPL-3.0-or-later
"""Apply / Reset on every fenced figure card, in a real browser.

The rich lanes have had an editable Source pane for a while; ```plot, ```abc,
```svg and ```latex cards did not — their Source view was read-only. This probe
builds one card of each of those four families, plus a rich-lane reference card
(```table, which needs no CDN library), exactly the way a rendered message does
(renderMarkdown + the post-render passes, inside a production `.msg-bubble.ai`
so the real CSS applies, with the per-<pre> Copy overlay attached), opens its
Source, edits the text, clicks ▶ Apply and then ↺ Reset, and reports what the
OUTPUT element holds after each step: its pixel box, the number of notes /
sliders / definition blocks / table headers, whether a hostile edit left any
<script>, <style>, <img> or javascript: link behind.

Further scenarios exercise what a click round-trip alone does not:
* ``typed``     — real keystrokes with Enter, so the contenteditable
                  canonicalisation runs on browser-made nodes;
* ``maximized`` — a REAL ⛶ Maximize (not a detached node): Reset must decline
                  without touching the source, and work again after restore;
* ``pending``   — a slider redraw queued just before Apply must not repaint the
                  OLD spec over the new one;
* ``nolib``     — with the family's library missing, Apply must decline and
                  leave the card intact;
* ``inflight``  — ▶ Play still loading its soundfont when Apply lands must not
                  hand the OLD tune's synth back to the card, nor leave the
                  button disabled;
* ``stoptimer`` — the end-of-tune timer of an earlier playback must not clear a
                  later one (Stop then Play; Apply then Play);
* ``siblings``  — Apply on one card must not re-render another card in the same
                  message (a sibling that failed to render, whose Source was
                  edited but never applied).

Same design as ``tests/fractal_probe.py``: one line of JSON on stdout, driven by
``tests/test_editable_source_browser.py``.
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}

# Each case: the fenced block as the model would write it, and the edit a user
# types into the Source pane. The SVG and LaTeX edits are deliberately hostile —
# they carry <style>/<script>, an <img onerror> and a javascript: link — so the
# probe proves an Apply re-render is gated like the original render path.
CASES = {
    "svg": {
        "md": "```svg\n<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"120\" height=\"60\">"
              "<circle cx=\"30\" cy=\"30\" r=\"20\" fill=\"tomato\"/></svg>\n```",
        "edit": "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"240\" height=\"120\">"
                "<rect x=\"10\" y=\"10\" width=\"200\" height=\"100\" fill=\"steelblue\"/>"
                "<style>rect{fill:red}</style><script>window.__pwned=1</script></svg>",
        "src": "svg-src", "wrap": ".svg-render-wrap",
        "out": ".svg-render-output", "pre": ".svg-src-pre",
    },
    "tex": {
        "md": "```latex\n\\frac{a}{b}\n```",
        # No underscore inside \text{} — KaTeX rejects it and would typeset
        # nothing, which is not the case under test.
        "edit": "\\sum_{i=1}^{n} x_i^2 + \\int_0^1 f(t)\\,dt "
                "\\href{javascript:window.pwnedTex=1}{link} "
                "\\text{<img src=x onerror=\"window.pwnedTex=1\">}",
        "src": "tex-src", "wrap": ".katex-block-wrap",
        "out": ".katex-block-output", "pre": ".katex-src-pre",
    },
    "abc": {
        "md": "```abc\nX:1\nT:Before\nM:4/4\nL:1/4\nK:C\nC D E F|\n```",
        "edit": "X:1\nT:After\nM:4/4\nL:1/8\nK:G\nG A B c d e f g|g f e d c B A G|",
        "src": "abc-src", "wrap": ".abc-render-wrap",
        "out": ".abc-render-output", "pre": ".abc-src-pre",
    },
    "plot": {
        "md": "```plot\ny = x^2\n```",
        "edit": "param: a = 1 .. 5 (2)\ny = a*sin(x)\ny = cos(x)",
        "src": "plot-src", "wrap": ".plot-render-wrap",
        "out": ".plot-render-output", "pre": ".plot-src-pre",
    },
    # The reference implementation the request pointed at ("like you can in the
    # fractal"): a rich lane, drawn without any CDN library.
    "rich": {
        "md": "```table\n{\"columns\":[\"Item\",\"Qty\"],\"rows\":[[\"A\",2],[\"B\",3]],\"total\":[\"Qty\"]}\n```",
        "edit": "{\"columns\":[\"Item\",\"Qty\",\"Price\"],\"rows\":[[\"X\",1,9.5],[\"Y\",4,2.5],[\"Z\",2,1]],\"total\":[\"Qty\"]}",
        "src": "rich-src", "wrap": ".rich-wrap",
        "out": ".rich-output", "pre": ".rich-src-pre",
    },
}

# Shared by every scenario: build a card the way a rendered message does.
_BUILD = r"""
  const build=async (md, wrapSel)=>{
    const host=document.createElement('div');
    host.className='msg-bubble ai'; host.style.width='700px';
    document.body.appendChild(host);
    host.innerHTML=renderMarkdown(md,{math:true});
    renderMath(host); renderAbcBlocks(host); renderPlotBlocks(host); renderRichBlocks(host); addCopyButtons(host);
    await new Promise(r=>setTimeout(r,160));
    const wrap=host.querySelector(wrapSel);
    return {host, wrap};
  };
  const box=el=>{ if(!el)return null; const r=el.getBoundingClientRect(); return {t:Math.round(r.top),l:Math.round(r.left),w:Math.round(r.width),h:Math.round(r.height)}; };
  const overlaps=(a,b)=>!!(a&&b&&a.w&&b.w&&a.l<b.l+b.w&&b.l<a.l+a.w&&a.t<b.t+b.h&&b.t<a.t+a.h);
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  // A synth stub whose init() resolves at once and whose playback lasts `secs`.
  const stubSynth=(secs)=>({
    CreateSynth: class { init(){ window.__abcInits=(window.__abcInits||0)+1; return Promise.resolve(); }
                         prime(){ return Promise.resolve(); }
                         start(){ window.__abcStarted=(window.__abcStarted||0)+1; }
                         stop(){ window.__abcStopped=(window.__abcStopped||0)+1; }
                         get duration(){ return secs; } },
    supportsAudio: ()=>true,
  });
"""

PROBE = "async (c) => {" + _BUILD + r"""
  const {host, wrap}=await build(c.md, c.wrap);
  if(!wrap) return {error:'no card was built for '+c.wrap};
  const sig=()=>{
    const out=wrap.querySelector(c.out);
    const r=out?out.getBoundingClientRect():{width:0,height:0};
    const svg=out?out.querySelector('svg'):null;
    let defs=0, p=wrap.previousElementSibling;
    while(p&&p.classList.contains('plot-defs-block')){defs++; p=p.previousElementSibling;}
    const first=wrap.previousElementSibling;
    return {
      htmlLen: out?out.innerHTML.length:-1,
      text: out?out.textContent.replace(/\s+/g,' ').trim().slice(0,120):null,
      w: Math.round(r.width), h: Math.round(r.height),
      svgW: svg?Math.round(svg.getBoundingClientRect().width):0,
      svgH: svg?Math.round(svg.getBoundingClientRect().height):0,
      notes: out?out.querySelectorAll('.abcjs-note').length:0,
      ths: out?out.querySelectorAll('th').length:0,
      sliders: wrap.querySelectorAll('.plot-params input[type=range]').length,
      defs,
      defsText: (first&&first.classList.contains('plot-defs-block'))?first.textContent.replace(/\s+/g,' ').trim().slice(0,120):null,
      tune: c.wrap==='.abc-render-wrap' ? !!(out&&out._abcTune) : null,
      hasScript: !!(out&&out.querySelector('script')),
      hasStyle: !!(out&&out.querySelector('style')),
      hasSum: !!(out&&out.textContent.includes('∑')),   // on the FULL text, not the 120-char slice
      hasImg: !!(out&&out.querySelector('img')),
      jsHref: !!(out&&out.querySelector('a[href^="javascript"],[*|href^="javascript"]')),
      pwned: !!window.__pwned, pwnedTex: !!window.pwnedTex,
      katexError: !!(out&&out.querySelector('.katex-error')),
      rendered: out?(out.dataset.rendered||''):null,
    };
  };
  const before=sig();
  const srcBtn=wrap.querySelector(`[data-act="${c.src}"]`);
  if(!srcBtn) return {error:'no Source button'};
  srcBtn.click();
  const pre=wrap.querySelector(c.pre);
  const code=pre&&pre.querySelector('code');
  if(!code) return {error:'no source <code>'};
  const applyBtn=pre.querySelector('[data-act="rich-apply"]');
  const resetBtn=pre.querySelector('[data-act="rich-reset"]');
  const copyBtn=pre.querySelector('.copy-code-btn');
  const afterOpen={
    bar: !!pre.querySelector('.rich-apply-bar'),
    editable: code.getAttribute('contenteditable'),
    isEditable: code.isContentEditable,
    preShown: getComputedStyle(pre).display!=='none',
    applyPx: applyBtn?applyBtn.offsetHeight:0,
    resetPx: resetBtn?resetBtn.offsetHeight:0,
    snapshotMatches: code._originalSrc===code.textContent,
    srcLabel: srcBtn.textContent,
    copyBox: box(copyBtn), applyBox: box(applyBtn), resetBox: box(resetBtn), codeBox: box(code),
    copyOverlapsApply: overlaps(box(copyBtn),box(applyBtn)),
    copyOverlapsReset: overlaps(box(copyBtn),box(resetBtn)),
    codeBelowBar: !!(box(code)&&box(applyBtn)&&box(code).t>=box(applyBtn).t+box(applyBtn).h),
  };
  if(!applyBtn||!resetBtn) return {before, afterOpen, error:'no Apply/Reset bar'};
  code.textContent=c.edit;
  applyBtn.click();
  await wait(250);
  const after=sig();
  const applyLabel=applyBtn.textContent;
  resetBtn.click();
  await wait(250);
  const reset=sig();
  const resetLabel=resetBtn.textContent;
  const restored=code.textContent===code._originalSrc;
  host.remove();
  return {before, afterOpen, after, applyLabel, reset, resetLabel, restored};
}
"""

# Step 1 of the typed scenario: build a plot card, open its Source, select all.
TYPED_OPEN = "async () => {" + _BUILD + r"""
  const {host, wrap}=await build("```plot\ny = x^2\n```", '.plot-render-wrap');
  host.id='vw-typed';
  if(!wrap) return {error:'no plot card'};
  wrap.querySelector('[data-act="plot-src"]').click();
  const code=wrap.querySelector('.plot-src-pre code');
  code.focus();
  document.execCommand('selectAll');
  return {focused: document.activeElement===code, sliders: wrap.querySelectorAll('.plot-params input').length};
}
"""
# Step 2: after Playwright typed into it, apply and measure.
TYPED_APPLY = "async () => {" + _BUILD + r"""
  const host=document.getElementById('vw-typed');
  const wrap=host&&host.querySelector('.plot-render-wrap');
  const code=wrap&&wrap.querySelector('.plot-src-pre code');
  const applyBtn=wrap&&wrap.querySelector('[data-act="rich-apply"]');
  if(!code||!applyBtn) return {error:'typed scenario lost its card or Apply bar'};
  const nodes=[...code.childNodes].map(n=>n.nodeName);
  const text=code.textContent, innerText=code.innerText;
  applyBtn.click();
  await wait(200);
  const first=wrap.previousElementSibling;
  const r={nodes, text, innerTextSame: text===innerText, applied: code.textContent,
           sliders: wrap.querySelectorAll('.plot-params input[type=range]').length,
           defsText: (first&&first.classList.contains('plot-defs-block'))?first.textContent.replace(/\s+/g,' ').trim():null,
           plotText: wrap.querySelector('.plot-render-output').textContent.replace(/\s+/g,' ').trim().slice(0,80)};
  host.remove();
  return r;
}
"""

MAXIMIZED = "async () => {" + _BUILD + r"""
  const edit='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="150"><rect width="300" height="150" fill="teal"/></svg>';
  const {host, wrap}=await build("```svg\n<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"120\" height=\"60\"><circle cx=\"30\" cy=\"30\" r=\"20\"/></svg>\n```", '.svg-render-wrap');
  if(!wrap) return {error:'no svg card'};
  wrap.querySelector('[data-act="svg-src"]').click();
  const pre=wrap.querySelector('.svg-src-pre'), code=pre.querySelector('code');
  const applyBtn=pre.querySelector('[data-act="rich-apply"]'), resetBtn=pre.querySelector('[data-act="rich-reset"]');
  code.textContent=edit; applyBtn.click(); await wait(100);
  const cardW=()=>{ const s=wrap.querySelector('.svg-render-output svg'); return s?Math.round(s.getBoundingClientRect().width):null; };
  const appliedW=cardW();
  wrap.querySelector('[data-act="viz-max"]').click(); await wait(150);
  const content=document.getElementById('viz-max-content');
  const ovOut=content&&content.querySelector('.svg-render-output');
  const ovW=()=>{ const s=ovOut&&ovOut.querySelector('svg'); return s?Math.round(s.getBoundingClientRect().width):null; };
  const inOverlay=!!ovOut, overlayW=ovW();
  const blockedWhy=_rerenderBlocked(wrap);
  resetBtn.click(); await wait(100);
  const textUntouched=code.textContent===edit;
  const overlayWAfterReset=ovW();
  const resetLabelWhileMax=resetBtn.textContent;
  closeMaximize(); await wait(150);
  const backW=cardW();
  resetBtn.click(); await wait(150);
  const restored=code.textContent===code._originalSrc;
  const finalW=cardW();
  host.remove();
  return {appliedW, inOverlay, overlayW, blockedWhy, textUntouched, overlayWAfterReset, resetLabelWhileMax, backW, restored, finalW};
}
"""

PENDING = "async () => {" + _BUILD + r"""
  const {host, wrap}=await build("```plot\nparam: a = 1 .. 5 (2)\ny = a*x\n```", '.plot-render-wrap');
  if(!wrap) return {error:'no plot card'};
  wrap.querySelector('[data-act="plot-src"]').click();
  const pre=wrap.querySelector('.plot-src-pre'), code=pre.querySelector('code');
  const out=wrap.querySelector('.plot-render-output');
  const inp=wrap.querySelector('.plot-params input[type=range]');
  if(!inp) return {error:'no slider was built'};
  const beforeText=out.textContent.replace(/\s+/g,' ').trim();
  inp.value=5; inp.dispatchEvent(new Event('input'));       // queues a redraw of the OLD spec
  code.textContent='y = 1000';
  pre.querySelector('[data-act="rich-apply"]').click();      // lands before that redraw fires
  await wait(300);
  const first=wrap.previousElementSibling;
  const r={beforeText: beforeText.slice(0,80),
           sliders: wrap.querySelectorAll('.plot-params input[type=range]').length,
           defsText: (first&&first.classList.contains('plot-defs-block'))?first.textContent.replace(/\s+/g,' ').trim():null,
           plotText: out.textContent.replace(/\s+/g,' ').trim().slice(0,100),
           has1000: out.textContent.includes('1000')};
  host.remove();
  return r;
}
"""

NOLIB = "async () => {" + _BUILD + r"""
  const {host, wrap}=await build("```plot\ny = x^2\n```", '.plot-render-wrap');
  if(!wrap) return {error:'no plot card'};
  wrap.querySelector('[data-act="plot-src"]').click();
  const pre=wrap.querySelector('.plot-src-pre'), code=pre.querySelector('code');
  const out=wrap.querySelector('.plot-render-output');
  const applyBtn=pre.querySelector('[data-act="rich-apply"]');
  const htmlBefore=out.innerHTML.length;
  code.textContent='y = 5*x';
  const m=window.math; delete window.math;
  let direct=null, err=null;
  try{ direct=_rerenderCard(wrap); }catch(e){ err=String(e&&e.message||e); }
  const why=_rerenderBlocked(wrap);
  applyBtn.click(); await wait(80);
  const label=applyBtn.textContent;
  window.math=m;
  let defs=0, p=wrap.previousElementSibling;
  while(p&&p.classList.contains('plot-defs-block')){defs++; p=p.previousElementSibling;}
  const r={direct, err, why, label, htmlBefore, htmlAfter: out.innerHTML.length, rendered: out.dataset.rendered||'', defs};
  host.remove();
  return r;
}
"""

INFLIGHT = "async () => {" + _BUILD + r"""
  const edit='X:1\nT:After\nM:4/4\nL:1/8\nK:G\nG A B c d e f g|g f e d c B A G|';
  const {host, wrap}=await build("```abc\nX:1\nT:Before\nM:4/4\nL:1/4\nK:C\nC D E F|\n```", '.abc-render-wrap');
  if(!wrap) return {error:'no abc card'};
  wrap.querySelector('[data-act="abc-src"]').click();
  const pre=wrap.querySelector('.abc-src-pre'), code=pre.querySelector('code');
  const out=wrap.querySelector('.abc-render-output');
  const play=wrap.querySelector('[data-act="abc-play"]');
  const orig=ABCJS.synth; let gate=null; window.__abcStarted=0; window.__abcInits=0;
  ABCJS.synth={
    CreateSynth: class { init(){ window.__abcInits++; return new Promise(r=>{gate=r;}); } prime(){ return Promise.resolve(); }
                         start(){ window.__abcStarted++; } stop(){} get duration(){ return 1; } },
    supportsAudio: ()=>true,
  };
  try{
    play.click(); await wait(40);
    const loading={label: play.textContent, disabled: play.disabled};
    code.textContent=edit; pre.querySelector('[data-act="rich-apply"]').click(); await wait(120);
    const afterApply={label: play.textContent, disabled: play.disabled, synth: !!out._abcSynth,
                      notes: out.querySelectorAll('.abcjs-note').length};
    if(!gate) return {error:'the stubbed synth never started loading'};
    gate(); await wait(120);
    const settled={label: play.textContent, synth: !!out._abcSynth, playing: !!out._abcPlaying,
                   started: window.__abcStarted, inits: window.__abcInits, disabled: play.disabled,
                   notes: out.querySelectorAll('.abcjs-note').length};
    return {loading, afterApply, settled};
  } finally { ABCJS.synth=orig; host.remove(); }
}
"""

# Playback lasts 1 s in the stub, so the end-of-tune timer fires at ~1.25 s.
STOPTIMER = "async () => {" + _BUILD + r"""
  const edit='X:1\nT:After\nM:4/4\nL:1/8\nK:G\nG A B c d e f g|g f e d c B A G|';
  const {host, wrap}=await build("```abc\nX:1\nT:Before\nM:4/4\nL:1/4\nK:C\nC D E F|\n```", '.abc-render-wrap');
  if(!wrap) return {error:'no abc card'};
  wrap.querySelector('[data-act="abc-src"]').click();
  const pre=wrap.querySelector('.abc-src-pre'), code=pre.querySelector('code');
  const out=wrap.querySelector('.abc-render-output');
  const play=wrap.querySelector('[data-act="abc-play"]');
  const orig=ABCJS.synth; window.__abcStarted=0; window.__abcStopped=0; window.__abcInits=0;
  ABCJS.synth=stubSynth(1.0);
  const state=()=>({label: play.textContent, playing: !!out._abcPlaying, started: window.__abcStarted, stopped: window.__abcStopped});
  try{
    // Stop then Play: the first start's timer must not end the second playback.
    play.click(); await wait(60);                 // start #1 (timer → ~1.25 s)
    const s1=state();
    play.click(); await wait(40);                 // Stop
    const s2=state();
    play.click(); await wait(60);                 // start #2 (timer → ~1.41 s)
    const s3=state();
    await wait(1150);                             // ≈1.31 s after start #1: its timer has fired, #2's has not
    const mid=state();
    await wait(400);                              // #2's own timer has now fired
    const ended=state();
    // Apply then Play: the old score's timer must not end the new score's playback.
    play.click(); await wait(60);                 // start #3 on the old score (timer → ~1.25 s)
    code.textContent=edit; pre.querySelector('[data-act="rich-apply"]').click(); await wait(120);
    const afterApply=state();
    play.click(); await wait(80);                 // start #4 on the NEW score
    const s4=state();
    await wait(1100);                             // #3's timer has fired, #4's has not
    const mid2=state();
    return {s1, s2, s3, mid, ended, afterApply, s4, mid2};
  } finally { ABCJS.synth=orig; host.remove(); }
}
"""

SIBLINGS = "async () => {" + _BUILD + r"""
  const {host}=await build("```plot\ny = x^2\n```\n\n```plot\ny = qqq\n```", 'div');
  const wraps=host.querySelectorAll('.plot-render-wrap');
  if(wraps.length!==2) return {error:'expected two plot cards, got '+wraps.length};
  const [A,B]=wraps;
  const st=w=>{ const out=w.querySelector('.plot-render-output'); return {rendered: out.dataset.rendered||'',
      sliders: w.querySelectorAll('.plot-params input[type=range]').length,
      text: out.textContent.replace(/\s+/g,' ').trim().slice(0,60)}; };
  // B failed to render (undefined symbol). Edit its Source but do NOT apply it.
  B.querySelector('[data-act="plot-src"]').click();
  B.querySelector('.plot-src-pre code').textContent='param: a = 1 .. 5 (2)\ny = a*x';
  const before={A: st(A), B: st(B)};
  A.querySelector('[data-act="plot-src"]').click();
  A.querySelector('.plot-src-pre code').textContent='y = 3*x';
  A.querySelector('[data-act="rich-apply"]').click(); await wait(200);
  const after={A: st(A), B: st(B)};
  host.remove();
  return {before, after};
}
"""

LIBS = "() => ({marked:!!window.marked, DOMPurify:!!window.DOMPurify, katex:!!window.katex, " \
       "ABCJS:!!window.ABCJS, math:!!window.math})"


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 900, "height": 700})
        pg.add_init_script(r"""
            window.fetch = () => Promise.resolve(new Response('{}',
              {status:200, headers:{'Content-Type':'application/json'}}));""")
        errors: list = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        try:
            pg.wait_for_function(
                "() => window.marked && window.DOMPurify && window.katex && window.ABCJS && window.math",
                timeout=30000)
        except Exception:  # noqa: BLE001 — reported below as missing libraries
            pass
        OUT["libs"] = pg.evaluate(LIBS)
        if not all(OUT["libs"].values()):
            OUT["skip"] = f"renderer libraries did not load from the CDN: {OUT['libs']}"
            b.close()
            return
        pg.wait_for_timeout(300)
        OUT["cases"] = {name: pg.evaluate(PROBE, case) for name, case in CASES.items()}
        # Real keystrokes, so the browser — not the probe — builds the edited nodes.
        typed_open = pg.evaluate(TYPED_OPEN)
        if "error" in typed_open:
            OUT["typed"] = typed_open
        else:
            pg.keyboard.type("param: a = 1 .. 5 (2)")
            pg.keyboard.press("Enter")
            pg.keyboard.type("y = a*sin(x)")
            OUT["typed"] = {**typed_open, **pg.evaluate(TYPED_APPLY)}
        OUT["maximized"] = pg.evaluate(MAXIMIZED)
        OUT["pending"] = pg.evaluate(PENDING)
        OUT["nolib"] = pg.evaluate(NOLIB)
        OUT["inflight"] = pg.evaluate(INFLIGHT)
        OUT["stoptimer"] = pg.evaluate(STOPTIMER)
        OUT["siblings"] = pg.evaluate(SIBLINGS)
        OUT["errors"] = errors[:5]
        OUT["ok"] = True
        b.close()


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]).resolve())
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT))
