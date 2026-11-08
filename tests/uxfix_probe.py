# SPDX-License-Identifier: AGPL-3.0-or-later
"""The 1.16.0 review fixes, measured in a real browser with canned API answers and a fake
WebSocket (ux_fixes_probe's harness, no server). Prints one JSON object.

Every value recorded is observed: a computed colour and the contrast it gives, a bounding
box, the element a real click or key press reached, the text a screen reader is handed,
the requests the page sent.

    python uxfix_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys
import time

import ux_fixes_probe as U
from ux_fixes_probe import BOX, CONTRAST, _toasts, _wait

OUT: dict = {}
NOW = int(time.time())

# After FAKE: /api/me (with a switchable 401), status polls that can be refused, DELETE of a
# conversation, and a record of every announcement (#sr-status), as a screen reader gets it.
EXTRA = r"""
(() => {
  const inner = window.fetch;
  window.__deletes = []; window.__said = [];
  window.fetch = (url, opt={}) => {
    const u = String(url), V = window.__UXV, m = (opt.method||'GET').toUpperCase();
    const json = (b, s=200) => Promise.resolve(new Response(JSON.stringify(b), {status:s, headers:{'Content-Type':'application/json'}}));
    if (u === '/api/me') return V.meStatus ? json({detail:'Sign-in required'}, V.meStatus) : json(V.me || {auth:false, admin:true});
    if (V.statusAuth && u.startsWith('/api/status/')) return json({detail:'Sign-in required'}, 401);
    if (m === 'DELETE' && u.startsWith('/api/sessions/')) {
      const id = decodeURIComponent(u.split('/').pop()); window.__deletes.push(id);
      V.sessions = (V.sessions || []).filter(s => s.id !== id); return json({ok:true});
    }
    if (u === '/api/sessions') return json(V.sessions || []);
    return inner(url, opt);
  };
  document.addEventListener('DOMContentLoaded', () => {
    const el = document.getElementById('sr-status');
    if (el) new MutationObserver(() => { const t = el.textContent.trim(); if (t) window.__said.push(t); })
      .observe(el, {childList: true, characterData: true, subtree: true});
  });
})();
"""

A = {"n": 1, "source": "https://docs.example.org/a.html", "text": "Alpha source text about streams.", "score": 0.8,
     "relevance": 0.8, "instance": "default"}
B = {"n": 1, "source": "/data/notes/b.md", "text": "Bravo source text about groups.", "score": 0.7,
     "relevance": 0.7, "instance": "default"}
C = {"n": 2, "source": "/data/notes/c.md", "text": "Charlie source text about trimming.", "score": 0.6,
     "relevance": 0.6, "instance": "default"}


def _u(content):
    return {"role": "user", "content": content, "meta": {"ts": NOW - 60}}


def _a(content, **meta):
    return {"role": "assistant", "content": content,
            "meta": {"ts": NOW - 50, "provider": "ollama", "model": "llama3:latest", **meta}}


def _row(sid, title, ago=50, messages=2, **kw):
    return {"id": sid, "messages": messages, "preview": "", "title": title, "updated": NOW - ago,
            "created": NOW - ago - 10, "pinned": False, "expires": None, **kw}


def _page(r, width=1400, height=900, scheme="light", wait_rows=0, **over):
    v = U._variant(**over)
    ctx = r.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(v) + ";")
    pg.add_init_script(U.FAKE)
    pg.add_init_script(EXTRA)
    pg.goto(U.INDEX.as_uri(), wait_until="load")
    _wait(pg, "() => typeof S === 'object' && S.modelsLoaded && Array.isArray(S.ragInstances)", 10)
    _wait(pg, "() => S.ws && S.ws.readyState === 1", 4)
    if wait_rows:
        _wait(pg, f"() => document.querySelectorAll('#sessions-list .session-row').length >= {wait_rows}", 6)
    pg.wait_for_timeout(250)
    return pg


def _open(pg, sid, n_answers):
    pg.locator(f"#sessions-list [data-sid='{sid}'] .session-title").click()
    _wait(pg, f"() => document.querySelectorAll('#chat-area .msg-wrap:not(.user)').length >= {n_answers}", 5)
    pg.wait_for_timeout(250)


# ── 8. the grounding badge in every state, both themes ───────────────────────
STATES = [
    ("grounded_docs", "Yes, streams keep their entries in insertion order [1].",
     dict(chunks=[A], rag_used=True, docs_only=True)),
    ("partly", "Streams keep their entries in insertion order [1].\n\nConsumer groups share the reading of one stream among many clients.",
     dict(chunks=[A], rag_used=True, docs_only=False)),
    ("uncited_docs", "This answer does not cite anything from the documents at all.",
     dict(chunks=[A], rag_used=True, docs_only=True)),
    ("norag", "Four. It is a basic sum that needs no documents at all.", dict(rag_used=False, docs_only=False)),
    ("noretrieval", "Nothing in the knowledge base matched, so this is a general answer from the model.",
     dict(rag_used=True, docs_only=False)),
    ("searchfailed", "Search failed: the knowledge base “default” could not be searched — its storage could not be reached.",
     dict(rag_used=True, docs_only=True, search_failed=True, kbs=["default"])),
    ("notfound", "Not found in your documents.", dict(rag_used=True, docs_only=True, not_found=True)),
]
# CONTRAST, with the element's (and its ancestors') opacity applied to the text colour:
# a suffix drawn at opacity .85 is that much fainter than its colour says.
CONTRAST_OP = CONTRAST.replace("let fg = parse(getComputedStyle(el).color);",
    "let fg = parse(getComputedStyle(el).color); { let o = 1; for (let e = el; e; e = e.parentElement) o *= +getComputedStyle(e).opacity; fg = {...fg, a: fg.a * o}; }")
assert CONTRAST_OP != CONTRAST
BADGES = r"""(contrast) => [...document.querySelectorAll('#chat-area .msg-wrap:not(.user)')].map(w => {
  const b = w.querySelector('.msg-meta .ground-badge'); if (!b) return null;
  const m = b.querySelector('.g-mode'), r = b.getBoundingClientRect();
  return {cls: b.className, text: b.textContent, aria: b.getAttribute('aria-label'), display: getComputedStyle(b).display,
          w: Math.round(r.width), h: Math.round(r.height), badge: contrast(b), mode: m ? contrast(m) : null,
          nomatch: !!w.querySelector('.rag-nomatch-badge')}; })"""


def badges(r):
    msgs = []
    for _k, text, meta in STATES:
        msgs += [_u("q"), _a(text, **meta)]
    for scheme in ("light", "dark"):
        pg = _page(r, scheme=scheme, wait_rows=1, sessions=[_row("s_b", "Badges", messages=len(msgs))],
                   mine=["s_b"], sessionMsgs={"s_b": msgs}, theme=scheme)
        if scheme == "dark":
            pg.evaluate("applyTheme('dark')")
        _open(pg, "s_b", len(STATES))
        got = pg.evaluate(f"(() => {{ const contrast = {CONTRAST_OP}; return ({BADGES})(contrast); }})()")
        OUT[f"badges_{scheme}"] = {k: g for (k, _t, _m), g in zip(STATES, got)}
        OUT[f"badges_{scheme}_theme"] = pg.evaluate("document.documentElement.getAttribute('data-theme')")
        r.shot(pg, f"badges_{scheme}")
        pg.context.close()
    # at phone width the longest badge stays inside the window
    pg = _page(r, width=375, height=800, wait_rows=1, sessions=[_row("s_b", "Badges", messages=len(msgs))],
               mine=["s_b"], sessionMsgs={"s_b": msgs})
    pg.evaluate("openSession('s_b')")                      # the phone sidebar is an overlay; opening is not under test
    _wait(pg, f"() => document.querySelectorAll('#chat-area .ground-badge').length >= {len(STATES)}", 5)
    OUT["badges_375"] = pg.evaluate("""() => ({vw: document.documentElement.clientWidth,
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        badges: [...document.querySelectorAll('#chat-area .ground-badge')].map(b => { const r = b.getBoundingClientRect();
          return {text: b.textContent, left: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width)}; })})""")
    pg.context.close()


# ── 5. each answer version with its own sources, mode and badge ──────────────
INSPECTOR = r"""(id) => { const el = document.getElementById(id); if (!el) return null;
  const b = el.querySelector('.ground-badge'), insp = el.querySelector('.rag-inspector');
  return {sources: insp ? [...insp.querySelectorAll('[id^="cite-' + id + '-"]')].map(c => c.textContent.includes('a.html') ? 'A'
                     : c.textContent.includes('b.md') ? 'B' : c.textContent.includes('c.md') ? 'C' : '?') : [],
          inspDisplay: insp ? getComputedStyle(insp).display : null, inspH: insp ? Math.round(insp.getBoundingClientRect().height) : 0,
          badge: b ? b.textContent : null, refs: [...el.querySelectorAll('.msg-bubble .cite-ref')].map(x => x.textContent),
          broken: el.querySelectorAll('.msg-bubble .cite-broken').length,
          ver: el.querySelector('.ver-num')?.textContent || null}; }"""


def versions(r):
    msgs = [_u("tell me about streams"), _a("Streams are append-only [1].", chunks=[A], rag_used=True, docs_only=False)]
    pg = _page(r, wait_rows=1, sessions=[_row("s_v", "Versions")], mine=["s_v"], sessionMsgs={"s_v": msgs})
    _open(pg, "s_v", 1)
    aid = pg.evaluate("document.querySelector('#chat-area .msg-wrap:not(.user)').id")
    OUT["ver_before"] = pg.evaluate(INSPECTOR, aid)
    # ↻ by a real click, then the server's answer: other sources, under Documents only.
    pg.locator(f"#{aid} .msg-action-btn[title^='Re-run the question']").click()
    _wait(pg, "() => window.__sockets.at(-1).sent.length > 0", 3)
    OUT["ver_regen_sent"] = pg.evaluate("JSON.parse(window.__sockets.at(-1).sent.at(-1)).replace_from")
    pg.evaluate("""() => { const ws = S.ws;
        ws._msg({type: 'stream_start'});
        ws._msg({type: 'rag_context', chunks: %s, rag_used: true, docs_only: true, latency: {rag: 0.1}});
        ws._msg({type: 'token', content: 'Groups share a stream [1], and streams can be trimmed [2].', done: true});
        ws._msg({type: 'stream_end', latency: {total: 0.2}, cache: null}); }""" % json.dumps([B, C]))
    pg.wait_for_timeout(300)
    nid = pg.evaluate("[...document.querySelectorAll('#chat-area .msg-wrap:not(.user)')].pop().id")
    OUT["ver_latest"] = pg.evaluate(INSPECTOR, nid)
    pg.locator(f"#{nid} .ver-btn").first.click()          # ‹ : version 1
    pg.wait_for_timeout(250)
    OUT["ver_first"] = pg.evaluate(INSPECTOR, nid)
    # the source card of [1] in version 1 is version 1's source (hover with the real mouse)
    box = pg.evaluate(f"(() => {{ const b = document.querySelector('#{nid} .msg-bubble .cite-ref').getBoundingClientRect(); return [b.left + b.width/2, b.top + b.height/2]; }})()")
    pg.mouse.move(*box)
    pg.wait_for_timeout(400)
    OUT["ver_first_card"] = pg.evaluate("document.querySelector('#cite-card .cite-card-src')?.textContent || null")
    pg.mouse.move(5, 5)
    pg.wait_for_timeout(300)
    pg.locator(f"#{nid} .ver-btn").last.click()           # › : version 2 again
    pg.wait_for_timeout(250)
    OUT["ver_second_again"] = pg.evaluate(INSPECTOR, nid)
    r.shot(pg, "versions")
    pg.context.close()


# ── 1. conversations stored on the server but not listed in this browser ─────
UNL = r"""() => { const el = document.getElementById('unlisted-sessions'); if (!el) return null;
  const r = el.getBoundingClientRect(), b = [...el.querySelectorAll('button')];
  return {hidden: el.hidden, display: getComputedStyle(el).display, h: Math.round(r.height), w: Math.round(r.width),
          text: document.getElementById('unlisted-text').textContent,
          buttons: b.map(x => { const q = x.getBoundingClientRect(); return {text: x.textContent, display: getComputedStyle(x).display,
                                 w: Math.round(q.width), h: Math.round(q.height), right: Math.round(q.right)}; }),
          actsDisplay: getComputedStyle(el.querySelector('.unl-acts')).display,
          panelRight: Math.round(document.querySelector('#tab-general').getBoundingClientRect().right)}; }"""
ROWS = "() => [...document.querySelectorAll('#sessions-list .session-row')].map(r => r.dataset.sid)"


def _settings_general(pg):
    pg.locator("#sidebar-footer .sidebar-btn", has_text="Settings").click()
    _wait(pg, "() => document.getElementById('settings-overlay').classList.contains('open')", 3)
    pg.wait_for_timeout(300)


def unlisted(r):
    sessions = [_row("s_mine", "Mine"), _row("s_other1", "From the laptop", ago=100),
                _row("s_other2", "Pinned elsewhere", ago=200, pinned=True), _row("s_empty", "Never used", messages=0)]
    for w in (1400, 375):
        pg = _page(r, width=w, height=800, wait_rows=1, sessions=sessions, mine=["s_mine"])
        key = f"unl_{w}"
        OUT[key + "_rows"] = pg.evaluate(ROWS)
        if w == 375:
            pg.evaluate("openSettings('general')")          # the phone sidebar is an overlay; opening is not under test
            pg.wait_for_timeout(300)
        else:
            _settings_general(pg)
        pg.evaluate("document.getElementById('unlisted-sessions').scrollIntoView({block:'center'})")
        pg.wait_for_timeout(150)
        OUT[key] = pg.evaluate(UNL)
        OUT[key + "_overflow"] = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        r.shot(pg, key)
        if w == 1400:
            pg.locator("#unlisted-show").click()
            _wait(pg, "() => document.querySelectorAll('#sessions-list .session-row').length >= 4", 3)
            pg.wait_for_timeout(200)
            OUT["unl_after_show"] = {"rows": pg.evaluate(ROWS), "panel": pg.evaluate(UNL),
                                     "stored": pg.evaluate("JSON.parse(localStorage.getItem('mySessionIds'))")}
        pg.context.close()
    # delete: confirmation first, then one DELETE per unlisted conversation
    pg = _page(r, wait_rows=1, sessions=[dict(s) for s in sessions], mine=["s_mine"])
    _settings_general(pg)
    pg.evaluate("document.getElementById('unlisted-sessions').scrollIntoView({block:'center'})")
    pg.locator("#unlisted-del").click()
    pg.wait_for_timeout(200)
    OUT["unl_modal"] = U._modal(pg)
    OUT["unl_deletes_before_confirm"] = pg.evaluate("window.__deletes.slice()")
    pg.locator("#modal-actions button", has_text="Delete 2").click()
    _wait(pg, "() => window.__deletes.length >= 2", 3)
    pg.wait_for_timeout(300)
    OUT["unl_deleted"] = {"deletes": pg.evaluate("window.__deletes.slice()"), "panel": pg.evaluate(UNL),
                          "rows": pg.evaluate(ROWS)}
    pg.context.close()
    # sign-in on: every one of the user's own conversations is listed; no such control
    pg = _page(r, wait_rows=1, sessions=sessions, mine=[], me={"auth": True, "user": "ann", "admin": True})
    _settings_general(pg)
    OUT["unl_signin"] = pg.evaluate(UNL)
    pg.context.close()


# ── 3. a user who is not an administrator ────────────────────────────────────
CHECK = r"""() => { const el = document.getElementById('welcome-checklist'); if (!el) return null;
  const r = el.getBoundingClientRect();
  return {hidden: el.hidden, h: Math.round(r.height),
          rows: [...el.querySelectorAll('.fr-item')].map(li => { const a = li.querySelector('.fr-ask'), ab = a && a.getBoundingClientRect();
            return {cls: li.className, tc: li.textContent.replace(/\s+/g, ' ').trim(), buttons: li.querySelectorAll('button').length,
                    ask: a ? {text: a.textContent, display: getComputedStyle(a).display, w: Math.round(ab.width), h: Math.round(ab.height),
                              right: Math.round(ab.right)} : null}; }),
          right: Math.round(r.right)}; }"""
STRIP = r"""() => [...document.querySelectorAll('#health-strip .hs-item')].map(el => ({tag: el.tagName,
  text: el.textContent.replace(/\s+/g, ' ').trim(), label: el.getAttribute('aria-label') || el.title}))"""


def nonadmin(r):
    user = {"auth": True, "user": "bob", "admin": False}
    inst0 = [{"name": "default", "chunks": 0, "color": "#7c6ff7", "redis_endpoint": "default", "reachable": True, "enabled": True}]
    for w in (1400, 375):
        pg = _page(r, width=w, height=800, me=user, ollama=[], ollamaStatus={"ok": True, "models": 0}, inst=inst0)
        OUT[f"na_checklist_{w}"] = pg.evaluate(CHECK)
        OUT[f"na_strip_{w}"] = pg.evaluate(STRIP)
        OUT[f"na_overflow_{w}"] = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        OUT[f"na_ask_contrast_{w}"] = pg.evaluate(CONTRAST, pg.query_selector("#welcome-checklist .fr-ask"))
        r.shot(pg, f"nonadmin_{w}")
        if w == 1400:
            # a real click on the strip's model item: nothing opens (it is not a button)
            pg.locator("#health-strip .hs-item", has_text="Model").click()
            pg.wait_for_timeout(200)
            OUT["na_strip_click"] = pg.evaluate("document.getElementById('settings-overlay').classList.contains('open')")
            OUT["na_add_docs"] = pg.evaluate("(async () => { await _frAddDocuments(); return {open: document.getElementById('settings-overlay').classList.contains('open')}; })()")
            OUT["na_add_docs_toasts"] = _toasts(pg)
        pg.context.close()
    pg = _page(r, width=375, height=800, scheme="dark", theme="dark", me=user, ollama=[],
               ollamaStatus={"ok": True, "models": 0}, inst=inst0)
    pg.evaluate("applyTheme('dark')")
    pg.wait_for_timeout(100)
    OUT["na_ask_contrast_dark"] = pg.evaluate(CONTRAST, pg.query_selector("#welcome-checklist .fr-ask"))
    pg.context.close()
    # only the documents are missing: nothing for this user to do, so no checklist
    pg = _page(r, me=user, inst=inst0, ollamaStatus={"ok": True, "models": 3})
    OUT["na_only_docs"] = pg.evaluate(CHECK)
    pg.context.close()
    # an answer's 💾 Save to knowledge base is not offered; 🔖 is
    msgs = [_u("q"), _a("An answer [1].", chunks=[A], rag_used=True)]
    pg = _page(r, me=user, wait_rows=1, sessions=[_row("s_n", "N")], mine=[], sessionMsgs={"s_n": msgs})
    _open(pg, "s_n", 1)
    OUT["na_actions"] = pg.evaluate("""() => [...document.querySelectorAll('#chat-area .msg-wrap:not(.user) .msg-action-btn')].map(b => ({
        text: b.textContent.trim(), display: getComputedStyle(b).display, w: Math.round(b.getBoundingClientRect().width),
        label: b.getAttribute('aria-label') || b.title}))""")
    pg.context.close()
    # the same page for an administrator still offers both
    pg = _page(r, wait_rows=1, sessions=[_row("s_n", "N")], mine=["s_n"], sessionMsgs={"s_n": msgs})
    _open(pg, "s_n", 1)
    OUT["admin_actions"] = pg.evaluate("""() => [...document.querySelectorAll('#chat-area .msg-wrap:not(.user) .msg-action-btn')].map(b => ({
        text: b.textContent.trim(), display: getComputedStyle(b).display}))""")
    OUT["bookmarks_label"] = pg.evaluate("""() => [...document.querySelectorAll('#sidebar .sidebar-label')].map(l => l.textContent)
        .concat([...document.querySelectorAll('#sidebar .sidebar-btn')].map(b => b.textContent.trim()).filter(t => /ookmark/.test(t)))""")
    pg.context.close()


# ── 6 + 12. signed out; one announcement per queued send ─────────────────────
WSTRIP = r"""() => { const items = [...document.querySelectorAll('#health-strip .hs-item')];
  const btns = [...document.querySelectorAll('#health-strip .hs-retry')];
  return {items: items.map(el => ({cls: el.className, text: el.textContent.replace(/\s+/g, ' ').trim()})),
          buttons: btns.map(b => { const r = b.getBoundingClientRect(); return {text: b.textContent, w: Math.round(r.width), h: Math.round(r.height),
                                    display: getComputedStyle(b).display}; }),
          state: S.wsState, signedOut: !!S.signedOut}; }"""


def signed_out(r):
    me = {"auth": True, "user": "ann", "admin": True}
    pg = _page(r, me=me, inst=U.INST)
    # The proxy session ends: the server refuses the socket's reconnect (it never opens) and
    # /api/me answers 401. A message typed meanwhile must come back, not vanish.
    pg.evaluate("window.__UXV.meStatus = 401; window.__wsDown = true; S.ws._drop();")
    pg.fill("#msg-input", "Is anyone there?")
    pg.keyboard.press("Enter")
    _wait(pg, "() => S.signedOut === true", 6, 100)
    pg.wait_for_timeout(300)
    n = pg.evaluate("window.__sockets.length")
    OUT["so_strip"] = pg.evaluate(WSTRIP)
    OUT["so_input"] = pg.evaluate("document.getElementById('msg-input').value")
    OUT["so_toasts"] = _toasts(pg)
    OUT["so_said"] = pg.evaluate("window.__said.slice(-3)")
    OUT["so_streaming"] = pg.evaluate("S.streaming")
    pg.wait_for_timeout(4500)                      # two reconnect periods: none is attempted
    OUT["so_new_sockets"] = pg.evaluate("window.__sockets.length") - n
    pg.evaluate("window.dispatchEvent(new Event('online')); _wsRetryNow();")
    pg.wait_for_timeout(300)
    OUT["so_new_sockets_retry"] = pg.evaluate("window.__sockets.length") - n
    r.shot(pg, "signed_out")
    pg.context.close()
    # a status poll answered 401 is a sign-in matter, not "storage cannot be reached" (at phone width)
    pg = _page(r, width=375, height=800, me=me, inst=U.INST)
    pg.evaluate("window.__UXV.meStatus = 401; window.__UXV.statusAuth = true;")
    pg.evaluate("checkLocalStatus()")
    _wait(pg, "() => S.signedOut === true", 4, 100)
    pg.wait_for_timeout(200)
    OUT["so_poll_strip"] = pg.evaluate(WSTRIP)
    OUT["so_poll_375"] = pg.evaluate("""() => { const s = document.getElementById('health-strip'), b = s.querySelector('.hs-retry').getBoundingClientRect();
        return {vw: document.documentElement.clientWidth, stripOverflow: s.scrollWidth - s.clientWidth,
                docOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
                reload: {left: Math.round(b.left), right: Math.round(b.right), w: Math.round(b.width), h: Math.round(b.height)}}; }""")
    r.shot(pg, "signed_out_375")
    pg.context.close()
    # sign-in off and the server merely away: still reconnecting, never "signed out"
    pg = _page(r, inst=U.INST)
    pg.evaluate("window.__wsDown = true; S.ws._drop();")
    pg.wait_for_timeout(2600)
    OUT["away_strip"] = pg.evaluate(WSTRIP)
    pg.context.close()


def announce_once(r):
    pg = _page(r, inst=U.INST)
    # (a) the socket has dropped and its close event has not arrived yet: one notice, which
    # says both that the connection is being re-established and that the message waits.
    pg.evaluate("window.__wsDown = true; S.ws.readyState = 3; window.__said.length = 0;")
    pg.fill("#msg-input", "First")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(400)
    pg.evaluate("S.ws.onclose && S.ws.onclose({})")
    pg.wait_for_timeout(500)
    OUT["said_unclosed"] = pg.evaluate("window.__said.slice()")
    pg.context.close()
    pg = _page(r, inst=U.INST)
    # (b) the drop was already announced; the send adds exactly one notice
    pg.evaluate("window.__wsDown = true; S.ws._drop();")
    pg.wait_for_timeout(300)
    pg.evaluate("window.__said.length = 0")
    pg.fill("#msg-input", "Second")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(600)
    OUT["said_after_drop"] = pg.evaluate("window.__said.slice()")
    pg.context.close()


# ── 11. the conversation list's structure ────────────────────────────────────
A11Y = r"""() => { const L = document.getElementById('sessions-list');
  const nested = [...L.querySelectorAll('button, [role="button"], a[href], input')].filter(x =>
      x.parentElement.closest('button, [role="button"]') && L.contains(x.parentElement.closest('button, [role="button"]')));
  return {groups: [...L.querySelectorAll('.session-group')].map(g => ({text: g.textContent, role: g.getAttribute('role'), level: g.getAttribute('aria-level')})),
          roleButtons: L.querySelectorAll('[role="button"]').length, nested: nested.length,
          rows: [...L.querySelectorAll('.session-row')].map(r => { const o = r.querySelector('.session-open'), b = r.getBoundingClientRect(),
                   ob = o.getBoundingClientRect(), t = r.querySelector('.session-title').getBoundingClientRect();
                   return {sid: r.dataset.sid, open: o.tagName, name: o.getAttribute('aria-label'), current: o.getAttribute('aria-current'),
                           rowW: Math.round(b.width), rowH: Math.round(b.height), openW: Math.round(ob.width), titleW: Math.round(t.width),
                           titleH: Math.round(t.height), openRight: Math.round(ob.right), rowRight: Math.round(b.right)}; })}; }"""


def session_list(r):
    sessions = [_row("s_today", "Boiler pressure notes", ago=600), _row("s_week", "Warranty terms", ago=3 * 86400),
                _row("s_pin", "Pinned plan", ago=40 * 86400, pinned=True)]
    for w in (1400, 375):
        pg = _page(r, width=w, height=800, wait_rows=3, sessions=sessions, mine=["s_today", "s_week", "s_pin"])
        if w == 375:
            pg.evaluate("toggleSidebar()")
            pg.wait_for_timeout(300)
        OUT[f"sl_{w}"] = pg.evaluate(A11Y)
        OUT[f"sl_aria_{w}"] = pg.locator("#sessions-list").aria_snapshot()
        r.shot(pg, f"session_list_{w}")
        pg.context.close()
    # keyboard: Tab to a conversation's main button, Tab on to its actions, Enter opens it
    pg = _page(r, wait_rows=3, sessions=sessions, mine=["s_today", "s_week", "s_pin"])
    pg.evaluate("document.querySelector('#sessions-list [data-sid=\"s_week\"] .session-open').focus()")
    seq = [pg.evaluate("document.activeElement.getAttribute('aria-label')")]
    for _ in range(3):
        pg.keyboard.press("Tab")
        seq.append(pg.evaluate("""(() => { const a = document.activeElement, cs = getComputedStyle(a), b = a.getBoundingClientRect();
            return (a.getAttribute('aria-label') || '') + '|' + cs.display + '|' + Math.round(b.width); })()"""))
    OUT["sl_tab"] = seq
    pg.keyboard.press("Shift+Tab")
    pg.keyboard.press("Shift+Tab")
    pg.keyboard.press("Shift+Tab")
    OUT["sl_back_on_open"] = pg.evaluate("document.activeElement.className")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(300)
    OUT["sl_enter_opened"] = pg.evaluate("S.sessionId")
    OUT["sl_stored_after_open"] = pg.evaluate("JSON.parse(localStorage.getItem('mySessionIds'))")
    # rename still works from a real click: the box stands in for the main button
    x, y = pg.evaluate("""(() => { const b = document.querySelector('#sessions-list [data-sid="s_today"] .session-act').getBoundingClientRect();
        return [b.left + b.width/2, b.top + b.height/2]; })()""") if pg.evaluate(
        "getComputedStyle(document.querySelector('#sessions-list [data-sid=\"s_today\"] .session-act')).display") != "none" else (None, None)
    if x is None:
        rb = pg.evaluate("""(() => { const b = document.querySelector('#sessions-list [data-sid="s_today"]').getBoundingClientRect(); return [b.left + 20, b.top + b.height/2]; })()""")
        pg.mouse.move(*rb)
        pg.wait_for_timeout(150)
        x, y = pg.evaluate("""(() => { const b = document.querySelector('#sessions-list [data-sid="s_today"] .session-act').getBoundingClientRect();
            return [b.left + b.width/2, b.top + b.height/2]; })()""")
    pg.mouse.click(x, y)
    pg.wait_for_timeout(150)
    pg.keyboard.type(" X")
    OUT["sl_rename"] = pg.evaluate("""(() => { const i = document.activeElement, b = i.getBoundingClientRect();
        return {cls: i.className, value: i.value, inButton: !!i.closest('button'), w: Math.round(b.width),
                openHidden: document.querySelector('#sessions-list [data-sid="s_today"] .session-open').hidden}; })()""")
    pg.keyboard.press("Escape")
    pg.context.close()


def main(index: pathlib.Path, engine: str, shots) -> None:
    U.INDEX = index
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    with sync_playwright() as p:
        try:
            r = U.Run(p, engine, shots)
        except Exception as exc:                  # noqa: BLE001 — engine not installed
            OUT["skip"] = f"{engine} not available: {exc}"[:300]
            return
        for step in (badges, versions, unlisted, nonadmin, signed_out, announce_once, session_list):
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
