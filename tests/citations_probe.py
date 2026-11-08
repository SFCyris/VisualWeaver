# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citation source cards, grounding badges and the Documents only toggle in a real browser
with canned API answers (ux_fixes_probe's fake fetch and WebSocket, no server). Prints one
JSON object of measured values: computed style, bounding boxes, focus, sent frames.

    python citations_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys
import time

import ux_fixes_probe as ux

OUT: dict = {}
NOW = int(time.time())

NOT_FOUND = ("Not found in your documents. Nothing in the knowledge base matched this question, "
             "so there is no answer to give from your documents.")

CHUNKS = [
    {"n": 1, "source": "https://docs.example.org/streams/intro.html", "score": 0.81, "relevance": 0.81,
     "reranker_score": 0.9312, "instance": "default",
     "text": 'Streams are append-only. <img src=x onerror="window.__xss=1"> <b>bold</b> and more text. '
             + 'Each entry has an ID made of a millisecond time and a sequence number. ' * 6},
    {"n": 2, "source": "/data/notes/consumer-groups.md", "score": 0.62, "relevance": 0.62, "instance": "default",
     "text": "Consumer groups split a stream between clients."},
    {"n": 3, "source": "manual.pdf", "score": 0, "relevance": 0.4, "lexical": True, "instance": "default",
     "text": "XREADGROUP reads on behalf of a group."},
]

PARTLY = ("Redis streams are append-only logs that keep every entry in order [1].\n\n"
          "Consumer groups let several clients share the work of reading a stream [2, 3].\n\n"
          "Entries can be trimmed by length, which the documents call capping the stream [7].")
GROUNDED = "Yes, streams keep their entries in insertion order [1]."


def _msgs():
    a = lambda content, **meta: {"role": "assistant", "content": content,
                                 "meta": {"ts": NOW - 50, "provider": "ollama", "model": "llama3:latest", **meta}}
    u = lambda content: {"role": "user", "content": content, "meta": {"ts": NOW - 60}}
    return [u("tell me about streams"), a(PARTLY, chunks=CHUNKS, rag_used=True, docs_only=False, kbs=["default"]),
            u("ordered?"), a(GROUNDED, chunks=CHUNKS, rag_used=True, docs_only=False),
            u("what is 2+2"), a("Four. It is a basic sum that needs no documents at all.", rag_used=False, docs_only=False),
            u("uncited"), a("This answer does not cite anything from the documents at all.", chunks=CHUNKS, rag_used=True),
            u("warranty?"), a(NOT_FOUND, rag_used=True, docs_only=True, not_found=True, kbs=["default"])]


def _page(r, width=1400, height=900, touch=False, docs_default=False):
    cfg = json.loads(json.dumps(ux.CONFIG))
    cfg["rag"]["documents_only"] = docs_default
    sessions = [{"id": "s_cite", "messages": 10, "preview": "", "title": "Streams", "updated": NOW - 50,
                 "created": NOW - 60, "pinned": False, "expires": None}]
    v = ux._variant(config=cfg, sessions=sessions, mine=["s_cite"], sessionMsgs={"s_cite": _msgs()})
    kw = {"viewport": {"width": width, "height": height}}
    if touch:
        kw.update(has_touch=True, is_mobile=r.engine != "firefox")
    ctx = r.browser.new_context(**kw)
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(v) + ";")
    pg.add_init_script(ux.FAKE)
    pg.goto(ux.INDEX.as_uri(), wait_until="load")
    ux._wait(pg, "() => typeof S === 'object' && S.modelsLoaded && document.querySelectorAll('#sessions-list .session-row').length >= 1", 10)
    pg.wait_for_timeout(200)
    return pg


def _open_conversation(pg, touch=False):
    if touch:
        pg.evaluate("openSession('s_cite')")        # the phone sidebar is an overlay; opening is not under test
    else:
        pg.locator("#sessions-list [data-sid='s_cite'] .session-title").click()
    ux._wait(pg, "() => document.querySelectorAll('#chat-area .msg-wrap:not(.user)').length >= 5", 5)
    pg.wait_for_timeout(200)


