# SPDX-License-Identifier: AGPL-3.0-or-later
"""Projects in the sidebar, each conversation's own knowledge-base selection, and `#`
mentions in the composer — in a real browser with canned API answers (ux_fixes_probe's
fake fetch and WebSocket, no server), driven with real pointer and keyboard input.
Prints one JSON object of measured values: computed display, bounding boxes, what was
sent, focus.

    python projects_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys
import time

import ux_fixes_probe as ux

OUT: dict = {}
NOW = int(time.time())
P1 = {"id": "p_aaaaaaaaaaaa", "name": "Manuals", "icon": "📘", "color": "#1d4ed8", "kbs": ["kb1", "kb2"],
      "template": "Redis Expert", "provider": "", "model": "", "documents_only": True}
P2 = {"id": "p_bbbbbbbbbbbb", "name": "<img src=x onerror=window.__xss=1>", "icon": "", "color": "",
      "kbs": [], "template": "", "provider": "", "model": "", "documents_only": None}
EVIL_KB = "My <b>Docs</b>"

# /api/projects (GET/POST/PATCH/DELETE), PATCH /api/sessions/{sid} and /api/rag/advice,
# answered the way routes_projects / routes_misc answer them.
SHIM = r"""
(() => {
  const inner = window.fetch;
  window.__projCalls = [];
  const json = (b, s=200) => Promise.resolve(new Response(JSON.stringify(b), {status:s, headers:{'Content-Type':'application/json'}}));
  window.fetch = (url, opt={}) => {
    const u = String(url), m = (opt.method||'GET').toUpperCase(), V = window.__UXV;
    let body = null; try { body = typeof opt.body === 'string' ? JSON.parse(opt.body) : null; } catch(e) {}
    if (u === '/api/projects' || u.startsWith('/api/projects/') || u.startsWith('/api/rag/advice')
        || (m === 'PATCH' && u.startsWith('/api/sessions/')))
      window.__projCalls.push({m, u, body});
    if (u === '/api/projects' && m === 'GET') return json(V.projects.map(p => ({...p})));
    if (u === '/api/projects' && m === 'POST') {
      if (!body.name || !body.name.trim()) return json({detail:'name is required'}, 400);
      const p = {id:'p_cccccccccccc', icon:'', color:'', kbs:[], template:'', provider:'', model:'', documents_only:null, ...body};
      V.projects.push(p); return json(p);
    }
    if (u.startsWith('/api/projects/')) {
      const id = decodeURIComponent(u.split('/').pop()), i = V.projects.findIndex(p => p.id === id);
      if (i < 0) return json({detail:'project not found'}, 404);
      if (m === 'PATCH') { Object.assign(V.projects[i], body); return json(V.projects[i]); }
      if (m === 'DELETE') { V.projects.splice(i, 1);
        const n = (V.sessions||[]).filter(s => s.project === id).length;
        (V.sessions||[]).forEach(s => { if (s.project === id) delete s.project; });
        return json({ok:true, unfiled:n}); }
    }
    if (u.startsWith('/api/rag/advice')) return json({advice:{settings:{}, corpora:[]}, presets:[]});
    if (m === 'PATCH' && u.startsWith('/api/sessions/')) {
      const id = decodeURIComponent(u.split('/').pop());
      const row = (V.sessions||[]).find(r => r.id === id) || {id, messages:2};
      Object.assign(row, body); return json(row);
    }
    return inner(url, opt);
  };
})();
"""

BOX = ux.BOX
SENT = "() => { const s = window.__sockets.filter(x => x.readyState === 1).pop(); return s ? s.sent.map(d => JSON.parse(d)) : []; }"
ROWS = r"""() => [...document.querySelectorAll('#sessions-list > *')].map(el => el.classList.contains('session-group')
  ? 'G:' + el.textContent.trim() : el.classList.contains('session-empty') ? 'E' : el.dataset.sid)"""


def _variant(projects=None):
    inst = [{"name": n, "chunks": 3, "color": c, "redis_endpoint": "default", "reachable": True, "enabled": True}
            for n, c in (("default", "#7c6ff7"), ("kb1", "#16a34a"), ("kb2", "#dc2626"), (EVIL_KB, "#0891b2"))]

    def row(sid, title, ago, **kw):
        return {"id": sid, "messages": 2, "preview": "", "title": title, "updated": NOW - ago,
                "created": NOW - ago - 60, "model": "llama3:latest", "provider": "ollama", "kbs": ["default"],
                "pinned": False, "expires": None, **kw}
    sessions = [row("s_a", "Manual chat", 3600, project=P1["id"], kb_select=["kb1", "kb2"], template="Redis Expert"),
                row("s_b", "Loose chat", 7200, kb_select=["kb2"]),
                row("s_c", "Odd project chat", 9000, project=P2["id"], kb_select="all")]
    msgs = {s["id"]: [{"role": "user", "content": "q " + s["id"], "meta": {}},
                      {"role": "assistant", "content": "a", "meta": {"model": "llama3:latest", "provider": "ollama"}}]
            for s in sessions}
    return ux._variant(inst=inst, sessions=sessions, sessionMsgs=msgs, mine=[s["id"] for s in sessions],
                       projects=[dict(P1), dict(P2)] if projects is None else projects)


def _page(r, width=1400, height=900, ctx_kw=None, admin=True, projects=None):
    v = _variant(projects)
    ctx = r.browser.new_context(viewport={"width": width, "height": height}, **(ctx_kw or {}))
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(v) + ";")
    pg.add_init_script(ux.FAKE)
    pg.add_init_script(SHIM)
    pg.goto(ux.INDEX.as_uri(), wait_until="load")
    ux._wait(pg, f"() => typeof S === 'object' && S.modelsLoaded && S.projects.length === {len(v['projects'])} && S.ragInstances.length === 4"
                 " && document.querySelectorAll('#sessions-list .session-row').length >= 4"
                 " && S.templates.length > 0", 10)
    pg.wait_for_timeout(300)
    if not admin:
        pg.evaluate("() => { S.me = {auth:true, user:'bob', admin:false}; _applySignedIn(); }")
    return pg


def _click(pg, js):
    b = pg.evaluate(f"() => {{ const e = ({js}); e.scrollIntoView({{block:'nearest'}}); const r = e.getBoundingClientRect();"
                    f" return [r.left + r.width / 2, r.top + r.height / 2]; }}")
    pg.mouse.click(*b)
    pg.wait_for_timeout(150)
    return b


def _hit(pg, js):
    """Does a click at the element's centre land on it (not under another layer)?"""
    return pg.evaluate(f"() => {{ const e = ({js}); const r = e.getBoundingClientRect();"
                       f" const t = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);"
                       f" return !!t && (t === e || e.contains(t)); }}")


