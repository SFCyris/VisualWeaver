# SPDX-License-Identifier: AGPL-3.0-or-later
"""Interactive fractal navigation in a real browser: box-zoom + the depth control.

test_fractal_zoom_js.py covers the box-zoom geometry offline. This probe builds
a real ```fractal card the way a rendered message does and drives the actual DOM:

* box-zoom (mandelbrot/julia): drag a rectangle and confirm the overlay is shown
  ratio-locked to the canvas, the view zooms in and recenters, the overlay is
  removed on release and the Reset-zoom button appears; a plain click zooms in,
  shift-click zooms out;
* the depth control (every family): move the slider and confirm the spec field
  and the readout change and the card re-renders (gen grows / pixels change);
* an attractor Steps change re-integrates the orbit (not a stale cache);
* an over-deep L-system Depth reverts to the last good value instead of blanking.

Emits one line of JSON on stdout. Driven by tests/test_fractal_interact_browser.py.
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}

SPECS = {
    "mandelbrot": '{"type":"mandelbrot"}',
    "julia":      '{"type":"julia"}',
    "lsystem":    '{"type":"koch"}',
    "ifs_chaos":  '{"type":"fern"}',        # point-cloud IFS → Iterations
    "ifs_depth":  '{"type":"sierpinski"}',  # self-tiling IFS → structural Depth
    "attractor":  '{"type":"lorenz"}',
}
FIELD = {"mandelbrot": "iter", "julia": "iter", "lsystem": "depth",
         "ifs_chaos": "points", "ifs_depth": "depth", "attractor": "steps"}

_BUILD = r"""
  const build=async (body)=>{
    const host=document.createElement('div');
    host.className='msg-bubble ai'; host.style.width='620px';
    document.body.appendChild(host);
    host.innerHTML=renderMarkdown('```fractal\n'+body+'\n```',{math:true});
    renderRichBlocks(host);
    // 300ms: past the fractal's own ResizeObserver debounce (150ms) so the
    // initial fallback-size render has been corrected before we measure.
    await new Promise(r=>setTimeout(r,300));
    const wrap=host.querySelector('.rich-wrap[data-kind="fractal"]');
    return {host, wrap};
  };
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const inkOf=(canvas)=>{ const d=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
    let n=0; for(let i=0;i<d.length;i+=4){ if(d[i]||d[i+1]||d[i+2]) n++; } return n; };
  // A position-weighted checksum of the pixels: any visual change moves it, even
  // when the inked-pixel COUNT happens to match (e.g. an L-system at two depths).
  const sigOf=(canvas)=>{ const d=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
    let h=0; for(let i=0;i<d.length;i+=16){ h=(h*31 + d[i]+d[i+1]+d[i+2])>>>0; } return h; };