CARD = r"""() => { const c = document.getElementById('cite-card'); if (!c) return {exists: false};
  const r = c.getBoundingClientRect(), cs = getComputedStyle(c);
  const cx = r.left + r.width / 2, cy = r.top + Math.min(r.height / 2, 20);
  const hit = document.elementFromPoint(cx, cy);
  return {exists: true, hidden: c.hidden, display: cs.display, position: cs.position,
          x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
          right: Math.round(r.right), bottom: Math.round(r.bottom),
          vw: document.documentElement.clientWidth, vh: window.innerHeight,
          onTop: !!(hit && c.contains(hit)), hOverflow: c.scrollWidth > c.clientWidth + 1,
          parent: c.parentElement && c.parentElement.tagName,
          title: c.querySelector('.cite-card-title')?.textContent, n: c.querySelector('.cite-card-n')?.textContent,
          src: c.querySelector('.cite-card-src')?.textContent, score: c.querySelector('.cite-card-score')?.textContent || null,
          text: c.querySelector('.cite-card-text')?.textContent, markup: !!c.querySelector('img,b,script'),
          link: c.querySelector('.cite-card-src a')?.getAttribute('href') || null,
          xss: window.__xss === 1}; }"""

REF = r"""(i) => { const b = document.querySelectorAll('#chat-area .cite-ref')[i]; const r = b.getBoundingClientRect();
  return {x: r.left + r.width / 2, y: r.top + r.height / 2, top: Math.round(r.top), bottom: Math.round(r.bottom),
          left: Math.round(r.left), text: b.textContent, msg: b.dataset.citeMsg,
          describedby: b.getAttribute('aria-describedby'), expanded: b.getAttribute('aria-expanded')}; }"""


# The toggle and what it shares the composer with: on a phone it must not squeeze the
# message box (its placeholder wrapped when the toggle sat inside it).
TOGGLE = r"""() => { const box = id => { const r = document.getElementById(id).getBoundingClientRect();
    return {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
            right: Math.round(r.right), bottom: Math.round(r.bottom)}; };
  const t = document.getElementById('msg-input');
  return {btn: box('docs-only-btn'), tpl: box('template-select'), wrap: box('input-wrap'), send: box('send-btn'),
          display: getComputedStyle(document.getElementById('docs-only-btn')).display,
          vw: document.documentElement.clientWidth, inputW: Math.round(t.getBoundingClientRect().width),
          placeholderWraps: t.scrollHeight > t.clientHeight + 2}; }"""


def badges_and_broken(r):
    pg = _page(r)
    _open_conversation(pg)
    OUT["badges"] = pg.evaluate("""() => [...document.querySelectorAll('#chat-area .msg-wrap:not(.user)')].map(w => {
        const b = w.querySelector('.msg-meta .ground-badge'); if (!b) return null;
        const r = b.getBoundingClientRect(), cs = getComputedStyle(b);
        return {cls: b.className, text: b.textContent, title: b.title, display: cs.display, color: cs.color,
                w: Math.round(r.width), h: Math.round(r.height)}; })""")
    OUT["broken"] = pg.evaluate("""() => [...document.querySelectorAll('#chat-area .cite-broken')].map(x => {
        const r = x.getBoundingClientRect(), cs = getComputedStyle(x);
        return {tag: x.tagName, text: x.firstChild.textContent, title: x.title, deco: cs.textDecorationLine,
                border: cs.borderTopStyle, color: cs.color, w: Math.round(r.width), h: Math.round(r.height),
                isButton: x.tagName === 'BUTTON' || !!x.closest('.cite-ref')}; })""")
    OUT["refs"] = pg.evaluate("[...document.querySelectorAll('#chat-area .cite-ref')].map(b => b.textContent)")
    OUT["d_toggle"] = pg.evaluate(TOGGLE)
    OUT["toggle_restored"] = pg.evaluate("""() => { const b = document.getElementById('docs-only-btn'), r = b.getBoundingClientRect();
        const chip = document.querySelector('#scope-row .docs-only-chip');
        return {pressed: b.getAttribute('aria-pressed'), w: Math.round(r.width), h: Math.round(r.height),
                chip: chip ? chip.innerText.trim() : null,
                chipDisplay: chip ? getComputedStyle(document.getElementById('scope-row')).display : null}; }""")
    r.shot(pg, "citations_badges")
    pg.context.close()


