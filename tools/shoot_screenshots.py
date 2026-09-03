#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Re-capture every screenshot in screenshots/ against a running VisualWeaver.

    python3 tools/shoot_screenshots.py                 # all of them
    python3 tools/shoot_screenshots.py welcome cache   # only shots matching a substring
    python3 tools/shoot_screenshots.py --list
    python3 tools/shoot_screenshots.py --url http://127.0.0.1:8420 --out /tmp/shots

Requires playwright (`pip install playwright && playwright install chromium`).

The capture is READ-ONLY, and deterministic except for `map.png`, which draws
live OpenStreetMap tiles and so changes with the tile server: sample conversations
are injected
straight into the render pipeline through appendMessage(), so no provider is
called and no answer has to be waited for, and nothing here saves settings,
ingests a document or starts a crawl. Panels that only exist mid-operation
(ingest progress, crawl progress, toasts) are driven with synthetic values.

Everything is shot at a 1280x800 viewport with deviceScaleFactor 2, so a
full-window frame lands at 2560x1600 and a clipped panel at twice its CSS size.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "screenshots"
DEFAULT_URL = "http://127.0.0.1:8420"
VIEWPORT = {"width": 1280, "height": 800}
SCALE = 2

# ── sample content ───────────────────────────────────────────────────────────
# Written out here rather than asked of a model: a docs screenshot has to be
# reproducible, and an answer regenerated on each run would reshuffle the page
# and change the crop under every caption.

MERMAID = """Here is how a question flows through the app:

```mermaid
flowchart TD
  A[User question] --> B{In cache?}
  B -- yes --> C[Return cached answer]
  B -- no --> D[RAG vector search]
  D --> E[LLM generates answer]
  E --> F[Store in semantic cache]
```
"""

CHART = """Quarterly sales came in as follows:

```chart
{"type":"bar",
 "data":{"labels":["Q1","Q2","Q3","Q4"],
         "datasets":[{"label":"Sales (k USD)","data":[118,152,89,208]}]},
 "options":{"plugins":{"title":{"display":true,"text":"Quarterly sales"}}}}
```
"""

CHART_DATA = """Cache hits tracked against total queries:

```chart
{"type":"line",
 "data":{"labels":["Jan","Feb","Mar","Apr","May","Jun"],
         "datasets":[{"label":"Queries","data":[120,110,76,230,180,300]},
                     {"label":"Cache hits","data":[30,55,48,180,140,240]}]}}
```
"""

DOT = """```dot
digraph {
  rankdir=LR; node [shape=box, style=rounded];
  Browser -> FastAPI [label="WebSocket"];
  FastAPI -> Redis [label="vector search"];
  FastAPI -> LLM [label="prompt"];
}
```
"""

GEOMETRY = """```geometry
{"boundingbox":[-1,5,7,-2],"axis":true,"elements":[
 {"type":"point","args":[1,0],"attrs":{"name":"A","size":3}},
 {"type":"point","args":[6,0],"attrs":{"name":"B","size":3}},
 {"type":"point","args":[1,3],"attrs":{"name":"C","size":3}},
 {"type":"polygon","args":[[1,0],[6,0],[1,3]],"attrs":{"fillColor":"#4f6ef7","fillOpacity":0.18}},
 {"type":"circle","args":[[1,0],1],"attrs":{"strokeColor":"#e0245e","dash":2}}
]}
```
"""

MAP = """```map
{"center":[52.5200,13.4050],"zoom":11,
 "markers":[{"lat":52.5200,"lng":13.4050,"label":"Berlin"},
            {"lat":52.5300,"lng":13.3850,"label":"Mitte"}]}
```
"""

PLOT3D = """```plot3d
{"data":[{"type":"surface","showscale":false,
  "z":[[0,1,2,3,4],[1,2,3,4,3],[2,3,4,3,2],[3,4,3,2,1],[4,3,2,1,0]]}]}
```
"""

MOLECULE = "```molecule\nCC(=O)Oc1ccccc1C(=O)O\n```\n"

MOLECULE3D = """```molecule3d
5
methane
C   0.000   0.000   0.000
H   0.629   0.629   0.629
H  -0.629  -0.629   0.629
H   0.629  -0.629  -0.629
H  -0.629   0.629  -0.629
```
"""

GANTT = """```gantt
title Release 1.5
dateFormat YYYY-MM-DD
section Retrieval
chunker rewrite    :a1, 2026-01-08, 10d
embedding swap     :a2, after a1, 8d
section Quality
regression suite   :b1, 2026-01-20, 9d
QA pass            :b2, after b1, 6d
```
"""

# Two milestones, not three: a mermaid timeline lays its periods out at a fixed
# width and a third one overflows the 560px card, cropping the last label
# mid-word.
TIMELINE = """```timeline
title Retrieval roadmap
2026-01 : project start : semantic cache
2026-04 : hybrid retrieval : reranking
```
"""

NETWORK = """```network
{"nodes":[{"id":"Browser"},{"id":"FastAPI"},{"id":"Redis 8"},{"id":"LLM"},
          {"id":"e5-small"},{"id":"BM25"},{"id":"KNN"}],
 "edges":[{"from":"Browser","to":"FastAPI"},{"from":"FastAPI","to":"Redis 8"},
          {"from":"FastAPI","to":"LLM"},{"from":"Redis 8","to":"BM25"},
          {"from":"Redis 8","to":"KNN"},{"from":"KNN","to":"e5-small"}],
 "options":{"layout":{"randomSeed":7}}}
```
"""