def _row(sid):
    return f"[...document.querySelectorAll('#sessions-list .session-row')].find(r => r.dataset.sid === '{sid}')"


def _state(pg):
    return pg.evaluate("""() => ({sid: S.sessionId, project: S.sessions[S.sessionId]?.project || null,
        sel: _kbSelection(), ragSelect: document.getElementById('rag-select')?.value,
        setOpt: document.querySelector('#rag-select option[value="__set__"]')?.textContent || null,
        tpl: document.getElementById('template-select').dataset.pick || '', docsOnly: _docsOnly(),
        model: S.currentModel, prefs: JSON.parse(localStorage.getItem('rr.prefs') || '{}')})""")


def switcher(r):
    pg = _page(r)
    OUT["bar"] = {k: pg.evaluate(BOX, s) for k, s in (("bar", "#project-bar"), ("filter", "#project-filter"),
                                                       ("new", "#project-new-btn"), ("edit", "#project-edit-btn"),
                                                       ("sidebar", "#sidebar"), ("newchat", ".new-chat-btn"),
                                                       ("list", "#sessions-list"))}
    OUT["filter_options"] = pg.evaluate("[...document.querySelectorAll('#project-filter option')].map(o => [o.value, o.textContent])")
    OUT["xss"] = pg.evaluate("() => ({fired: window.__xss || 0, imgs: document.querySelectorAll('#project-bar img, #sessions-list img').length})")
    OUT["rows_all"] = pg.evaluate(ROWS)
    OUT["row_marks"] = pg.evaluate("""() => [...document.querySelectorAll('#sessions-list .session-row')].map(r => {
        const i = r.querySelector('.icon'); const b = i.getBoundingClientRect();
        return [r.dataset.sid, i.textContent, i.classList.contains('session-proj') ? getComputedStyle(i).color : null,
                Math.round(b.width), r.querySelector('.session-open').getAttribute('aria-label')]; })""")
    # pick the project with the switcher: the list shows its conversations (date groups kept)
    pg.select_option("#project-filter", P1["id"])
    pg.wait_for_timeout(200)
    OUT["rows_p1"] = pg.evaluate(ROWS)
    OUT["edit_shown"] = pg.evaluate(BOX, "#project-edit-btn")
    pg.wait_for_timeout(500)                     # the border colour is transitioned
    OUT["filter_border"] = pg.evaluate("getComputedStyle(document.getElementById('project-filter')).borderTopColor")
    pg.select_option("#project-filter", "__none__")
    pg.wait_for_timeout(150)
    OUT["rows_none"] = pg.evaluate(ROWS)
    pg.select_option("#project-filter", P1["id"])
    pg.wait_for_timeout(150)
    # New Chat inside the project: its knowledge bases, template and Documents only
    _click(pg, "document.querySelector('.new-chat-btn')")
    pg.wait_for_timeout(250)
    OUT["new_in_project"] = _state(pg)
    OUT["project_chip"] = pg.evaluate("""() => { const c = document.querySelector('#scope-row .project-chip'); if (!c) return null;
        const b = c.getBoundingClientRect(); return {text: c.innerText, display: getComputedStyle(c).display,
        w: Math.round(b.width), h: Math.round(b.height), border: getComputedStyle(c).borderTopColor}; }""")
    pg.click("#msg-input")
    pg.keyboard.type("how do I reset it?")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(200)
    OUT["sent_in_project"] = pg.evaluate(SENT)[-1:]
    r.shot(pg, "projects_switcher")
    # reopen a conversation without a project: ITS selection comes back (kb2), not the project's
    pg.select_option("#project-filter", "")
    pg.wait_for_timeout(150)
    _click(pg, _row("s_b") + ".querySelector('.session-title')")
    pg.wait_for_timeout(400)
    OUT["open_loose"] = _state(pg)
    # ...and the project's conversation: its stored pair of knowledge bases
    _click(pg, _row("s_a") + ".querySelector('.session-title')")
    pg.wait_for_timeout(400)
    OUT["open_project"] = _state(pg)
    # a change inside the project's conversation is the conversation's: stored with it, the
    # browser's preference untouched
    prefs_before = pg.evaluate("localStorage.getItem('rr.prefs')")
    pg.select_option("#rag-select", "kb1§default")
    pg.wait_for_timeout(700)
    OUT["change_in_project"] = {"prefs_same": pg.evaluate("localStorage.getItem('rr.prefs')") == prefs_before,
                                "patches": pg.evaluate("window.__projCalls.filter(c => c.m === 'PATCH' && c.u.startsWith('/api/sessions/'))"),
                                "state": _state(pg)}
    # a change in a conversation without a project still updates the preference (as before)
    _click(pg, _row("s_b") + ".querySelector('.session-title')")
    pg.wait_for_timeout(400)
    pg.select_option("#rag-select", "__all__")
    pg.wait_for_timeout(700)
    OUT["change_loose"] = {"prefs": pg.evaluate("JSON.parse(localStorage.getItem('rr.prefs')||'{}').ragScope"),
                           "patch": pg.evaluate("window.__projCalls.filter(c => c.m === 'PATCH' && c.u === '/api/sessions/s_b').map(c => c.body)")}
    # a new conversation without a project starts from the browser's preference
    _click(pg, "document.querySelector('.new-chat-btn')")
    pg.wait_for_timeout(300)
    OUT["new_loose"] = _state(pg)
    pg.context.close()