def hover_card(r):
    pg = _page(r)
    _open_conversation(pg)
    pg.evaluate("document.getElementById('chat-area').scrollTop = 0")
    pg.wait_for_timeout(100)
    ref = pg.evaluate(REF, 0)
    pg.mouse.move(ref["x"], ref["y"])
    pg.wait_for_timeout(400)
    OUT["hover_card"] = pg.evaluate(CARD)
    OUT["hover_ref"] = pg.evaluate(REF, 0)
    r.shot(pg, "citations_hover")
    # into the card: it stays open (a link in it can be reached)
    c = OUT["hover_card"]
    if c.get("display") == "block":
        pg.mouse.move(ref["x"], ref["y"] + 8)
        pg.mouse.move(c["x"] + 20, c["y"] + 12)
        pg.wait_for_timeout(400)
        OUT["hover_into_card"] = pg.evaluate(CARD)["display"]
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(450)
    OUT["hover_left"] = pg.evaluate(CARD)["display"]
    # a citation near the bottom of a short window: the card opens above it, inside the window
    pg.set_viewport_size({"width": 1400, "height": 600})
    pg.wait_for_timeout(200)
    pg.evaluate("""() => { const a = document.getElementById('chat-area'); const b = document.querySelectorAll('#chat-area .cite-ref')[3];
        a.scrollTop += b.getBoundingClientRect().bottom - (a.getBoundingClientRect().bottom - 8); }""")
    pg.wait_for_timeout(150)
    low = pg.evaluate(REF, 3)
    pg.mouse.move(low["x"], low["y"])
    pg.wait_for_timeout(400)
    OUT["low_ref"] = low
    OUT["low_card"] = pg.evaluate(CARD)
    pg.set_viewport_size({"width": 1400, "height": 900})
    pg.wait_for_timeout(200)
    # a click jumps to the source in the list, as before, and closes the card
    pg.evaluate("document.getElementById('chat-area').scrollTop = 0")
    pg.wait_for_timeout(100)
    two = pg.evaluate(REF, 1)
    pg.mouse.move(two["x"], two["y"])
    pg.wait_for_timeout(300)
    pg.mouse.click(two["x"], two["y"])
    pg.wait_for_timeout(500)
    OUT["click_jump"] = pg.evaluate("""(msg) => { const t = document.getElementById('cite-' + msg + '-2');
        return {inspectorOpen: !!t && t.closest('.rag-inspector').classList.contains('open'),
                contentOpen: !!t && t.querySelector('.rag-chunk-content').classList.contains('open'),
                card: getComputedStyle(document.getElementById('cite-card')).display}; }""", two["msg"])
    pg.context.close()


def keyboard(r):
    pg = _page(r)
    _open_conversation(pg)
    pg.evaluate("document.getElementById('chat-area').scrollTop = 0")
    # Real Tab presses from the top of the conversation until a citation has focus.
    pg.evaluate("document.querySelector('#chat-area .msg-wrap:not(.user) .msg-bubble').setAttribute('tabindex','-1');"
                "document.querySelector('#chat-area .msg-wrap:not(.user) .msg-bubble').focus()")
    for _ in range(40):
        pg.keyboard.press("Tab")
        if pg.evaluate("document.activeElement.classList.contains('cite-ref')"):
            break
    pg.wait_for_timeout(150)
    OUT["kb_focus"] = pg.evaluate("document.activeElement.className + '|' + document.activeElement.textContent")
    OUT["kb_card"] = pg.evaluate(CARD)
    OUT["kb_ref"] = pg.evaluate("({describedby: document.activeElement.getAttribute('aria-describedby'), expanded: document.activeElement.getAttribute('aria-expanded')})")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(100)
    OUT["kb_escape"] = {"card": pg.evaluate(CARD)["display"],
                        "focus": pg.evaluate("document.activeElement.className + '|' + document.activeElement.textContent"),
                        "expanded": pg.evaluate("document.activeElement.getAttribute('aria-expanded')")}
    pg.keyboard.press("Tab")
    pg.wait_for_timeout(150)
    OUT["kb_next"] = {"focus": pg.evaluate("document.activeElement.textContent"), "card": pg.evaluate(CARD)}
    # a small scroll of the conversation moves the card with its marker; one that takes the
    # marker out of view closes it (wheel over the conversation, away from the card)
    pg.mouse.move(300, 450)
    pg.evaluate("document.getElementById('chat-area').style.scrollBehavior = 'auto'")
    before = pg.evaluate(CARD)["y"]
    pg.evaluate("document.getElementById('chat-area').scrollTop += 30")
    pg.wait_for_timeout(150)
    OUT["kb_scroll_small"] = {"before": before, "after": pg.evaluate(CARD)["y"], "display": pg.evaluate(CARD)["display"],
                              "ref_top": pg.evaluate("Math.round(document.activeElement.getBoundingClientRect().top)")}
    pg.mouse.wheel(0, 3000)
    pg.wait_for_timeout(400)
    OUT["kb_scroll_away"] = pg.evaluate(CARD)["display"]
    pg.evaluate("document.activeElement.scrollIntoView({block:'center'})")
    pg.wait_for_timeout(150)
    # Enter on a marker opens its card and moves focus into it: the card's controls are
    # reachable from the keyboard (Tab stays inside, Escape returns to the marker).
    FOCUS = """() => { const a = document.activeElement, c = document.getElementById('cite-card');
        return {card: getComputedStyle(c).display, role: c.getAttribute('role'), label: c.getAttribute('aria-label'),
                inCard: c.contains(a), focus: a.className + '|' + a.textContent.trim().slice(0, 30)}; }"""
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(200)
    OUT["kb_enter"] = pg.evaluate(FOCUS)
    pg.keyboard.press("Tab")
    pg.wait_for_timeout(100)
    OUT["kb_enter_tab"] = pg.evaluate(FOCUS)
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(150)
    OUT["kb_enter_escape"] = pg.evaluate(FOCUS)
    # ...and Enter on its "Show in sources" jumps to the source, focus going along.
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(150)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)
    OUT["kb_jump"] = pg.evaluate("""() => { const a = document.activeElement;
        return {open: !!a && !!a.querySelector('.rag-chunk-content') && a.querySelector('.rag-chunk-content').classList.contains('open'),
                focusId: a.id, card: getComputedStyle(document.getElementById('cite-card')).display}; }""")
    # the marker of a URL source: the card's link is reached first, then its button
    pg.evaluate("document.querySelector('.cite-ref[data-cite=\"1\"]').focus()")
    pg.wait_for_timeout(150)
    pg.keyboard.press(" ")
    pg.wait_for_timeout(150)
    OUT["kb_space_link"] = pg.evaluate(FOCUS)
    pg.keyboard.press("Tab")
    OUT["kb_space_tab"] = pg.evaluate(FOCUS)
    pg.keyboard.press("Tab")
    OUT["kb_space_wrap"] = pg.evaluate(FOCUS)
    pg.keyboard.press("Escape")
    pg.context.close()