GEOJSON = """```geojson
{"type":"FeatureCollection","features":[
 {"type":"Feature","properties":{"name":"Route"},
  "geometry":{"type":"LineString","coordinates":[[2.35,48.85],[8.54,47.37],[13.40,52.52]]}},
 {"type":"Feature","properties":{"name":"Paris"},
  "geometry":{"type":"Point","coordinates":[2.35,48.85]}},
 {"type":"Feature","properties":{"name":"Berlin"},
  "geometry":{"type":"Point","coordinates":[13.40,52.52]}}
]}
```
"""

TABLE = """```table
{"columns":["Region","Revenue","Units","Last order"],
 "rows":[["North America","$3,400.75",7,"2026-07-22"],
         ["EMEA","$1,200.50",43,"2026-03-01"],
         ["APAC","$980.00",120,"2026-01-15"],
         ["LATAM","$210.10",88,"2026-05-09"]],
 "total":["Revenue","Units"]}
```
"""

FRACTAL = """```fractal
{"type":"lorenz"}
```
"""

FRACTAL_CURVE = """```fractal
{"type":"hilbert","order":5}
```
"""

FRACTAL_MANDELBROT = """```fractal
{"type":"mandelbrot"}
```
"""

FRACTAL_SIERPINSKI = """```fractal
{"type":"sierpinski"}
```
"""

EDITABLE_PLOT = """```plot
y = x^2
```
"""

GRAPH_AND_FORMULA = """The Fourier series of a square wave is built from odd harmonics only:

$$f(x) = \\frac{4}{\\pi}\\sum_{n=1,3,5,\\dots}^{\\infty} \\frac{1}{n}\\sin(nx)$$

Each term contributes progressively less to the shape:

| Harmonic $n$ | Coefficient $4/(n\\pi)$ | Contribution |
|---|---|---|
| 1 | 1.273 | fundamental |
| 3 | 0.424 | 33% |
| 5 | 0.255 | 20% |
| 7 | 0.182 | 14% |

The first two partial sums, drawn against each other:

```plot
f(x) = (4/pi)*sin(x)
g(x) = (4/pi)*(sin(x) + sin(3*x)/3 + sin(5*x)/5)
x = -6.5 .. 6.5
```
"""

NOTATION_AND_SVG = """The opening phrase, in ABC notation:

```abc
X:1
T:Old MacDonald Had a Farm
M:4/4
L:1/4
K:G
G G G D | E E D2 | B B A A | G4 |
```

It opens on a rising fifth, G to D — the string lengths behind that interval
stand in a ratio of 3 to 2, drawn here to scale:

```svg
<svg viewBox="0 0 260 120" xmlns="http://www.w3.org/2000/svg">
  <rect x="20" y="26" width="180" height="14" rx="3" fill="#4f6ef7" fill-opacity="0.25"
        stroke="#4f6ef7" stroke-width="1.5"/>
  <rect x="20" y="66" width="120" height="14" rx="3" fill="#e0245e" fill-opacity="0.25"
        stroke="#e0245e" stroke-width="1.5"/>
  <text x="208" y="38" font-size="12">G — 3 units</text>
  <text x="148" y="78" font-size="12">D — 2 units</text>
  <text x="20" y="104" font-size="12" fill="#666">3 : 2 — a perfect fifth</text>
</svg>
```
"""

CITED_ANSWER = """A symbolic link is a special file type containing a text string that
names another file or directory [1]. It functions as a pointer, and the kernel resolves
the path at the moment of use rather than when the link is created [2].

Unlike a hard link, a symbolic link may span filesystem boundaries and may refer to a
path that does not currently exist — a dangling link is legal, and only fails when it is
followed [1]."""

NO_MATCH_ANSWER = """Nothing in your documents covers this, so the following is from
general knowledge rather than your knowledge base."""


# ── driving the page ─────────────────────────────────────────────────────────

async def reset(page):
    """Back to a clean, empty chat with every overlay closed.

    This clears STATE, not just the visible chat. Selections made for one shot
    survive into the next otherwise, and the result is a frame that looks right
    at a glance and is quietly wrong: the source-scope chip set for
    citations-scope, and the "No RAG" selection made for no-rag, both leaked all
    the way into the two readme header images before this reset existed.
    """
    await page.evaluate("""() => {
      for (const id of ['settings-overlay','pinned-panel','search-overlay',
                        'modal-overlay','img-lightbox','viz-max-overlay'])
        document.getElementById(id)?.classList.remove('open');
      const toasts = document.getElementById('toast-container');
      if (toasts) toasts.innerHTML = '';
      const area = document.getElementById('chat-area');
      if (area) {
        // The app's own clearChat does this first: Chart.js, Leaflet, Plotly,
        // 3Dmol and vis-network instances outlive an innerHTML wipe, and over 40
        // shots the leaked WebGL contexts start getting dropped by the browser.
        if (typeof destroyRichBlocks === 'function') destroyRichBlocks(area);
        area.innerHTML = '';
      }
      const body = document.getElementById('settings-body');
      if (body) body.scrollTop = 0;
      if (typeof setSourceScope === 'function') setSourceScope('');
      const sel = document.querySelector('#rag-select, #topbar select, header select');
      if (sel && window.__shotRagValue != null && sel.value !== window.__shotRagValue) {
        sel.value = window.__shotRagValue;
        sel.dispatchEvent(new Event('change', {bubbles:true}));
      }
      document.getElementById('toast-container').innerHTML = '';
      document.documentElement.setAttribute('data-theme','light');
      window.scrollTo(0,0);
    }""")
    await page.wait_for_timeout(200)
    # Assert the baseline rather than trust it. A leak of this kind produces a
    # frame that looks plausible, so nothing but an explicit check catches it.
    dirty = await page.evaluate("""() => {
      const bad = [];
      const row = document.getElementById('scope-row');
      if (row && row.style.display !== 'none' && row.innerHTML.trim())
        bad.push('source scope still set');
      const sel = document.querySelector('#rag-select, #topbar select, header select');
      if (sel && window.__shotRagValue != null && sel.value !== window.__shotRagValue)
        bad.push(`RAG selection is ${sel.value}, expected ${window.__shotRagValue}`);
      const body = document.getElementById('settings-body');
      if (body && body.scrollTop > 0) bad.push('settings panel still scrolled');
      if (document.getElementById('toast-container').children.length)
        bad.push('a toast is still on screen');
      return bad;
    }""")
    if dirty:
        raise RuntimeError("state leaked from the previous shot: " + "; ".join(dirty))