LABEL = """() => { const l = document.querySelector('.sessions-label'), b = document.getElementById('project-new-btn');
    const lb = l.getBoundingClientRect(), bb = b.getBoundingClientRect();
    return {label_h: Math.round(lb.height), btn: {display: getComputedStyle(b).display, w: Math.round(bb.width), h: Math.round(bb.height),
            x: Math.round(bb.left), right: Math.round(bb.right)}, label_right: Math.round(lb.right),
            hit: document.elementFromPoint(bb.left + bb.width / 2, bb.top + bb.height / 2) === b}; }"""


def empty(r):
    """No project yet: no switcher (the list keeps its room), only ＋ Project in the label."""
    pg = _page(r, projects=[])
    OUT["empty_bar"] = pg.evaluate(BOX, "#project-bar")
    OUT["empty_label"] = pg.evaluate(LABEL)
    OUT["empty_list_y"] = pg.evaluate(BOX, "#sessions-list")["y"]
    pg.context.close()


def editor(r):
    pg = _page(r)
    OUT["label"] = pg.evaluate(LABEL)
    _click(pg, "document.getElementById('project-new-btn')")
    pg.wait_for_timeout(250)
    OUT["editor"] = {"modal": pg.evaluate(BOX, "#modal-box"), "focus": pg.evaluate("document.activeElement.id"),
                     "kbs": pg.evaluate("[...document.querySelectorAll('.pj-kb span')].map(s => s.textContent)"),
                     "imgs": pg.evaluate("document.querySelectorAll('#modal-box img, #modal-box b').length"),
                     "kb_box": pg.evaluate(BOX, ".pj-kb")}
    pg.keyboard.type("Support")
    _click(pg, "document.querySelector('.pj-kb input[data-kb=\"kb2\"]')")
    pg.select_option("#pj-docs", "off")
    _click(pg, "[...document.querySelectorAll('#modal-actions button')].find(b => b.textContent === 'Create')")
    pg.wait_for_timeout(300)
    OUT["created"] = {"post": pg.evaluate("window.__projCalls.filter(c => c.m === 'POST').map(c => c.body)"),
                      "filter": pg.evaluate("S.projectFilter"), "edit": pg.evaluate(BOX, "#project-edit-btn"),
                      "modal_open": pg.evaluate("document.getElementById('modal-overlay').classList.contains('open')"),
                      "current": _state(pg)}
    # edit the first project, then delete it: its conversations stay, without a project
    pg.select_option("#project-filter", P1["id"])
    pg.wait_for_timeout(150)
    _click(pg, "document.getElementById('project-edit-btn')")
    pg.wait_for_timeout(250)
    OUT["edit_values"] = pg.evaluate("""() => ({name: document.getElementById('pj-name').value,
        kbs: [...document.querySelectorAll('.pj-kb input:checked')].map(x => x.dataset.kb),
        tpl: document.getElementById('pj-template').value, docs: document.getElementById('pj-docs').value})""")
    _click(pg, "[...document.querySelectorAll('#modal-actions button')].find(b => b.textContent === 'Delete project')")
    pg.wait_for_timeout(250)
    OUT["delete_confirm"] = {"title": pg.evaluate("document.getElementById('modal-title').textContent"),
                             "body": pg.evaluate("document.getElementById('modal-body').innerText"),
                             "focus": pg.evaluate("document.activeElement.textContent")}
    _click(pg, "[...document.querySelectorAll('#modal-actions button')].find(b => b.textContent === 'Delete project')")
    pg.wait_for_timeout(350)
    OUT["deleted"] = {"calls": pg.evaluate("window.__projCalls.filter(c => c.m === 'DELETE').map(c => c.u)"),
                      "filter": pg.evaluate("S.projectFilter"), "rows": pg.evaluate(ROWS),
                      "s_a_project": pg.evaluate("S.sessions.s_a.project || null"),
                      "options": pg.evaluate("[...document.querySelectorAll('#project-filter option')].map(o => o.value)"),
                      "toasts": ux._toasts(pg)}
    # Settings → RAG: advice for one project's knowledge bases
    pg.evaluate("openSettings('rag')")
    pg.wait_for_timeout(300)
    OUT["adv_row"] = pg.evaluate(BOX, "#adv-scope-row")
    pg.select_option("#adv-scope", P2["id"])
    pg.wait_for_timeout(250)
    OUT["adv_calls"] = pg.evaluate("window.__projCalls.filter(c => c.u.startsWith('/api/rag/advice')).map(c => c.u)")
    pg.context.close()
    # someone who is not an administrator sees the switcher, not the controls
    pg = _page(r, admin=False)
    OUT["user_bar"] = {"new": pg.evaluate(BOX, "#project-new-btn"), "filter": pg.evaluate(BOX, "#project-filter")}
    pg.context.close()