def touch(r):
    pg = _page(r, width=375, height=812, touch=True)
    _open_conversation(pg, touch=True)
    pg.evaluate("document.getElementById('chat-area').scrollTop = 0")
    pg.wait_for_timeout(100)
    # the right-most citation on screen, so the card has to be pulled inside the window
    idx = pg.evaluate("""() => { const refs = [...document.querySelectorAll('#chat-area .cite-ref')];
        let best = 0, bx = -1; refs.forEach((b, i) => { const r = b.getBoundingClientRect();
        if (r.top > 60 && r.bottom < innerHeight - 200 && r.right > bx) { bx = r.right; best = i; } }); return best; }""")
    ref = pg.evaluate(REF, idx)
    pg.touchscreen.tap(ref["x"], ref["y"])
    pg.wait_for_timeout(300)
    OUT["m_ref"] = ref
    OUT["m_card"] = pg.evaluate(CARD)
    OUT["m_scrolled"] = pg.evaluate("document.querySelector('.rag-inspector.open') !== null")
    r.shot(pg, "citations_mobile")
    pg.touchscreen.tap(ref["x"], ref["y"])          # tap again: closes
    pg.wait_for_timeout(250)
    OUT["m_second_tap"] = pg.evaluate(CARD)["display"]
    pg.touchscreen.tap(ref["x"], ref["y"])          # open, then tap elsewhere: closes
    pg.wait_for_timeout(250)
    OUT["m_reopen"] = pg.evaluate(CARD)["display"]
    pg.touchscreen.tap(30, 100)
    pg.wait_for_timeout(250)
    OUT["m_outside"] = pg.evaluate(CARD)["display"]
    OUT["m_toggle"] = pg.evaluate(TOGGLE)
    pg.context.close()