async def welcome(page):
    """Restore the empty-state hero the app shows with no messages."""
    await reset(page)
    await page.evaluate("""() => {
      const area = document.getElementById('chat-area');
      if (area) area.innerHTML = getWelcomeHTML();
      if (typeof _fillWelcomeVersion === 'function') _fillWelcomeVersion();
    }""")
    await page.wait_for_timeout(600)


async def turns(page, pairs, settle=1800):
    """Render a canned conversation through the app's own pipeline.

    `pairs` is a list of (role, markdown). appendMessage() runs the real
    markdown, math, plot and rich-block passes, so what lands on screen is the
    app rendering, not a mock of it.
    """
    await reset(page)
    await page.evaluate("""(pairs) => {
      const area = document.getElementById('chat-area');
      if (area) area.innerHTML = '';
      for (const [role, text] of pairs) appendMessage(role, text, {});
    }""", pairs)
    await page.wait_for_timeout(settle)


async def one_card(page, markdown, settle=1800):
    """Render a single rich block and return a locator for its card."""
    await turns(page, [["assistant", markdown]], settle=settle)
    return page.locator(".rich-wrap").first


async def settings(page, tab, settle=900):
    await reset(page)
    await page.evaluate(f"openSettings({tab!r})")
    await page.wait_for_timeout(settle)
    # #settings-body keeps its scrollTop across tab switches. Without this reset a
    # shot taken after a scrolled one silently frames the wrong section — which is
    # exactly how settings-rag came back showing Ingest Documents.
    await page.evaluate("() => { const b = document.getElementById('settings-body');"
                        "        if (b) b.scrollTop = 0; }")
    await page.wait_for_timeout(120)


def _card(page):
    return page.locator("#settings-overlay > *").first


async def scroll_panel_to(page, needle):
    """Scroll the settings body so the section whose heading contains `needle`
    sits near the top. Returns True when the section was found.

    Matches on `includes`, not `startsWith`: the panel headings are prefixed with
    an emoji ("\U0001f522 Token Usage"), so anchoring at the start silently
    matched nothing and the shot came back identical to its unscrolled twin.
    """
    return await page.evaluate("""(needle) => {
      const body = document.getElementById('settings-body');
      if (!body) return false;
      const want = needle.toLowerCase();
      const box = [...body.querySelectorAll('h1,h2,h3,h4,h5,.section-title,summary')]
        .find(n => (n.textContent || '').toLowerCase().includes(want));
      if (!box) return false;
      body.scrollTop += box.getBoundingClientRect().top
                      - body.getBoundingClientRect().top - 12;
      return true;
    }""", needle)