def _menu(pg):
    return pg.evaluate("""() => { const m = document.getElementById('mention-menu'), ta = document.getElementById('msg-input');
        const b = m.getBoundingClientRect(), t = ta.getBoundingClientRect(), cs = getComputedStyle(m);
        const opts = [...m.querySelectorAll('.mention-opt')];
        return {hidden: m.hidden, display: cs.display, x: Math.round(b.left), y: Math.round(b.top), w: Math.round(b.width),
                h: Math.round(b.height), right: Math.round(b.right), bottom: Math.round(b.bottom), taTop: Math.round(t.top),
                opts: opts.map(o => o.querySelector('.mention-name').textContent),
                selected: opts.findIndex(o => o.getAttribute('aria-selected') === 'true'),
                optH: opts.length ? Math.round(opts[0].getBoundingClientRect().height) : 0,
                selBg: opts.length ? getComputedStyle(opts[Math.max(0, opts.findIndex(o => o.getAttribute('aria-selected') === 'true'))]).backgroundColor : null,
                active: ta.getAttribute('aria-activedescendant'), markup: m.querySelectorAll('b, img').length,
                vw: innerWidth, value: ta.value, focus: document.activeElement === ta}; }""")


CHIPS = """() => [...document.querySelectorAll('#scope-row .mention-chip')].map(c => { const b = c.getBoundingClientRect();
    return {text: c.querySelector('strong').textContent, w: Math.round(b.width), h: Math.round(b.height),
            x: Math.round(b.left), right: Math.round(b.right), display: getComputedStyle(c).display}; })"""


