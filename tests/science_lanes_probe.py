# SPDX-License-Identifier: AGPL-3.0-or-later
"""The science / 3-D lanes in a real browser.

tests/test_science_lanes_js.py pins the pure helpers offline. This probe builds
each card the way a rendered message does and drives the actual DOM:

* ``plotly``   — a histogram draws bars, Apply swaps it for a box plot, Reset
                 brings the bars back, Maximize resizes the figure;
* ``ode``      — sliders re-integrate the portrait, Apply/Reset never stack a
                 second slider strip, an undeclared symbol is reported;
* ``scene``    — WebGL pixels are drawn, drag orbits, wheel zooms, shift-drag
                 pans, Reset view restores the camera, Maximize resizes the
                 renderer, teardown releases the GL context;
* ``sequence`` / ``phylo`` / ``reaction`` / ``circuit`` — the figure is drawn,
                 Apply/Reset re-render it in place;
* ``plot``     — parametric, polar, field, contour and implicit forms render
                 with the real math.js and list their definitions;
* ``mermaid``  — the additional diagram types render.

Emits one line of JSON on stdout. Driven by tests/test_science_lanes_browser.py.
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}

_BUILD = r"""
  const wait=ms=>new Promise(r=>setTimeout(r,ms));
  const until=async (fn,ms=25000)=>{ const t0=Date.now(); while(Date.now()-t0<ms){ try{ if(fn())return true; }catch(e){} await wait(50); } return false; };
  const errOf=out=>{ const e=out&&out.querySelector('.rich-err'); return e?e.textContent:null; };
  const build=async (lang, body, width)=>{
    const host=document.createElement('div');
    host.className='msg-bubble ai'; host.style.width=(width||700)+'px';
    document.body.appendChild(host);
    host.innerHTML=renderMarkdown('```'+lang+'\n'+body+'\n```',{math:true});
    renderPlotBlocks(host); renderRichBlocks(host); addCopyButtons(host);
    const wrap=host.querySelector('.rich-wrap, .plot-render-wrap');
    const out=wrap&&wrap.querySelector('.rich-output, .plot-render-output');
    // "finished": a rich card has its rendered flag and is no longer loading (or
    // shows its error); a plot card has its <svg> or its "Plot error:" text.
    const finished=()=>{ if(!out)return false; if(out.querySelector('.rich-err'))return true;
      if(wrap.classList.contains('plot-render-wrap')) return !!out.querySelector('svg')||/^Plot error/.test(out.textContent);
      return out.dataset.rendered==='1'&&!out.classList.contains('rich-loading'); };
    const done=await until(finished);
    await wait(250);
    const err=!done?'timed out waiting for the card to render':(errOf(out)||(/^Plot error/.test(out.textContent)?out.textContent.slice(0,160):null));
    return {host, wrap, out, err};
  };
  const settled=(wrap)=>{ const out=wrap.querySelector('.rich-output'); return !out.classList.contains('rich-loading'); };
  const applySrc=async (wrap, text, ready)=>{
    const pre=wrap.querySelector('.rich-src-pre');
    if(getComputedStyle(pre).display==='none') wrap.querySelector('[data-act="rich-src"]').click();
    pre.querySelector('code').textContent=text;
    pre.querySelector('[data-act="rich-apply"]').click();
    const ok=await until(()=>settled(wrap)&&ready()); await wait(250); return ok;
  };
  const resetSrc=async (wrap, ready)=>{
    wrap.querySelector('.rich-src-pre [data-act="rich-reset"]').click();
    const ok=await until(()=>settled(wrap)&&ready()); await wait(250); return ok;
  };
  // Opaque-pixel count and a position-weighted checksum of a (WebGL) canvas,
  // read through a 2-D copy so preserveDrawingBuffer is exercised too.
  // (the renderer keeps no drawing buffer between frames, so draw first)
  const canvasInk=(canvas,inst)=>{ if(inst&&inst.render)inst.render(); const c=document.createElement('canvas'); c.width=canvas.width; c.height=canvas.height;
    const ctx=c.getContext('2d'); ctx.drawImage(canvas,0,0);
    const d=ctx.getImageData(0,0,c.width,c.height).data; let n=0,h=0;
    for(let i=0;i<d.length;i+=16){ if(d[i+3]>0){ n++; h=(h*31+d[i]+d[i+1]+d[i+2])>>>0; } } return {n,h}; };