async def capture(page, out: pathlib.Path, target=None, pad=0):
    """Write one PNG/JPG. `target` is a locator, a clip dict, or None for the
    whole viewport.

    A clipped card hides the composer for the duration of the shot. The composer
    is fixed to the bottom of the window, so a card taller than the free area had
    it painted straight through the bottom of the frame — chart-data came back
    with its table cut mid-row behind the message box.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    hid = False
    if target is not None and not isinstance(target, dict):
        hid = await page.evaluate("""() => {
          // #input-area is an id, not a class — the earlier selector matched
          // nothing and the composer stayed in every clipped card.
          const bar = document.getElementById('input-area')
                   || document.getElementById('msg-input')?.closest('form, footer');
          if (!bar) return false;
          bar.dataset.shotHidden = '1';
          // display, not visibility: taking it out of the flow lets the chat area
          // reflow, so a tall card gets the space back instead of being clipped
          // against a blank band where the composer used to sit.
          bar.style.display = 'none';
          return true;
        }""")
    kw = {"path": str(out)}
    if out.suffix.lower() in (".jpg", ".jpeg"):
        kw["type"], kw["quality"] = "jpeg", 92
    if target is None:
        await page.screenshot(**kw)
    elif isinstance(target, dict):
        await page.screenshot(clip=target, **kw)
    else:
        box = await target.bounding_box()
        if not box:
            raise RuntimeError(f"{out.name}: target not visible")
        await page.screenshot(clip={
            "x": max(0, box["x"] - pad), "y": max(0, box["y"] - pad),
            "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad}, **kw)
    if hid:
        await page.evaluate("""() => {
          const bar = document.querySelector('[data-shot-hidden]');
          if (bar) { bar.style.display = ''; delete bar.dataset.shotHidden; }
        }""")


# ── the shots ────────────────────────────────────────────────────────────────
# Each entry is (relative path, async setup(page) -> target). The target is a
# locator, a clip dict or None for the whole viewport.

async def _welcome(page):
    await welcome(page)
    return None


async def _first_run(page):
    """The empty state as it looks before any model is reachable."""
    await welcome(page)
    await page.evaluate("""() => {
      const setup = document.getElementById('welcome-setup');
      if (!setup) throw new Error('welcome-setup missing');
      setup.hidden = false;
      setup.style.display = '';
      const chips = document.getElementById('welcome-chips');
      if (chips) chips.style.display = 'none';
    }""")
    await page.wait_for_timeout(300)
    return None


def _settings_shot(tab, *, full=False, scroll_to=None, settle=1400):
    async def run(page):
        await settings(page, tab, settle=settle)
        if scroll_to:
            if not await scroll_panel_to(page, scroll_to):
                raise RuntimeError(f"section {scroll_to!r} not found in the {tab} panel")
            await page.wait_for_timeout(400)
        return None if full else _card(page)
    return run


def _lane_shot(markdown, *, settle=1800, viewport=None):
    async def run(page):
        return await one_card(page, markdown, settle=settle)
    if viewport:
        run.viewport = viewport
    return run


async def _chart_data(page):
    await one_card(page, CHART_DATA, settle=1800)
    await page.locator("button:has-text('Data')").first.click()
    await page.wait_for_timeout(700)
    return page.locator(".rich-wrap").first


async def _timeline(page):
    """Captured maximized.

    mermaid lays a timeline out at a fixed width per milestone — 895px for two,
    whatever the labels say — and the card is 558px however wide the window is,
    so in the card the second milestone is always cut through its label. Maximize
    is the app's own answer to content wider than the column, and at 1203px the
    diagram fits whole.
    """
    await one_card(page, TIMELINE, settle=2400)
    await page.locator(".rich-wrap button:has-text('Maximize')").first.click()
    await page.wait_for_selector("#viz-max-overlay.open", timeout=6000)
    await page.wait_for_timeout(1200)
    fits = await page.evaluate("""() => {
      const ov = document.getElementById('viz-max-overlay');
      const svg = ov.querySelector('svg'), card = ov.firstElementChild;
      if (!svg || !card) return false;
      return svg.getBoundingClientRect().width <= card.getBoundingClientRect().width + 1;
    }""")
    if not fits:
        raise RuntimeError("the timeline still overflows even maximized")
    return page.locator("#viz-max-overlay > *").first


async def _table_sort(page):
    """Captured mid-sort. DOCS.md embeds this directly under "click any header to
    sort", so a shot of the table in source order does not show the feature."""
    await turns(page, [["assistant", TABLE]], settle=1400)
    before = await page.evaluate(
        "() => [...document.querySelectorAll('.dtable tbody tr td:first-child')].map(c=>c.textContent)")
    await page.evaluate("""() => {
      const th = [...document.querySelectorAll('.dtable thead th')]
        .find(n => /revenue/i.test(n.textContent));
      if (!th) throw new Error('no Revenue column');
      // ONE click, ascending. The rows are already in descending revenue order,
      // so sorting descending is a no-op and shows nothing. Ascending is also the
      // better demonstration: by value $210.10 leads, where a plain string sort
      // would put $1,200.50 first.
      th.click();
    }""")
    await page.wait_for_timeout(500)
    after = await page.evaluate(
        "() => [...document.querySelectorAll('.dtable tbody tr td:first-child')].map(c=>c.textContent)")
    if before == after:
        raise RuntimeError(f"clicking Revenue did not reorder the rows ({before})")
    return page.locator(".rich-wrap, .dtable-wrap").first


CHUNKS = [
    {"n": 1, "score": 0.83, "instance": "Ubuntu", "source": "man/symlink.7",
     "text": "A symbolic link is a special file that contains a text string "
             "naming another file or directory. It is resolved at the moment of "
             "use, not when the link is created."},
    {"n": 2, "score": 0.79, "instance": "Ubuntu", "source": "man/ln.1",
     "text": "ln -s creates a symbolic link. Unlike a hard link it may span "
             "filesystem boundaries and may name a path that does not exist."},
    {"n": 3, "score": 0.62, "instance": "Ubuntu", "source": "man/path_resolution.7",
     "text": "A dangling link is legal; the error surfaces only when it is "
             "followed."},
]


async def _with_inspector(page, question, answer, *, scope=None, open_panel=True):
    """Render a turn and attach the retrieval inspector the app builds for a
    real answer, from canned chunks."""
    await turns(page, [["user", question], ["assistant", answer]], settle=800)
    await page.evaluate("""({chunks, openPanel, scope}) => {
      const msg = document.querySelector('.msg-wrap:not(.user)');
      if (!msg) throw new Error('no assistant message');
      const id = msg.id;
      // buildRagInspector returns an element, not a markup string.
      const built = buildRagInspector(chunks, {rag: 0.041, llm: 1.284}, openPanel, '', id);
      const node = (built instanceof Node) ? built
        : Object.assign(document.createElement('div'), {innerHTML: built}).firstElementChild;
      const body = msg.querySelector('.msg-body');
      body.insertBefore(node, body.querySelector('.msg-meta'));
      // The app's own scope row, set through its own entry point, so the chip is
      // the real control rather than a look-alike.
      if (scope) setSourceScope(scope);
    }""", {"chunks": CHUNKS, "openPanel": open_panel, "scope": scope})
    await page.wait_for_timeout(700)