def mentions(r):
    pg = _page(r)
    pg.click("#msg-input")
    pg.keyboard.type("compare #")
    pg.wait_for_timeout(100)
    OUT["menu_all"] = _menu(pg)
    OUT["menu_hit"] = _hit(pg, "document.querySelector('#mention-menu .mention-opt')")
    pg.keyboard.type("k")
    pg.wait_for_timeout(80)
    OUT["menu_k"] = _menu(pg)
    pg.keyboard.press("ArrowDown")
    OUT["menu_down"] = _menu(pg)
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(100)
    OUT["after_enter"] = {"menu": _menu(pg), "chips": pg.evaluate(CHIPS), "mentions": pg.evaluate("S.mentions.slice()")}
    # Escape closes the menu only (no other layer, the text stays)
    pg.keyboard.type("#")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(80)
    OUT["after_escape"] = _menu(pg)
    pg.keyboard.press("Backspace")
    # the escaped name, picked with the mouse
    pg.keyboard.type(" and #my")
    pg.wait_for_timeout(80)
    OUT["menu_my"] = _menu(pg)
    _click(pg, "document.querySelector('#mention-menu .mention-opt')")
    OUT["after_click"] = {"chips": pg.evaluate(CHIPS), "focus": pg.evaluate("document.activeElement.id"),
                          "markup": pg.evaluate("document.querySelectorAll('#scope-row b, #scope-row img').length")}
    r.shot(pg, "projects_mentions")
    # with Documents only: the question searches the two mentioned, in that mode
    _click(pg, "document.getElementById('docs-only-btn')")
    pg.click("#msg-input")
    pg.keyboard.press("End")
    pg.keyboard.type(" please")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(250)
    OUT["sent_mention"] = pg.evaluate(SENT)[-1:]
    OUT["after_send"] = {"chips": pg.evaluate(CHIPS), "sel": pg.evaluate("_kbSelection()"),
                         "bubble_tags": pg.evaluate("[...document.querySelectorAll('.msg-wrap.user .mention-tag')].map(t => t.textContent)")}
    # remove a chip with its ✕ (mouse); the question then searches the selection again
    pg.click("#msg-input")
    pg.keyboard.type("#kb1")
    pg.keyboard.press("Tab")
    _click(pg, "document.querySelector('#scope-row .mention-chip .scope-chip-x')")
    OUT["after_remove"] = pg.evaluate("S.mentions.slice()")
    pg.context.close()