def toggle_and_send(r):
    pg = _page(r)
    OUT["new_conv_pressed"] = pg.evaluate("document.getElementById('docs-only-btn').getAttribute('aria-pressed')")
    pg.locator("#docs-only-btn").click()
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(500)
    OUT["after_click"] = pg.evaluate("""() => ({pressed: document.getElementById('docs-only-btn').getAttribute('aria-pressed'),
        chip: document.querySelector('#scope-row .docs-only-chip')?.innerText.trim() || null,
        rowDisplay: getComputedStyle(document.getElementById('scope-row')).display,
        bg: getComputedStyle(document.getElementById('docs-only-btn')).backgroundColor})""")
    pg.locator("#msg-input").click()
    pg.keyboard.type("what is the warranty?")
    pg.keyboard.press("Enter")
    ux._wait(pg, "() => window.__sockets.at(-1).sent.length > 0", 5)
    OUT["sent"] = pg.evaluate("JSON.parse(window.__sockets.at(-1).sent.at(-1))")
    # the server's documents-only "not found" turn, frame by frame as _answer_not_found sends it
    pg.evaluate("""(t) => { const w = window.__sockets.at(-1);
        w._msg({type:'stream_start'});
        w._msg({type:'rag_context', chunks:[], rag_used:true, docs_only:true, not_found:true, latency:{}});
        w._msg({type:'token', content:t, done:true});
        w._msg({type:'stream_end', latency:{cache:0,rag:0,llm:0,total:0}, usage:null, title:null, cache:null, not_found:true}); }""",
                NOT_FOUND)
    pg.wait_for_timeout(250)
    OUT["live_notfound"] = pg.evaluate("""() => { const w = [...document.querySelectorAll('#chat-area .msg-wrap:not(.user)')].at(-1);
        const b = w.querySelector('.ground-badge'); return b ? {cls: b.className, text: b.textContent,
        nomatch: !!w.querySelector('.rag-nomatch-badge')} : null; }""")
    # the chip's ✕ turns it off; the next question goes without it
    pg.locator("#scope-row .docs-only-chip .scope-chip-x").click()
    pg.wait_for_timeout(100)
    OUT["chip_off"] = pg.evaluate("[document.getElementById('docs-only-btn').getAttribute('aria-pressed'), getComputedStyle(document.getElementById('scope-row')).display]")
    pg.locator("#msg-input").click()
    pg.keyboard.type("and streams?")
    pg.keyboard.press("Enter")
    ux._wait(pg, "() => window.__sockets.at(-1).sent.length > 1", 5)
    OUT["sent2"] = pg.evaluate("JSON.parse(window.__sockets.at(-1).sent.at(-1)).documents_only")
    # a live, streamed answer: graded when its text is final
    pg.evaluate("""(chunks) => { const w = window.__sockets.at(-1);
        w._msg({type:'stream_start'});
        w._msg({type:'rag_context', chunks, rag_used:true, docs_only:false, latency:{rag:0.01}});
        for (const t of ['Streams keep every entry ', 'in insertion order [1]. ', 'Consumer groups share ', 'the reading work between clients [2].'])
          w._msg({type:'token', content:t, done:false});
        w._msg({type:'token', content:'', done:true});
        w._msg({type:'stream_end', latency:{cache:0,rag:0,llm:0,total:0}, usage:null, title:null, cache:{}}); }""",
                CHUNKS)
    pg.wait_for_timeout(400)
    OUT["live_grounded"] = pg.evaluate("""() => { const w = [...document.querySelectorAll('#chat-area .msg-wrap:not(.user)')].at(-1);
        const b = w.querySelector('.ground-badge'); return {badge: b ? b.textContent : null,
        refs: [...w.querySelectorAll('.cite-ref')].map(x => x.textContent),
        stored: S.sessions[S.sessionId].messages.at(-1).meta.rag_used}; }""")
    pg.context.close()


def settings_default(r):
    pg = _page(r, docs_default=True)
    OUT["default_on"] = pg.evaluate("[document.getElementById('docs-only-btn').getAttribute('aria-pressed'), _ragPayload().documents_only]")
    pg.evaluate("openSettings('rag')")
    pg.wait_for_timeout(300)
    OUT["settings_checkbox"] = pg.evaluate("document.getElementById('s-docs-only').checked")
    lab = pg.locator("#s-docs-only").locator("xpath=..")
    lab.scroll_into_view_if_needed()
    lab.click()
    pg.wait_for_timeout(100)
    OUT["settings_after_click"] = pg.evaluate("document.getElementById('s-docs-only').checked")
    OUT["settings_box"] = pg.evaluate(ux.BOX, "#s-docs-only-label")
    pg.evaluate("saveSettings()")
    ux._wait(pg, "() => window.__posts.some(p => p.url === '/api/config')", 5)
    OUT["saved"] = pg.evaluate("window.__posts.filter(p => p.url === '/api/config').at(-1).body.rag.documents_only")
    pg.context.close()


def main(index, engine, shots):
    ux.INDEX = index
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    with sync_playwright() as p:
        try:
            r = ux.Run(p, engine, shots)
        except Exception as exc:                  # noqa: BLE001 — engine not installed
            OUT["skip"] = f"{engine} not available: {exc}"[:300]
            return
        for step in (badges_and_broken, hover_card, keyboard, touch, toggle_and_send, settings_default):
            try:
                step(r)
            except Exception as exc:              # noqa: BLE001 — reported per step
                OUT.setdefault("step_errors", {})[step.__name__] = f"{type(exc).__name__}: {exc}"[:600]
        OUT["errors"] = r.errors[:8]
        OUT["ok"] = True
        r.browser.close()


if __name__ == "__main__":
    shots = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    try:
        main(pathlib.Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv) > 2 else "chromium", shots)
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT, ensure_ascii=False, default=str))