async def _citations_scope(page):
    await _with_inspector(page,
        "In two sentences: what is a symbolic link?", CITED_ANSWER,
        scope="man/symlink.7")
    # setSourceScope raises a toast; it would sit on top of the frame.
    await page.evaluate("() => document.getElementById('toast-container').innerHTML = ''")
    await page.wait_for_timeout(200)
    return None


async def _citations_click(page):
    await _with_inspector(page,
        "Can a Redis stream entry be edited after it is written?",
        "Redis streams are append-only, so an entry can never be edited once "
        "written [1]. Keys, by contrast, are removed as soon as their TTL "
        "elapses [2].")
    await page.evaluate("""() => {
      const two = document.querySelector('.rag-chunk[data-cite-n="2"]');
      if (!two) throw new Error('chunk 2 not rendered');
      const btn = two.querySelector('.rag-chunk-expand');
      if (!btn) throw new Error('chunk 2 has no expander');
      btn.click();
    }""")
    await page.wait_for_timeout(600)
    expanded = await page.evaluate(
        "() => !!document.querySelector('.rag-chunk[data-cite-n=\"2\"] .rag-chunk-content.open')")
    if not expanded:
        raise RuntimeError("chunk 2 did not expand")
    return page.locator(".rag-inspector").first


async def _no_kb_match(page):
    await turns(page, [["assistant", NO_MATCH_ANSWER]], settle=500)
    await page.evaluate("""() => {
      const b = document.querySelector('.msg-bubble.ai');
      if (b && !b.querySelector('.no-kb-pill'))
        b.insertAdjacentHTML('beforeend',
          '<div class="no-kb-pill" style="margin-top:8px;display:inline-block;'
          + 'font-size:11px;padding:3px 8px;border-radius:999px;'
          + 'background:var(--warn-bg,#fff3cd);color:var(--warn-fg,#8a6d1f)">'
          + '\\u26a0 No KB match</div>');
    }""")
    await page.wait_for_timeout(200)
    return page.locator(".msg-bubble.ai").first


async def _query_actions(page):
    await turns(page, [["user", "How do Redis streams differ from pub/sub?"]], settle=500)
    await page.locator(".msg-wrap.user").first.hover()
    await page.wait_for_timeout(400)
    return page.locator(".msg-wrap.user").first


async def _search(page):
    """Five matches across three conversations, one of them inside a retrieved
    source. _runSearch reads S.sessions in memory, so the conversations are put
    there directly — nothing is written to the backend."""
    await reset(page)
    await page.evaluate("""() => {
      const mk = (title, msgs) => ({title, messages: msgs});
      S.sessions = {
        s1: mk('Streams vs pub/sub', [
          {role:'user', content:'how does recall work with streams?'},
          {role:'assistant', content:'Recall is driven by vector search over the indexed chunks.',
           meta:{chunks:[{source:'tuning.md',
                          text:'The recall knob is the similarity threshold in Settings.'}]}}]),
        s2: mk('Tuning retrieval', [
          {role:'user', content:'Lower the threshold to widen recall at the cost of precision.'}]),
        s3: mk('Cache behaviour', [
          {role:'user', content:'does the cache affect recall?'}]),
      };
      S.sessionId = 's1';
      if (typeof renderSessionList === 'function') renderSessionList();
    }""")
    await page.evaluate("openSearch()")
    await page.evaluate("""() => {
      const all = document.getElementById('search-all-sessions');
      const ch  = document.getElementById('search-chunks');
      if (all) all.checked = true;
      if (ch) ch.checked = true;
    }""")
    await page.fill("#search-input", "recall")
    await page.wait_for_timeout(1200)
    hits = await page.evaluate(
        "() => (document.getElementById('search-count')||{}).textContent || ''")
    if "match" not in hits:
        raise RuntimeError(f"search found nothing (count read {hits!r})")
    return page.locator("#search-overlay > *").first


async def _toasts(page):
    await reset(page)
    await page.evaluate("""() => {
      toast('Crawl failed: connection reset while fetching https://docs.example/api/v2','error');
      toast('Ingested 4 files','success');
      toast('Scheduled https://redis.io/llms.txt','info');
    }""")
    await page.wait_for_timeout(700)
    return page.locator("#toast-container")


async def _notifications_log(page):
    await reset(page)
    # _toastLog is module-scoped, so it is filled the only way the app fills it.
    await page.evaluate("""async () => {
      const wait = ms => new Promise(r => setTimeout(r, ms));
      for (const [msg, type] of [
        ['Crawl failed: connection reset while fetching https://docs.example/api/v2','error'],
        ['Ingested 6 files (2 errors)','warning'],
        ['Saved to "saved-answers" \\u2014 3 chunks, retrievable from now on','success'],
        ['Scheduled https://redis.io/llms.txt','info'],
        ['Settings NOT saved: embedding model change requires a rebuild','error'],
      ]) { toast(msg, type); await wait(60); }
      document.getElementById('toast-container').innerHTML = '';
    }""")
    await page.evaluate("openSettings('logs')")
    await page.wait_for_timeout(900)
    await page.evaluate("renderToastLog()")
    await page.wait_for_timeout(300)
    return page.locator("#toast-log")