def mobile(r):
    kw = {"has_touch": True} if r.engine == "firefox" else {"has_touch": True, "is_mobile": True}
    pg = _page(r, width=375, height=812, ctx_kw=kw)
    pg.tap("#toggle-sidebar")
    pg.wait_for_timeout(300)
    OUT["m_bar"] = {k: pg.evaluate(BOX, s) for k, s in (("bar", "#project-bar"), ("filter", "#project-filter"),
                                                         ("new", "#project-new-btn"), ("sidebar", "#sidebar"))}
    OUT["m_overflow"] = pg.evaluate("[document.documentElement.scrollWidth - document.documentElement.clientWidth,"
                                    " (() => { const b = document.getElementById('project-bar'); return b.scrollWidth - b.clientWidth; })()]")
    pg.select_option("#project-filter", P1["id"])
    pg.wait_for_timeout(200)
    OUT["m_edit"] = pg.evaluate(BOX, "#project-edit-btn")
    OUT["m_rows"] = pg.evaluate(ROWS)
    pg.tap(".new-chat-btn")                      # a new chat in the project closes the overlay
    pg.wait_for_timeout(300)
    OUT["m_new"] = _state(pg)
    OUT["m_sidebar_after"] = pg.evaluate("document.getElementById('sidebar').classList.contains('open')")
    pg.tap("#msg-input")
    pg.keyboard.type("#")
    pg.wait_for_timeout(120)
    OUT["m_menu"] = _menu(pg)
    OUT["m_hit"] = _hit(pg, "document.querySelectorAll('#mention-menu .mention-opt')[1]")
    b = pg.evaluate("() => { const r = document.querySelectorAll('#mention-menu .mention-opt')[1].getBoundingClientRect(); return [r.left + r.width/2, r.top + r.height/2]; }")
    pg.touchscreen.tap(*b)
    pg.wait_for_timeout(150)
    OUT["m_chips"] = pg.evaluate(CHIPS)
    OUT["m_scope"] = pg.evaluate(BOX, "#scope-row")
    OUT["m_overflow_after"] = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    r.shot(pg, "projects_mobile")
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
        for step in (empty, switcher, editor, mentions, mobile):
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