"""

DRIVE = "async (c) => {" + _BUILD + r"""
  const {host, wrap}=await build(c.body);
  if(!wrap) return {error:'no fractal card for '+c.body};
  const out=wrap.querySelector('.rich-output'), inst=out._richInst, canvas=out.querySelector('canvas');
  const hostDiv=canvas.parentElement;
  const r=canvas.getBoundingClientRect();
  const fire=(type,cx,cy,extra={})=>canvas.dispatchEvent(new PointerEvent(type,
      {clientX:r.left+cx,clientY:r.top+cy,button:0,bubbles:true,pointerId:1,...extra}));
  const res={type:inst.spec.type, view0:{...inst.view},
             ctl:!!wrap.querySelector('.frac-ctl'),
             ctlLabel:(wrap.querySelector('.frac-ctl label')||{}).textContent||null,
             cursor:canvas.style.cursor,
             canvasRatio:+(r.width/r.height).toFixed(3)};

  if(inst.spec.type==='mandelbrot'||inst.spec.type==='julia'){
    const g0=inst.gen;
    fire('pointerdown', r.width*0.25, r.height*0.25);
    fire('pointermove', r.width*0.55, r.height*0.40);
    const boxMid=hostDiv.querySelector('.frac-zoom-box');
    res.boxShown=!!boxMid;
    res.boxRatio=boxMid?+(parseFloat(boxMid.style.width)/parseFloat(boxMid.style.height)).toFixed(3):null;
    // the box must sit inside the canvas
    if(boxMid){ const L=parseFloat(boxMid.style.left),T=parseFloat(boxMid.style.top),
                      W=parseFloat(boxMid.style.width),H=parseFloat(boxMid.style.height);
      res.boxInside = L>=-0.5 && T>=-0.5 && L+W<=r.width+0.5 && T+H<=r.height+0.5; }
    fire('pointerup', r.width*0.55, r.height*0.40);
    res.boxGone=!hostDiv.querySelector('.frac-zoom-box');
    res.view1={...inst.view};
    res.zoomedIn=inst.view.zoom>res.view0.zoom;
    res.recentred=(inst.view.cx!==res.view0.cx)||(inst.view.cy!==res.view0.cy);
    res.genGrew=inst.gen>g0;
    const btn=wrap.querySelector('[data-act="fractal-reset"]');
    res.resetShown=btn?getComputedStyle(btn).display!=='none':null;
    const z1=inst.view.zoom;
    fire('pointerdown', r.width*0.5, r.height*0.5); fire('pointerup', r.width*0.5, r.height*0.5);
    res.clickZoomIn=inst.view.zoom>z1;
    const z2=inst.view.zoom;
    fire('pointerdown', r.width*0.5, r.height*0.5, {shiftKey:true}); fire('pointerup', r.width*0.5, r.height*0.5, {shiftKey:true});
    res.shiftZoomOut=inst.view.zoom<z2;
  }

  const inp=wrap.querySelector('.frac-ctl input[type=range]');
  if(inp){
    const f=c.field; res.field=f; res.min=+inp.min; res.max=+inp.max; res.before=inst.spec[f];
    const g0=inst.gen, sig0=sigOf(canvas);
    const target = f==='depth' ? Math.min(6, +inp.max) : Math.round((+inp.min + +inp.max)/2);
    inp.value=target; inp.dispatchEvent(new Event('input',{bubbles:true}));
    await wait(160); inp.dispatchEvent(new Event('change',{bubbles:true})); await wait(220);
    res.after=inst.spec[f]; res.outText=(wrap.querySelector('.frac-ctl output')||{}).textContent;
    res.viewKept = !res.view1 || (inst.view.zoom===res.view1.zoom);   // depth change must not reset the pan/zoom
    res.ctlGenGrew=inst.gen>g0;
    res.ctlSigChanged=sigOf(canvas)!==sig0;
  }
  host.remove();
  return res;
}
"""

ATTRACTOR = "async () => {" + _BUILD + r"""
  const {host, wrap}=await build('{"type":"lorenz","iter":6000}');
  const inst=wrap.querySelector('.rich-output')._richInst;
  const n0=inst._orbit?inst._orbit.n:null;
  const inp=wrap.querySelector('.frac-ctl input');
  inp.value=40000; inp.dispatchEvent(new Event('input',{bubbles:true}));
  await wait(200); inp.dispatchEvent(new Event('change',{bubbles:true})); await wait(220);
  const n1=inst._orbit?inst._orbit.n:null;
  host.remove();
  return {steps1:inst.spec.steps, orbitN0:n0, orbitN1:n1};
}
"""

LSYS_REVERT = "async () => {" + _BUILD + r"""
  const {host, wrap}=await build('{"type":"gosper","order":3}');
  const inst=wrap.querySelector('.rich-output')._richInst;
  const canvas=wrap.querySelector('.rich-output canvas');
  const before=inst.spec.depth, sigBefore=sigOf(canvas);
  const inp=wrap.querySelector('.frac-ctl input');
  // A LONE change event (no preceding input) so the catch's spec-revert is the
  // only thing that can restore the value — nothing re-applies a snapped-back
  // slider afterwards to mask a broken revert.
  inp.value=14; inp.dispatchEvent(new Event('change',{bubbles:true}));
  await wait(320);
  const r={before, requested:14, after:inst.spec.depth, inpVal:+inp.value,
           outText:(wrap.querySelector('.frac-ctl output')||{}).textContent,
           sigBefore, sigAfter:sigOf(canvas), sigRestored:sigOf(canvas)===sigBefore};
  host.remove();
  return r;
}
"""


RERENDER = "async () => {" + _BUILD + r"""
  const {host, wrap}=await build('{"type":"mandelbrot"}');
  const count=()=>wrap.querySelectorAll('.frac-ctl').length;
  const first=count();
  // Re-render in place twice, the way Apply/Reset on the Source pane does.
  _rerenderRichCard(wrap); await wait(120);
  _rerenderRichCard(wrap); await wait(120);
  const after=count();
  host.remove();
  return {first, after};
}
"""


RESIZE = "async () => {" + _BUILD + r"""
  const outer=document.createElement('div'); outer.style.width='620px'; document.body.appendChild(outer);
  outer.innerHTML='<div class="msg-bubble ai" style="width:100%">'+renderMarkdown('```fractal\n{"type":"mandelbrot"}\n```',{math:true})+'</div>';
  renderRichBlocks(outer); await wait(200);
  const wrap=outer.querySelector('.rich-wrap[data-kind="fractal"]');
  const inst=wrap.querySelector('.rich-output')._richInst;
  const canvas=wrap.querySelector('.rich-output canvas'), host=canvas.parentElement;
  const w0=canvas.width, h0=canvas.height;
  outer.style.width='380px';                         // shrink the card → host resizes
  await wait(400);                                    // past the 150ms debounce
  const r=canvas.getBoundingClientRect();
  const res={w0, h0, w1:canvas.width, h1:canvas.height, resized:canvas.width!==w0,
             backingAspect:+(canvas.width/canvas.height).toFixed(2),
             displayAspect:+(r.width/r.height).toFixed(2)};
  outer.remove();
  return res;
}
"""

STALE = "async () => {" + _BUILD + r"""
  const {host, wrap}=await build('{"type":"gosper","order":3}');
  const inst=wrap.querySelector('.rich-output')._richInst;
  const inp=wrap.querySelector('.frac-ctl input');
  // Spy on toast() itself — the app collapses a repeat message into a counter, so
  // counting .toast DOM nodes would miss a stale toast whose text already shows.
  const origToast=window.toast; let toastCount=0;
  window.toast=function(){ toastCount++; return origToast.apply(this,arguments); };
  try{
    // Schedule a debounced apply of an over-deep value, then tear the card down
    // (as Apply/Reset does) BEFORE the 70ms debounce fires.
    inp.value=14; inp.dispatchEvent(new Event('input',{bubbles:true}));
    const hadTimer=!!inst._ctlTimer;
    _rerenderRichCard(wrap);
    const timerCleared=!inst._ctlTimer;
    await wait(200);                    // past the 70ms debounce the stale timer would fire at
    return {hadTimer, timerCleared, spuriousToast:toastCount>0};
  } finally {
    window.toast=origToast;
    wrap.closest('.msg-bubble')?.remove(); host.remove?.();
  }
}
"""


IFS_STRUCTURE = "async () => {" + _BUILD + r"""
  const measure=async(depth)=>{
    const {host, wrap}=await build('{"type":"sierpinski"}');
    const inst=wrap.querySelector('.rich-output')._richInst;
    const canvas=wrap.querySelector('.rich-output canvas');
    const inp=wrap.querySelector('.frac-ctl input');
    inp.value=depth; inp.dispatchEvent(new Event('change',{bubbles:true}));
    await wait(300);
    const W=canvas.width,H=canvas.height, d=canvas.getContext('2d').getImageData(0,0,W,H).data;
    let ink=0,minx=1e9,maxx=-1e9,miny=1e9,maxy=-1e9; const G=8, occ=new Set();
    const bg=[d[0],d[1],d[2]];
    for(let y=0;y<H;y++)for(let x=0;x<W;x++){const o=(y*W+x)*4;
      if(Math.abs(d[o]-bg[0])+Math.abs(d[o+1]-bg[1])+Math.abs(d[o+2]-bg[2])>18){
        ink++; if(x<minx)minx=x;if(x>maxx)maxx=x;if(y<miny)miny=y;if(y>maxy)maxy=y;
        occ.add(((y*G/H)|0)*G+((x*G/W)|0));}}
    const bw=ink?(maxx-minx):0, bh=ink?(maxy-miny):0;
    const r={depth:inst.spec.depth, ink, bboxW:bw, bboxH:bh, W, H, gridCells:occ.size};
    host.remove(); return r;
  };
  const low=await measure(2), mid=await measure(6);
  return {low, mid};
}
"""


IFS_SEED = "async () => {" + _BUILD + r"""
  const bandRatio=async(spec)=>{
    const {host, wrap}=await build(spec);
    const inp=wrap.querySelector('.frac-ctl input'); inp.value=2; inp.dispatchEvent(new Event('change',{bubbles:true}));
    await wait(350);
    const c=wrap.querySelector('.rich-output canvas'), W=c.width,H=c.height,d=c.getContext('2d').getImageData(0,0,W,H).data,bg=[d[0],d[1],d[2]];
    let miny=1e9,maxy=-1e9; const rMin={},rMax={};
    for(let y=0;y<H;y++)for(let x=0;x<W;x++){const o=(y*W+x)*4;
      if(Math.abs(d[o]-bg[0])+Math.abs(d[o+1]-bg[1])+Math.abs(d[o+2]-bg[2])>18){
        if(y<miny)miny=y;if(y>maxy)maxy=y; if(rMin[y]===undefined||x<rMin[y])rMin[y]=x; if(rMax[y]===undefined||x>rMax[y])rMax[y]=x;}}
    const h=maxy-miny, band=(a,b)=>{let w=0;for(let y=a;y<=b;y++)if(rMax[y]!==undefined)w=Math.max(w,rMax[y]-rMin[y]);return w;};
    const topW=band(miny,miny+Math.round(h*0.10)), botW=band(maxy-Math.round(h*0.10),maxy);
    host.remove(); return {topW, botW, ratio: botW>0?topW/botW:1};
  };
  return {sierpinski: await bandRatio('{"type":"sierpinski"}')};
}
"""


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 940, "height": 820})
        pg.add_init_script("window.fetch=()=>Promise.resolve(new Response('{}',"
                           "{status:200,headers:{'Content-Type':'application/json'}}));")
        errors: list = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(400)
        OUT["cases"] = {name: pg.evaluate(DRIVE, {"body": SPECS[name], "field": FIELD[name]})
                        for name in SPECS}
        OUT["attractor_reintegrate"] = pg.evaluate(ATTRACTOR)
        OUT["lsystem_revert"] = pg.evaluate(LSYS_REVERT)
        OUT["rerender_dedupe"] = pg.evaluate(RERENDER)
        OUT["resize_rerender"] = pg.evaluate(RESIZE)
        OUT["stale_timer"] = pg.evaluate(STALE)
        OUT["ifs_structure"] = pg.evaluate(IFS_STRUCTURE)
        OUT["ifs_seed"] = pg.evaluate(IFS_SEED)
        OUT["errors"] = errors[:5]
        OUT["ok"] = True
        b.close()


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]).resolve())
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT))