async def _ingest_progress(page):
    await settings(page, "rag", settle=1200)
    await page.evaluate("""() => {
      const p = document.getElementById('ingest-progress');
      p.style.display = 'block';
      document.getElementById('ingest-bar').style.width = '62%';
      document.getElementById('ingest-status').textContent =
        '\\u2713 handbook.pdf \\u2014 62 chunks (3/5)';
      document.getElementById('ingest-cancel-btn').style.display = '';
      p.scrollIntoView({block:'center'});
    }""")
    await page.wait_for_timeout(400)
    return page.locator("#ingest-progress")


async def _crawl_progress(page):
    await settings(page, "websources", settle=1200)
    await page.evaluate("""() => {
      const c = document.getElementById('crawl-progress-card');
      c.style.display = 'block';
      const n = (id,v) => document.getElementById(id).textContent = v;
      n('crawl-count-indexed','18'); n('crawl-count-skipped','4');
      n('crawl-count-blocked','1');  n('crawl-count-errors','0');
      n('crawl-count-chunks','212'); n('crawl-count-rate','1.4');
      document.getElementById('crawl-rate-stat').style.display = '';
      document.getElementById('crawl-bar').style.width = '48%';
      document.getElementById('crawl-progress-note').textContent =
        '23 of 47 pages found so far \\u00b7 18 indexed \\u00b7 29 queued ' +
        '\\u2014 the total grows as new links are discovered';
      document.getElementById('crawl-log').innerHTML = [
        ['redis.io/docs/latest/develop/data-types/streams/','14 chunks'],
        ['redis.io/docs/latest/develop/data-types/sets/','14 chunks'],
        ['redis.io/docs/latest/commands/xadd/','14 chunks'],
      ].map(([u,c]) => `<div style="display:flex;justify-content:space-between;
        gap:12px;padding:3px 0"><span><span class="status-pill pill-ok"
        style="margin-right:8px">indexed</span>${u}</span>
        <span style="color:var(--text2)">${c}</span></div>`).join('');
      c.scrollIntoView({block:'start'});
    }""")
    await page.wait_for_timeout(400)
    return page.locator("#crawl-progress-card")


# The document manager lists whatever is in the operator's own Redis, which makes
# it the one shot that is not reproducible and that leaks a private corpus into a
# public image. The API is answered from here instead, so the app renders its own
# markup from fixed rows.
_DOC_ROWS = [
    ("https://redis.io/docs/latest/develop/data-types/streams/", 56),
    ("https://redis.io/docs/latest/develop/data-types/sets/", 19),
    ("https://redis.io/docs/latest/develop/get-started/vector-database/", 18),
    ("https://redis.io/docs/latest/commands/xadd/", 14),
    ("https://redis.io/docs/latest/commands/ft.search/", 10),
    ("https://redis.io/docs/latest/operate/oss_and_stack/", 6),
    ("handbook.pdf", 62),
    ("release-notes.md", 2),
]


async def _documents(page):
    await reset(page)
    ingested = 1_753_000_000
    payload = {"total": len(_DOC_ROWS),
               "documents": [{"source": src, "chunks": n,
                              "ingested_at": ingested - 86400 * i}
                             for i, (src, n) in enumerate(_DOC_ROWS)]}

    async def canned(route):
        await route.fulfill(status=200, content_type="application/json",
                            body=json.dumps(payload))

    await page.route("**/api/rag/*/documents*", canned)
    try:
        await page.evaluate("openDocuments(0)")
        await page.wait_for_selector(".doc-row", timeout=8000)
        await page.wait_for_timeout(400)
        return page.locator("#modal-overlay > *").first
    finally:
        await page.unroute("**/api/rag/*/documents*", canned)


async def _unsaved(page):
    """The discard prompt. Reached by staging a change and cancelling — nothing
    here saves, and a staged change is discarded with the dialog."""
    await settings(page, "rag", settle=1200)
    await page.evaluate("""() => {
      const s = document.querySelector('#tab-rag input[type=range]');
      if (s) { s.value = String(Number(s.value) + 10); s.dispatchEvent(new Event('input',{bubbles:true})); }
      const d = document.getElementById('settings-dirty');
      if (d) d.hidden = false;
    }""")
    await page.wait_for_timeout(300)
    await page.evaluate("closeSettings()")
    await page.wait_for_timeout(700)
    return page.locator("#modal-overlay > *").first


async def _keep_answer(page):
    await turns(page, [["user", "How does the semantic cache decide a question is the same one?"],
                       ["assistant", CITED_ANSWER]], settle=700)
    await page.evaluate("""() => {
      const b = [...document.querySelectorAll('.msg-action-btn')]
        .find(n => /Keep this answer/i.test(n.getAttribute('title') || ''));
      if (!b) throw new Error('keep-answer button not found');
      b.click();
    }""")
    await page.wait_for_selector("#modal-overlay.open", timeout=6000)
    await page.wait_for_timeout(1200)
    return page.locator("#modal-overlay > *").first