"""

PLOTLY = "async () => {" + _BUILD + r"""
  const r={};
  const {host,wrap,out,err}=await build('plotly', JSON.stringify({data:[{type:'histogram',x:[1,2,2,3,3,3,4,4,5,5,5,5,6]}],layout:{title:'Response times'}}));
  if(err){ host.remove(); return {error:err}; }
  r.kind=out._richInst&&out._richInst.kind;
  r.plotDiv=!!out.querySelector('.js-plotly-plot');
  r.bars=out.querySelectorAll('.trace.bars .point path').length;
  r.title=(out.querySelector('.gtitle')||{}).textContent||'';
  r.h=Math.round(out.getBoundingClientRect().height);
  r.pngBtn=!!wrap.querySelector('[data-act="plot3d-png"]');
  r.applied=await applySrc(wrap, JSON.stringify({data:[{type:'box',y:[1,2,2,3,3,3,4,9]}]}), ()=>out.querySelector('.trace.boxes'));
  r.afterBoxes=out.querySelectorAll('.trace.boxes').length;
  r.afterBars=out.querySelectorAll('.trace.bars .point path').length;
  r.plotDivs=out.querySelectorAll('.js-plotly-plot').length;
  r.reset=await resetSrc(wrap, ()=>out.querySelector('.trace.bars .point path'));
  r.resetBars=out.querySelectorAll('.trace.bars .point path').length;
  const cont=()=>out.querySelector('.svg-container');
  r.w0=Math.round(cont()?cont().getBoundingClientRect().width:0);
  wrap.querySelector('[data-act="viz-max"]').click(); await wait(600);
  r.maxW=Math.round(cont()?cont().getBoundingClientRect().width:0);
  closeMaximize(); await wait(400);
  r.restoredW=Math.round(cont()?cont().getBoundingClientRect().width:0);
  host.remove(); return r;
}
"""

ODE = "async () => {" + _BUILD + r"""
  const r={};
  const {host,wrap,out,err}=await build('ode', 'dx = y\ndy = -k*x - c*y\nparam: k = 0.5 .. 2 (1)\nparam: c = 0 .. 1 (0.2)\nstart: 2, 0');
  if(err){ host.remove(); return {error:err}; }
  const traj=()=>out.querySelector('path[stroke="#2563eb"]');
  r.svg=!!out.querySelector('svg'); r.traj=!!traj();
  r.sliders=wrap.querySelectorAll('.plot-params input[type=range]').length;
  r.strips=wrap.querySelectorAll('.plot-params').length;
  const d0=traj().getAttribute('d');
  const inp=wrap.querySelectorAll('.plot-params input[type=range]')[1];
  inp.value=0.9; inp.dispatchEvent(new Event('input',{bubbles:true})); await wait(450);
  r.dChanged=traj().getAttribute('d')!==d0;
  r.stripsAfterSlide=wrap.querySelectorAll('.plot-params').length;
  r.applied=await applySrc(wrap,'dx = 1\ndy = 0', ()=>out.querySelector('svg')&&!wrap.querySelector('.plot-params'));
  r.stripsAfterApply=wrap.querySelectorAll('.plot-params').length;
  const lines=[...out.querySelectorAll('g[stroke="#94a3b8"] line')];
  r.arrows=lines.length; r.horizontal=lines.length>0&&lines.every(l=>l.getAttribute('y1')===l.getAttribute('y2'));
  r.reset=await resetSrc(wrap, ()=>wrap.querySelectorAll('.plot-params input[type=range]').length===2);
  r.stripsAfterReset=wrap.querySelectorAll('.plot-params').length;
  r.slidersAfterReset=wrap.querySelectorAll('.plot-params input[type=range]').length;
  // a second Apply of a parametrised system must not stack a second strip
  r.applied2=await applySrc(wrap,'dx = y\ndy = -q*x\nparam: q = 1 .. 3 (2)', ()=>wrap.querySelectorAll('.plot-params input[type=range]').length===1);
  r.stripsAfterApply2=wrap.querySelectorAll('.plot-params').length;
  r.applied3=await applySrc(wrap,'dx = z*y\ndy = -x', ()=>out.querySelector('.rich-err'));
  r.undefinedMsg=(out.querySelector('.rich-err')||{}).textContent||'';
  r.stripsAfterError=wrap.querySelectorAll('.plot-params').length;
  await resetSrc(wrap, ()=>out.querySelector('svg'));
  r.outH=Math.round(out.getBoundingClientRect().height); r.svgH=Math.round(out.querySelector('svg').getBoundingClientRect().height);
  host.remove(); return r;
}
"""

SCENE = "async () => {" + _BUILD + r"""
  const r={};
  const specStr=JSON.stringify({objects:[
      {type:'box',size:[2,0.2,1],color:'#4f8fe6'},
      {type:'sphere',radius:0.5,position:[0,0.8,0],color:'tomato'},
      {type:'cylinder',radius:0.3,height:1,position:[1.2,0.5,0]}],axes:true});
  const {host,wrap,out,err}=await build('scene', specStr);
  if(err){ host.remove(); return {error:err}; }
  const inst=out._richInst, canvas=out.querySelector('canvas');
  if(!inst||!canvas){ host.remove(); return {error:'no scene instance / canvas'}; }
  r.kind=inst.kind; r.cw=canvas.width; r.ch=canvas.height;
  r.cssW=Math.round(canvas.getBoundingClientRect().width); r.hostW=Math.round(out.getBoundingClientRect().width);
  const ink0=canvasInk(canvas,inst); r.ink=ink0.n;
  r.touchAction=getComputedStyle(canvas).touchAction; r.role=canvas.getAttribute('role'); r.tabIndex=canvas.tabIndex; r.aria=canvas.getAttribute('aria-label')||'';
  const hintEl=inst.host.querySelector('.scene-hint'); r.hint0=!!hintEl&&!hintEl.hidden&&getComputedStyle(hintEl).display!=='none';
  // the "paused" overlay must not be PAINTED over a live scene (its hidden attribute
  // alone proved nothing: a display:flex rule beat it), and the canvas must be what
  // the pointer actually lands on
  canvas.scrollIntoView({block:'center'}); await wait(80);
  r.pausedDisplay0=getComputedStyle(inst.host.querySelector('.scene-paused')).display;
  { const cr=canvas.getBoundingClientRect(); const hit=document.elementFromPoint(cr.left+cr.width/2,cr.top+cr.height/2); r.hitIsCanvas=hit===canvas; }
  const ctl=wrap.querySelector('.scene-ctl'), sliders=ctl?[...ctl.querySelectorAll('input[type=range]')]:[], spinBtn=ctl&&ctl.querySelector('.scene-ctl-spin');
  r.strips=wrap.querySelectorAll('.scene-ctl').length; r.sliderLabels=sliders.map(i=>i.getAttribute('aria-label')); r.spinBtn=!!spinBtn;
  r.rotSlider0=sliders[0]?Number(sliders[0].value):null;
  const wrapDeg=d=>((d+180)%360+360)%360-180;
  const rect=canvas.getBoundingClientRect();
  const fire=(type,x,y,extra={})=>canvas.dispatchEvent(new PointerEvent(type,{clientX:rect.left+x,clientY:rect.top+y,button:0,bubbles:true,pointerId:1,...extra}));
  const p0=inst.camera.position.clone(), t0=inst.target.clone();
  const resetBtn=wrap.querySelector('[data-act="scene-reset"]');
  r.resetHidden0=getComputedStyle(resetBtn).display==='none'; r.cursor0=canvas.style.cursor;
  fire('pointerdown',100,100); r.cursorDrag=canvas.style.cursor;
  fire('pointermove',160,120); fire('pointerup',160,120);
  r.orbited=inst.camera.position.distanceTo(p0)>1e-3; r.interacted=inst.interacted;
  r.resetShown=getComputedStyle(resetBtn).display!=='none'; r.cursorAfter=canvas.style.cursor;
  r.pixelsChanged=canvasInk(canvas,inst).h!==ink0.h;
  r.hintHidden=hintEl.hidden;
  r.rotSliderMoved=Number(sliders[0].value)!==r.rotSlider0;
  r.sliderFollowsOrbit=Number(sliders[0].value)===wrapDeg(Math.round(inst.sph.theta*180/Math.PI));
  const rad0=inst.sph.radius;
  canvas.dispatchEvent(new WheelEvent('wheel',{deltaY:300,bubbles:true,cancelable:true}));
  r.zoomedOut=inst.sph.radius>rad0;
  fire('pointerdown',100,100,{shiftKey:true}); fire('pointermove',140,100,{shiftKey:true}); fire('pointerup',140,100,{shiftKey:true});
  r.panned=inst.target.distanceTo(t0)>1e-4;
  resetBtn.click(); await wait(80);
  r.resetCamera=inst.camera.position.distanceTo(p0)<1e-6; r.resetTarget=inst.target.distanceTo(t0)<1e-9;
  r.resetHiddenAgain=getComputedStyle(resetBtn).display==='none'; r.interactedAfterReset=inst.interacted;
  { sliders[0].value=90; sliders[0].dispatchEvent(new Event('input',{bubbles:true})); r.sliderSetsTheta=Math.abs(inst.sph.theta-Math.PI/2)<1e-6;
    const z0=inst.sph.radius; sliders[2].value=Math.min(100,Number(sliders[2].value)+30); sliders[2].dispatchEvent(new Event('input',{bubbles:true})); r.zoomSliderZoomsIn=inst.sph.radius<z0;
    r.sliderInkChanged=canvasInk(canvas,inst).h!==ink0.h;
    spinBtn.click(); const ts=inst.sph.theta; await wait(300); r.spun=inst.sph.theta!==ts; r.spinPressed=spinBtn.getAttribute('aria-pressed');
    fire('pointerdown',50,50); fire('pointerup',50,50); r.spinStoppedOnPointer=spinBtn.getAttribute('aria-pressed')==='false'&&!inst._spinRaf;
    spinBtn.click(); await wait(120); r.spinAgain=!!inst._spinRaf; spinBtn.click(); r.spinToggledOff=!inst._spinRaf;
    inst.reset(); resetBtn.style.display='none'; inst.interacted=false; }
  r.pngBtn=!!wrap.querySelector('[data-act="scene-png"]');
  inst.render(); r.png=canvas.toDataURL('image/png').length>2000;
  // keyboard orbit
  const th0=inst.sph.theta; canvas.focus(); canvas.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true,cancelable:true}));
  r.keyOrbit=inst.sph.theta!==th0; inst.reset(); resetBtn.style.display='none'; inst.interacted=false;
  // theme: lights and grid are re-applied
  const hemi=inst.scene.children.find(c=>c.isHemisphereLight); const hi0=hemi.intensity;
  applyTheme('dark'); await wait(50); r.darkHemi=hemi.intensity; applyTheme('light'); await wait(50); r.lightHemi=hemi.intensity; r.hemi0=hi0;
  // the canvas follows its card's width (a sidebar toggle, a window resize)
  host.style.width='380px'; await wait(450); r.resizedCw=canvas.width; r.resizedAspect=+inst.camera.aspect.toFixed(2);
  host.style.width='700px'; await wait(450); r.regrownCw=canvas.width;
  // a lost WebGL context pauses the view and a restore repaints it
  const pausedEl=inst.host.querySelector('.scene-paused'); const ext=inst.renderer.getContext().getExtension('WEBGL_lose_context');
  if(ext){ ext.loseContext(); await wait(150); r.pausedShown=!pausedEl.hidden; r.pausedDisplayLost=getComputedStyle(pausedEl).display;
    ext.restoreContext(); await wait(500); r.pausedHidden=pausedEl.hidden; r.pausedDisplayRestored=getComputedStyle(pausedEl).display; r.inkAfterRestore=canvasInk(canvas,inst).n; }
  wrap.querySelector('[data-act="viz-max"]').click(); await wait(600);
  r.maxCw=canvas.width; r.maxInk=canvasInk(canvas,inst).n;
  closeMaximize(); await wait(400);
  r.restoredCw=canvas.width;
  r.applied=await applySrc(wrap, specStr, ()=>out.querySelector('canvas'));
  r.stripsAfterApply=wrap.querySelectorAll('.scene-ctl').length;
  const inst2=out._richInst; inst2._ctl.querySelector('.scene-ctl-spin').click(); await wait(60); r.spinBeforeTeardown=!!inst2._spinRaf;
  const gl=inst2.renderer.getContext();
  destroyRichBlocks(host); await wait(80);
  r.contextLost=gl.isContextLost(); r.stripAfterTeardown=wrap.querySelectorAll('.scene-ctl').length; r.spinAfterTeardown=!!inst2._spinRaf;
  host.remove(); return r;
}
"""

SCENE_EDGE = "async () => {" + _BUILD + r"""
  const r={};
  const a=await build('scene', JSON.stringify({objects:[{type:'box',scale:2},{type:'sphere',color:'not-a-color'}]}));
  if(a.err){ a.host.remove(); return {error:a.err}; }
  const meshes=a.out._richInst.scene.children.filter(c=>c.isMesh);
  r.scaleX=meshes[0].scale.x; r.sphereColor=meshes[1].material.color.getHexString();
  destroyRichBlocks(a.host); a.host.remove();
  const b=await build('scene', JSON.stringify({objects:[{type:'pyramid'}]})); r.unknownErr=b.err; destroyRichBlocks(b.host); b.host.remove();
  const c=await build('scene', JSON.stringify({objects:[{type:'box',width:2,height:1,depth:1}]}));
  if(c.err){ c.host.remove(); return {error:c.err}; }
  r.boxW=c.out._richInst.scene.children.find(x=>x.isMesh).geometry.parameters.width;
  destroyRichBlocks(c.host); c.host.remove();
  const d=await build('scene', JSON.stringify({objects:[{type:'sphere',radius:1e7}]})); r.hugeErr=d.err; r.hugeDrawn=!!d.out.querySelector('canvas'); if(!d.err)destroyRichBlocks(d.host); d.host.remove();
  return r;
}
"""

SCENE_MOUSE_BUILD = "async () => {" + _BUILD + r"""
  const {host,wrap,out,err}=await build('scene', JSON.stringify({objects:[{type:'box',size:[2,0.2,1]},{type:'sphere',radius:0.5,position:[0,0.8,0],color:'tomato'}]}));
  if(err){ host.remove(); return {error:err}; }
  const inst=out._richInst, canvas=out.querySelector('canvas');
  canvas.scrollIntoView({block:'center'}); await wait(100);
  const cr=canvas.getBoundingClientRect();
  window.__sm={host,inst,canvas};
  return {x:cr.left+cr.width/2, y:cr.top+cr.height/2, theta0:inst.sph.theta};
}
"""

SCENE_MOUSE_READ = "() => {" + r"""
  const {host,inst}=window.__sm; const sl=[...inst._ctl.querySelectorAll('input[type=range]')];
  const wrapDeg=d=>((d+180)%360+360)%360-180;
  const r={theta1:inst.sph.theta, rotSlider:Number(sl[0].value), sliderMatches:Number(sl[0].value)===wrapDeg(Math.round(inst.sph.theta*180/Math.PI)), interacted:inst.interacted};
  destroyRichBlocks(host); host.remove(); return r;
}
"""

SEQUENCE = "async () => {" + _BUILD + r"""
  const r={};
  const one=await build('sequence','>gene\nATGGCCATTGTAATGGGCCGCTGAAAGGGTGCCCGATAG');
  if(one.err){ one.host.remove(); return {error:one.err}; }
  r.rects=one.out.querySelectorAll('rect').length-1;
  r.svgW=Math.round(one.out.querySelector('svg').getBoundingClientRect().width);
  r.pngBtn=!!one.wrap.querySelector('[data-act="rich-png"]');
  r.applied=await applySrc(one.wrap,'ACGTACGT', ()=>one.out.querySelectorAll('rect').length===9);
  r.rectsAfter=one.out.querySelectorAll('rect').length-1;
  r.reset=await resetSrc(one.wrap, ()=>one.out.querySelectorAll('rect').length===40);
  r.rectsReset=one.out.querySelectorAll('rect').length-1;
  one.host.remove();
  const aln=await build('sequence','>human\nMKTAYIAKQR\n>mouse\nMKTAYVAKQR');
  if(aln.err){ aln.host.remove(); return {error:aln.err}; }
  const marks=[...aln.out.querySelectorAll('text')].map(t=>t.textContent);
  r.stars=marks.filter(t=>t==='*').length; r.dots=marks.filter(t=>t==='·').length;
  r.names=marks.filter(t=>t==='human'||t==='mouse').length;
  aln.host.remove();
  // a mermaid sequence DIAGRAM in this fence is drawn as the diagram it is
  const mm=await build('sequence','sequenceDiagram\n  Alice->>Bob: Hello\n  Bob-->>Alice: Hi');
  r.diagramErr=mm.err; r.diagramSvg=!!mm.out.querySelector('svg'); r.diagramText=mm.out.textContent.includes('Alice')&&mm.out.textContent.includes('Hello');
  r.diagramCells=mm.out.querySelectorAll('rect[rx="2"]').length;
  mm.host.remove();
  const bad=await build('sequence','A->>B: hi'); r.badErr=bad.err; bad.host.remove();
  // a gene of a few kb scrolls inside a capped card instead of a 2,000-px figure
  const long=await build('sequence','>gene\n'+'ACGT'.repeat(600));
  r.longErr=long.err; r.longH=Math.round(long.out.getBoundingClientRect().height); r.longScroll=long.out.scrollHeight; long.host.remove();
  return r;
}
"""

PHYLO = "async () => {" + _BUILD + r"""
  const r={};
  const {host,wrap,out,err}=await build('phylo','((Human:0.1,Chimp:0.12):0.3,(Mouse:0.4,Rat:0.38):0.2);');
  if(err){ host.remove(); return {error:err}; }
  r.leaves=out.querySelectorAll('circle').length;
  r.texts=[...out.querySelectorAll('text')].map(t=>t.textContent);
  r.svgW=Math.round(out.querySelector('svg').getBoundingClientRect().width);
  r.applied=await applySrc(wrap,'((A,B),(C,(D,E)));', ()=>out.querySelectorAll('circle').length===5);
  r.leavesAfter=out.querySelectorAll('circle').length; r.textsAfter=out.querySelectorAll('text').length;
  r.reset=await resetSrc(wrap, ()=>out.querySelectorAll('circle').length===4);
  r.leavesReset=out.querySelectorAll('circle').length;
  host.remove(); return r;
}
"""

REACTION = "async () => {" + _BUILD + r"""
  const r={};
  const {host,wrap,out,err}=await build('reaction','CC(=O)O.OCC>[H+]>CC(=O)OCC.O');
  if(err){ host.remove(); return {error:err}; }
  const mols=[...out.querySelectorAll('.rxn-mol svg')];
  r.mols=mols.length; r.drawn=mols.filter(s=>s.childElementCount>0).length;
  r.signs=out.querySelectorAll('.rxn-sign').length;
  r.agents=(out.querySelector('.rxn-agents')||{}).textContent||'';
  r.arrow=!!out.querySelector('.rxn-arrowline');
  const boxes=[...out.querySelectorAll('.rxn-mol')].map(b=>b.getBoundingClientRect());
  r.molMinW=Math.round(Math.min(...boxes.map(b=>b.width))); r.molMinH=Math.round(Math.min(...boxes.map(b=>b.height)));
  r.arrowW=Math.round(out.querySelector('.rxn-arrow').getBoundingClientRect().width);
  r.inCard=boxes.every(b=>b.right<=out.getBoundingClientRect().right+1&&b.bottom<=out.getBoundingClientRect().bottom+1);
  r.applied=await applySrc(wrap,'CCO', ()=>out.querySelector('.rich-err'));
  r.errMsg=(out.querySelector('.rich-err')||{}).textContent||'';
  // an unbalanced species: SmilesDrawer draws nothing and swallows the error
  r.appliedBad=await applySrc(wrap,'C(C>>O', ()=>out.querySelector('.rich-err'));
  r.errMsgBad=(out.querySelector('.rich-err')||{}).textContent||'';
  r.reset=await resetSrc(wrap, ()=>out.querySelectorAll('.rxn-mol svg').length===4);
  r.molsReset=out.querySelectorAll('.rxn-mol svg').length;
  host.remove();
  // two species get large structures; a maximized card redraws them larger still
  const two=await build('reaction','CCO>>CC=O');
  if(two.err){ two.host.remove(); return {error:two.err}; }
  const molW=()=>Math.min(...[...two.out.querySelectorAll('.rxn-mol')].map(b=>b.getBoundingClientRect().width));
  r.twoW=Math.round(molW());
  two.wrap.querySelector('[data-act="viz-max"]').click(); await wait(700); r.twoMaxW=Math.round(molW()); r.twoMaxDrawn=[...two.out.querySelectorAll('.rxn-mol svg')].every(s=>s.childElementCount>0);
  closeMaximize(); await wait(500); r.twoBackW=Math.round(molW());
  two.host.remove();
  return r;
}
"""

CIRCUIT = "async () => {" + _BUILD + r"""
  const r={};
  const base={components:[{type:'battery',from:[0,2],to:[0,0],label:'9 V'},{type:'resistor',from:[0,0],to:[3,0],label:'R1 220 Ω'},{type:'led',from:[3,0],to:[3,2]},{type:'wire',from:[3,2],to:[0,2]}]};
  const {host,wrap,out,err}=await build('circuit', JSON.stringify(base));
  if(err){ host.remove(); return {error:err}; }
  r.symbols=out.querySelectorAll('g[transform]').length;
  r.labels=[...out.querySelectorAll('text')].map(t=>t.textContent);
  r.svgW=Math.round(out.querySelector('svg').getBoundingClientRect().width);
  r.pngBtn=!!wrap.querySelector('[data-act="rich-png"]');
  const more={components:[...base.components,{type:'capacitor',from:[1.5,0],to:[1.5,2],label:'C1'}]};
  r.applied=await applySrc(wrap, JSON.stringify(more), ()=>out.querySelectorAll('g[transform]').length===4);
  r.symbolsAfter=out.querySelectorAll('g[transform]').length;
  r.junctions=out.querySelectorAll('circle[r="3.5"]').length;
  r.reset=await resetSrc(wrap, ()=>out.querySelectorAll('g[transform]').length===3);
  r.symbolsReset=out.querySelectorAll('g[transform]').length;
  host.remove(); return r;
}
"""

PLOT = "async () => {" + _BUILD + r"""
  const cases={
    parametric:'x = cos(t)\ny = sin(2*t)\nt = 0 .. 2*pi',
    polar:'r = 1 + cos(theta)',
    field:'field: -y, x\nx = -2 .. 2\ny = -2 .. 2',
    contour:'contour: x^2 - y^2\nx = -2 .. 2',
    implicit:'implicit: x^2 + y^2 = 4\nx = -3 .. 3\ny = -3 .. 3',
    mixed:'y = sin(x)\nfield: 1, cos(x)\nx = -6 .. 6',
    slider:'x = a*cos(t)\ny = sin(t)\nparam: a = 1 .. 3 (2)',
  };
  const r={};
  for(const [name,src] of Object.entries(cases)){
    const {host,wrap,out,err}=await build('plot', src);
    const svg=out&&out.querySelector('svg');
    const defs=host.querySelector('.plot-defs-block');
    r[name]={ svg:!!svg, err: err||(svg?null:(out?out.textContent.trim().slice(0,160):'no output')),
      curves: svg?svg.querySelectorAll('path[stroke="#2563eb"],path[stroke="#dc2626"]').length:0,
      arrows: svg?svg.querySelectorAll('g[stroke="#64748b"] line').length:0,
      levels: svg?[...svg.querySelectorAll('path')].filter(p=>(p.getAttribute('stroke')||'').startsWith('hsl(')).length:0,
      defs: defs?defs.textContent.replace(/\s+/g,' ').trim():null,
      defColors: defs?[...defs.querySelectorAll('.plot-def')].map(d=>getComputedStyle(d).color):[],
      sliders: wrap?wrap.parentElement.querySelectorAll('.plot-params input[type=range]').length:0 };
    host.remove();
  }
  return r;
}
"""

MERMAID = "async () => {" + _BUILD + r"""
  const cases={
    mindmap:'mindmap\n  root((VisualWeaver))\n    Chat\n    Rendering\n      Charts\n      3D',
    quadrant:'quadrantChart\n    title Reach and engagement\n    x-axis Low Reach --> High Reach\n    y-axis Low Engagement --> High Engagement\n    quadrant-1 Expand\n    quadrant-2 Promote\n    quadrant-3 Re-evaluate\n    quadrant-4 Improve\n    Campaign A: [0.3, 0.6]\n    Campaign B: [0.45, 0.23]',
    xychart:'xychart-beta\n    title "Sales"\n    x-axis [jan, feb, mar]\n    y-axis "Revenue" 0 --> 100\n    bar [50, 60, 70]\n    line [40, 55, 65]',
    sankey:'sankey-beta\n\nA,B,10\nA,C,5\nB,D,8',
    block:'block-beta\n  columns 3\n  a b c\n  d:2 e',
    kanban:'kanban\n  Todo\n    t1[Write docs]\n  Done\n    t2[Ship]',
  };
  const r={};
  for(const [name,src] of Object.entries(cases)){
    const {host,out,err}=await build('mermaid', src);
    const svg=out&&out.querySelector('svg');
    r[name]={ svg:!!svg, children: svg?svg.childElementCount:0, err: err||null,
      syntaxError: !!(out&&/syntax error/i.test(out.textContent)),
      h: svg?Math.round(svg.getBoundingClientRect().height):0 };
    host.remove();
  }
  return r;
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
        for key, js in (("plotly", PLOTLY), ("ode", ODE), ("scene", SCENE), ("scene_edge", SCENE_EDGE), ("sequence", SEQUENCE),
                        ("phylo", PHYLO), ("reaction", REACTION), ("circuit", CIRCUIT),
                        ("plot", PLOT), ("mermaid", MERMAID)):
            try:
                OUT[key] = pg.evaluate(js)
            except Exception as exc:                  # noqa: BLE001 — reported per case
                OUT[key] = {"error": f"{type(exc).__name__}: {exc}"}
        # A REAL mouse drag through hit-testing — element.dispatchEvent on the canvas
        # bypassed the overlay that was painted over every live scene in 1.13.0.
        try:
            m0 = pg.evaluate(SCENE_MOUSE_BUILD)
            if "error" in m0:
                OUT["scene_mouse"] = m0
            else:
                x, y = m0["x"], m0["y"]
                pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x + 80, y + 20, steps=6); pg.mouse.up()
                pg.wait_for_timeout(150)
                OUT["scene_mouse"] = {**m0, **pg.evaluate(SCENE_MOUSE_READ)}
        except Exception as exc:                  # noqa: BLE001
            OUT["scene_mouse"] = {"error": f"{type(exc).__name__}: {exc}"}
        OUT["errors"] = errors[:5]
        OUT["ok"] = True
        b.close()


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]).resolve())
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT))