async def _no_rag(page):
    await reset(page)
    await page.evaluate("""() => {
      const sel = document.querySelector('#rag-select, #topbar select, .topbar select, header select');
      if (!sel) throw new Error('no RAG selector on the top bar');
      const o = [...sel.options].find(o => /no rag/i.test(o.textContent));
      if (!o) throw new Error('the selector has no "No RAG" option');
      sel.value = o.value;
      sel.dispatchEvent(new Event('change', {bubbles:true}));
    }""")
    await page.wait_for_timeout(600)
    picked = await page.evaluate(
        """() => { const s = document.querySelector('#rag-select, #topbar select, header select');
                  return s ? s.options[s.selectedIndex].textContent : ''; }""")
    if "no rag" not in picked.lower():
        raise RuntimeError(f"selector shows {picked!r}, not No RAG")
    return {"x": 0, "y": 0, "width": VIEWPORT["width"], "height": 52}


def _full_answer(markdown_pairs):
    """A whole-window shot of one answer.

    Taller than the standard viewport: these two illustrate several blocks at
    once, and at 800px the frame opened mid-table with the formula its caption
    names already scrolled off the top.
    """
    async def run(page):
        await turns(page, markdown_pairs, settle=2600)
        await page.evaluate("""() => {
          const area = document.getElementById('chat-area');
          if (area) area.scrollTop = 0;
        }""")
        await page.wait_for_timeout(300)
        return None
    run.viewport = {"width": 1280, "height": 1180}
    return run


async def _fractal_zoom(page):
    """A box-zoom in progress on a Mandelbrot card: the ratio-locked selection
    rectangle over the set, plus the Iterations slider underneath."""
    card = await one_card(page, FRACTAL_MANDELBROT, settle=2400)
    await page.evaluate("""() => {
      const wrap = document.querySelector('.rich-wrap[data-kind="fractal"]');
      const canvas = wrap.querySelector('canvas'), r = canvas.getBoundingClientRect();
      const fire = (t, fx, fy) => canvas.dispatchEvent(new PointerEvent(t,
        {clientX: r.left + r.width * fx, clientY: r.top + r.height * fy,
         button: 0, bubbles: true, pointerId: 1}));
      fire('pointerdown', 0.30, 0.30); fire('pointermove', 0.60, 0.52);   // leave the box mid-drag
    }""")
    await page.wait_for_timeout(200)
    return card


async def _fractal_sierpinski(page):
    """The Sierpinski gasket as nested triangle outlines, its Depth slider at 6."""
    card = await one_card(page, FRACTAL_SIERPINSKI, settle=2000)
    await page.evaluate("""async () => {
      const inp = document.querySelector('.frac-ctl input');
      inp.value = 6;
      inp.dispatchEvent(new Event('input', {bubbles: true}));
      inp.dispatchEvent(new Event('change', {bubbles: true}));
      await new Promise(r => setTimeout(r, 700));
    }""")
    await page.wait_for_timeout(200)
    return card


async def _editable_source(page):
    """A plot card whose Source pane has been edited (a parameter and a second
    function added) and re-rendered with Apply — the editable-source feature."""
    await turns(page, [["assistant", EDITABLE_PLOT]], settle=1800)
    await page.evaluate("""async () => {
      const wrap = document.querySelector('.plot-render-wrap');
      wrap.querySelector('[data-act="plot-src"]').click();
      const code = wrap.querySelector('.plot-src-pre code');
      code.textContent = 'param: a = 1 .. 5 (3)\ny = a*sin(x)\ny = cos(x)';
      wrap.querySelector('[data-act="rich-apply"]').click();
      await new Promise(r => setTimeout(r, 1700));   // let '\u2713 Applied' revert to '\u25b6 Apply'
    }""")
    await page.wait_for_timeout(200)
    return page.locator(".plot-render-wrap").first


SHOTS: list[tuple[str, object]] = [
    # ── tutorial walkthrough ─────────────────────────────────────────────────
    ("tutorial/03-welcome.png",            _welcome),
    ("tutorial/04-status.png",             _settings_shot("status", full=True, settle=2200)),
    ("tutorial/05-providers.png",          _settings_shot("providers", settle=1800)),
    ("tutorial/07-rag.png",                _settings_shot("rag", settle=1800)),
    ("tutorial/08-web-sources.png",        _settings_shot("websources", full=True)),
    ("tutorial/08b-recrawl-schedule.png",  _settings_shot("websources", full=True,
                                                          scroll_to="Scheduled Re-crawl")),
    ("tutorial/08c-crawl-progress.png",    _crawl_progress),
    ("tutorial/10c-keep-answer.png",       _keep_answer),
    ("tutorial/11-cache.png",              _settings_shot("cache", full=True, settle=2200)),
    ("tutorial/13-analytics.png",          _settings_shot("analytics", full=True, settle=2600)),
    ("tutorial/13b-token-usage.png",       _settings_shot("analytics", full=True, settle=2600,
                                                          scroll_to="Token Usage")),
    # ── feature close-ups ────────────────────────────────────────────────────
    ("features/first-run.png",             _first_run),
    ("features/no-rag.png",                _no_rag),
    ("features/query-actions.png",         _query_actions),
    ("features/citations-click.png",       _citations_click),
    ("features/search.png",                _search),
    ("features/notifications-toasts.png",  _toasts),
    ("features/notifications-log.png",     _notifications_log),
    ("features/ingest-progress.png",       _ingest_progress),
    ("features/settings-rag.png",          _settings_shot("rag", settle=1800,
                                                          scroll_to="RAG Configuration")),
    ("features/settings-visual.png",       _settings_shot("templates", settle=1800,
                                                          scroll_to="Base Instruction")),
    ("features/unsaved-settings.png",      _unsaved),
    # ── rendering lanes ──────────────────────────────────────────────────────
    ("rendering/mermaid.png",              _lane_shot(MERMAID)),
    ("rendering/chart.png",                _lane_shot(CHART)),
    ("rendering/chart-data.png",           _chart_data),
    ("rendering/dot.png",                  _lane_shot(DOT)),
    ("rendering/geometry.png",             _lane_shot(GEOMETRY, settle=2400)),
    ("rendering/fractal.png",              _lane_shot(FRACTAL, settle=2400)),
    ("rendering/fractal-curve.png",       _lane_shot(FRACTAL_CURVE, settle=2400)),
    ("rendering/fractal-zoom.png",         _fractal_zoom),
    ("rendering/fractal-sierpinski.png",   _fractal_sierpinski),
    ("rendering/map.png",                  _lane_shot(MAP, settle=3000)),
    ("rendering/plot3d.png",               _lane_shot(PLOT3D, settle=2800)),
    ("rendering/molecule.png",             _lane_shot(MOLECULE, settle=2200)),
    ("rendering/molecule3d.png",           _lane_shot(MOLECULE3D, settle=2800)),
    ("rendering/gantt.png",                _lane_shot(GANTT, settle=2200)),
    ("rendering/timeline.png",             _timeline),
    ("rendering/network.png",              _lane_shot(NETWORK, settle=2800)),
    ("rendering/geojson.png",              _lane_shot(GEOJSON, settle=3000)),
    ("rendering/table-sort.png",           _table_sort),
    ("rendering/editable-source.png",      _editable_source),
    ("rendering/citations-scope.png",      _citations_scope),
    ("rendering/documents.png",            _documents),
    ("rendering/no-kb-match.png",          _no_kb_match),
    # ── wide feature shots on the readme ─────────────────────────────────────
    ("graph-and-formula.jpg",              _full_answer([
        ["user", "Explain the Fourier series of a square wave."],
        ["assistant", GRAPH_AND_FORMULA]])),
    ("notation-and-svg.jpg",               _full_answer([
        ["user", "Write out the opening of Old MacDonald and show the interval."],
        ["assistant", NOTATION_AND_SVG]])),
]


# ── runner ───────────────────────────────────────────────────────────────────

async def run(url: str, out_dir: pathlib.Path, only: list[str]) -> int:
    from playwright.async_api import async_playwright

    picked = [(n, f) for n, f in SHOTS
              if not only or any(k.lower() in n.lower() for k in only)]
    if not picked:
        print(f"no shot matches {only}", file=sys.stderr)
        return 2

    failures: list[tuple[str, str]] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page(viewport=VIEWPORT, device_scale_factor=SCALE)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(url, wait_until="networkidle")
        await page.wait_for_timeout(2500)
        await page.evaluate("() => document.documentElement.setAttribute('data-theme','light')")
        # The selection the app came up with, so reset() can put it back after the
        # no-rag shot changes it.
        await page.evaluate("""() => {
          const sel = document.querySelector('#rag-select, #topbar select, header select');
          window.__shotRagValue = sel ? sel.value : null;
        }""")

        for name, setup in picked:
            errors.clear()
            want = getattr(setup, "viewport", None)
            try:
                if want:
                    await page.set_viewport_size(want)
                    await page.wait_for_timeout(400)
                target = await setup(page)
                # The whole point of this exercise was renders that silently
                # failed. .rich-wrap matches a card whether it drew or shows a red
                # box, so an unchecked shot would have filed the error as `ok`.
                broken = await page.evaluate("""() => [...document.querySelectorAll('.rich-err')]
                    .map(n => (n.textContent || '').trim().slice(0, 120))""")
                if broken:
                    raise RuntimeError("lane error on the page: " + " | ".join(broken))
                await capture(page, out_dir / name, target)
                size = (out_dir / name).stat().st_size
                if errors:
                    raise RuntimeError("page errors: " + " | ".join(errors[:3]))
                print(f"  ok   {name:<38} {size/1024:7.1f} KB")
            except Exception as exc:                       # noqa: BLE001 — reported, not raised
                failures.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"  FAIL {name:<38} {type(exc).__name__}: {exc}")
            finally:
                if want:
                    await page.set_viewport_size(VIEWPORT)
                    await page.wait_for_timeout(300)
        await browser.close()

    if failures:
        print(f"\n{len(failures)} of {len(picked)} failed:")
        for n, e in failures:
            print(f"  {n}: {e}")
    else:
        print(f"\nall {len(picked)} captured")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("only", nargs="*", help="substrings; capture only matching shots")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--out", default=str(DEFAULT_OUT), type=pathlib.Path)
    ap.add_argument("--list", action="store_true", help="list shot names and exit")
    a = ap.parse_args()
    if a.list:
        for n, _ in SHOTS:
            print(n)
        return 0
    print(f"{len(SHOTS)} shots -> {a.out}  (from {a.url})")
    return asyncio.run(run(a.url, pathlib.Path(a.out), a.only))


if __name__ == "__main__":
    raise SystemExit(main())
