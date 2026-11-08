# SPDX-License-Identifier: AGPL-3.0-or-later
"""The organised conversation sidebar — date groups, pins, rename, "expires in …" and the
retention setting — in a real browser with canned API answers (ux_fixes_probe's fake
fetch and WebSocket, no server). Prints one JSON object of measured values: computed
display, bounding boxes, request bodies, focus.

    python history_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys
import time

import ux_fixes_probe as ux

OUT: dict = {}
NOW = int(time.time())
H, D = 3600, 86400

# The fake server's answer to PATCH /api/sessions/{sid}: the stored row with the change
# applied, the way sessions.update_session answers (a pin clears the expiry).
PATCH = r"""
(() => {
  const inner = window.fetch;
  window.__patches = [];
  window.fetch = (url, opt={}) => {
    const u = String(url), m = (opt.method||'GET').toUpperCase();
    if (m === 'PATCH' && u.startsWith('/api/sessions/')) {
      const V = window.__UXV, id = decodeURIComponent(u.split('/').pop());
      const body = JSON.parse(opt.body);
      window.__patches.push({id, body});
      if (V.patchFail) return Promise.resolve(new Response(JSON.stringify({detail:'store down'}),
                                              {status:503, headers:{'Content-Type':'application/json'}}));
      const row = (V.sessions||[]).find(r => r.id === id) || {id, messages:2};
      if ('title' in body) row.title = body.title;
      if ('pinned' in body) { row.pinned = body.pinned;
        row.expires = body.pinned ? null : (V.ttl ? Math.round(Date.now()/1000) + V.ttl : null); }
      return Promise.resolve(new Response(JSON.stringify(row), {status:200, headers:{'Content-Type':'application/json'}}));
    }
    return inner(url, opt);
  };
})();
"""

ROWS = r"""() => [...document.querySelectorAll('#sessions-list > *')].map(el => el.classList.contains('session-group')
  ? 'G:' + el.textContent.trim()
  : el.dataset.sid + '|' + (el.querySelector('.session-title')?.innerText || '') + '|' + (el.querySelector('.session-exp')?.innerText || ''))"""

ACT = r"""(sid) => { const row = [...document.querySelectorAll('#sessions-list .session-row')].find(r => r.dataset.sid === sid);
  const rb = row.getBoundingClientRect(), sb = document.getElementById('sidebar').getBoundingClientRect();
  const t = row.querySelector('.session-title, .session-rename').getBoundingClientRect();
  return {row: {x: Math.round(rb.left), w: Math.round(rb.width), right: Math.round(rb.right), h: Math.round(rb.height)},
          sidebarRight: Math.round(sb.right), titleW: Math.round(t.width),
          acts: [...row.querySelectorAll('.session-act, .session-del')].map(b => { const r = b.getBoundingClientRect(), cs = getComputedStyle(b);
            return {label: b.getAttribute('aria-label').split(':')[0], display: cs.display, opacity: +cs.opacity,
                    x: Math.round(r.left), right: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height)}; })}; }"""


def _sessions():
    def row(sid, title, updated, **kw):
        return {"id": sid, "messages": 2, "preview": "", "title": title, "updated": updated,
                "created": updated - 60, "model": "llama3:latest", "kbs": ["default"],
                "pinned": False, "expires": None, **kw}
    return [row("s_today", "Today chat", NOW - H, expires=NOW + 20 * H),
            row("s_yday", "Yesterday chat", NOW - D - 2 * H, expires=NOW + 22 * H),
            row("s_week", "Week chat", NOW - 3 * D, expires=NOW + 40 * 60),
            row("s_month", "Month chat", NOW - 20 * D),
            row("s_old", "Old chat", NOW - 100 * D),
            row("s_pin", "Pinned chat", NOW - 10 * D, pinned=True),
            row("s_xss", "<img src=x onerror=window.__xss=1>", NOW - 2 * H, expires=NOW + 3 * D)]


def _page(r, width=1400, height=900, ctx_kw=None, **over):
    cfg = json.loads(json.dumps(ux.CONFIG))
    cfg["sessions"] = {"persist": True, "ttl": over.pop("ttl_cfg", D)}
    sessions = _sessions()
    v = ux._variant(config=cfg, sessions=sessions, mine=[s["id"] for s in sessions], ttl=D,
                    sessionMsgs={s["id"]: [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
                                 for s in sessions}, **over)
    ctx = r.browser.new_context(viewport={"width": width, "height": height}, **(ctx_kw or {}))
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(v) + ";")
    pg.add_init_script(ux.FAKE)
    pg.add_init_script(PATCH)
    pg.goto(ux.INDEX.as_uri(), wait_until="load")
    ux._wait(pg, "() => typeof S === 'object' && S.modelsLoaded && document.querySelectorAll('#sessions-list .session-row').length >= 7", 10)
    pg.wait_for_timeout(200)
    return pg


def _center(pg, selector_js):
    box = pg.evaluate(f"() => {{ const b = ({selector_js}).getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; }}")
    return box


def groups_and_expiry(r):
    pg = _page(r)
    OUT["rows"] = pg.evaluate(ROWS)
    OUT["exp_box"] = pg.evaluate("""() => { const e = [...document.querySelectorAll('#sessions-list .session-row')]
        .find(r => r.dataset.sid === 's_today').querySelector('.session-exp'); const b = e.getBoundingClientRect(), cs = getComputedStyle(e);
        return {display: cs.display, h: Math.round(b.height), w: Math.round(b.width), font: cs.fontSize, title: e.title}; }""")
    OUT["group_box"] = pg.evaluate("""() => { const g = document.querySelector('#sessions-list .session-group'); const b = g.getBoundingClientRect();
        return {display: getComputedStyle(g).display, h: Math.round(b.height), text: g.innerText}; }""")
    # the open conversation (a new, empty one) offers no rename/pin; a stored one does on hover
    OUT["acts_idle"] = pg.evaluate(ACT, "s_month")
    x, y = _center(pg, "[...document.querySelectorAll('#sessions-list .session-row')].find(r => r.dataset.sid === 's_month')")
    pg.mouse.move(x, y)
    pg.wait_for_timeout(150)
    OUT["acts_hover"] = pg.evaluate(ACT, "s_month")
    OUT["acts_pinned_idle"] = pg.evaluate(ACT, "s_pin")
    OUT["new_conv_acts"] = pg.evaluate("""() => document.querySelector('#sessions-list .session-row.active').querySelectorAll('.session-act').length""")
    r.shot(pg, "history_groups")
    pg.context.close()


def rename(r):
    pg = _page(r)
    row = "[...document.querySelectorAll('#sessions-list .session-row')].find(r => r.dataset.sid === 's_week')"
    pg.mouse.move(*_center(pg, row))
    pg.wait_for_timeout(100)
    pg.mouse.click(*_center(pg, row + ".querySelector('.session-act')"))
    pg.wait_for_timeout(100)
    OUT["rename_focus"] = pg.evaluate("""() => ({cls: document.activeElement.className, value: document.activeElement.value,
        selected: document.activeElement.selectionEnd - document.activeElement.selectionStart,
        w: Math.round(document.activeElement.getBoundingClientRect().width)})""")
    pg.keyboard.type("Boiler  notes and more")              # spaces must reach the box
    OUT["rename_typed"] = pg.evaluate("document.activeElement.value")
    OUT["rename_session_before_enter"] = pg.evaluate("S.sessionId")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(200)
    OUT["rename_patch"] = pg.evaluate("window.__patches.slice()")
    OUT["rename_rows"] = pg.evaluate(ROWS)
    OUT["rename_session_after"] = pg.evaluate("S.sessionId")
    # Esc cancels: nothing sent, the old name stays, no panel reacts
    pg.mouse.move(*_center(pg, row))
    pg.mouse.click(*_center(pg, row + ".querySelector('.session-act')"))
    pg.wait_for_timeout(100)
    pg.keyboard.type("discard me")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(150)
    OUT["rename_cancel"] = {"patches": pg.evaluate("window.__patches.length"),
                            "title": pg.evaluate(f"({row}).querySelector('.session-title').innerText"),
                            "input_left": pg.evaluate("!!document.querySelector('.session-rename')")}
    # a list refresh while typing does not throw the typed name away
    pg.mouse.move(*_center(pg, row))
    pg.mouse.click(*_center(pg, row + ".querySelector('.session-act')"))
    pg.keyboard.type("Kept")
    pg.evaluate("renderSessionList()")
    OUT["rename_survives_render"] = pg.evaluate("document.querySelector('.session-rename')?.value")
    pg.keyboard.press("Escape")
    # the XSS title: shown as text in the row and in the box, never as markup
    xrow = "[...document.querySelectorAll('#sessions-list .session-row')].find(r => r.dataset.sid === 's_xss')"
    pg.mouse.move(*_center(pg, xrow))
    pg.mouse.click(*_center(pg, xrow + ".querySelector('.session-act')"))
    OUT["xss"] = pg.evaluate("""() => ({value: document.querySelector('.session-rename')?.value,
        fired: window.__xss || 0, imgs: document.querySelectorAll('#sessions-list img').length})""")
    pg.keyboard.press("Escape")
    pg.context.close()


def pin(r):
    pg = _page(r)
    row = "[...document.querySelectorAll('#sessions-list .session-row')].find(r => r.dataset.sid === 's_today')"
    pg.mouse.move(*_center(pg, row))
    pg.wait_for_timeout(100)
    pg.mouse.click(*_center(pg, row + ".querySelectorAll('.session-act')[1]"))
    pg.wait_for_timeout(200)
    OUT["pin_patch"] = pg.evaluate("window.__patches.slice()")
    OUT["pin_rows"] = pg.evaluate(ROWS)
    OUT["pin_btn"] = pg.evaluate(f"""() => {{ const b = ({row}).querySelectorAll('.session-act')[1];
        return {{pressed: b.getAttribute('aria-pressed'), on: b.classList.contains('on'), color: getComputedStyle(b).color,
                display: getComputedStyle(b).display}}; }}""")
    # unpin from the Pinned group
    pg.mouse.move(*_center(pg, row))
    pg.mouse.click(*_center(pg, row + ".querySelectorAll('.session-act')[1]"))
    pg.wait_for_timeout(200)
    OUT["unpin_rows"] = pg.evaluate(ROWS)
    pg.context.close()
    # a refused update says so and changes nothing
    pg = _page(r, patchFail=True)
    pg.mouse.move(*_center(pg, row))
    pg.mouse.click(*_center(pg, row + ".querySelectorAll('.session-act')[1]"))
    pg.wait_for_timeout(250)
    OUT["pin_fail"] = {"toasts": ux._toasts(pg), "rows": pg.evaluate(ROWS)[:3]}
    pg.context.close()


def settings(r):
    pg = _page(r)
    pg.evaluate("openSettings('general')")
    pg.wait_for_timeout(200)
    OUT["retention_select"] = pg.evaluate("""() => { const s = document.getElementById('s-session-retention'), b = s.getBoundingClientRect();
        return {value: s.value, options: [...s.options].map(o => o.textContent), display: getComputedStyle(s).display,
                w: Math.round(b.width), h: Math.round(b.height)}; }""")
    pg.select_option("#s-session-retention", "0")
    pg.evaluate("async () => { await saveSettings(); }")
    pg.wait_for_timeout(200)
    post = pg.evaluate("window.__posts.filter(p => p.url === '/api/config').pop()?.body")
    OUT["retention_post"] = post and post.get("sessions")
    OUT["sessions_reloaded"] = pg.evaluate("window.__calls.filter(c => c === 'GET /api/sessions').length")
    pg.context.close()
    # a value set by hand that is not one of the choices is shown, and saved unchanged
    pg = _page(r, ttl_cfg=7 * D)
    pg.evaluate("openSettings('general')")
    pg.wait_for_timeout(200)
    OUT["retention_custom"] = pg.evaluate("""() => { const s = document.getElementById('s-session-retention');
        return {value: s.value, label: s.selectedOptions[0].textContent, dirty: _settingsDirty()}; }""")
    pg.evaluate("async () => { await saveSettings(); }")
    pg.wait_for_timeout(200)
    post = pg.evaluate("window.__posts.filter(p => p.url === '/api/config').pop()?.body")
    OUT["retention_custom_post"] = post and post.get("sessions")
    pg.context.close()


def mobile(r):
    # A phone: 375 px, touch, no hover (Firefox has no mobile emulation: touch only).
    kw = {"has_touch": True} if r.engine == "firefox" else {"has_touch": True, "is_mobile": True}
    pg = _page(r, width=375, height=812, ctx_kw=kw)
    OUT["m_hover_none"] = pg.evaluate("matchMedia('(hover:none)').matches")
    pg.tap("#toggle-sidebar")
    pg.wait_for_timeout(250)
    OUT["m_sidebar"] = pg.evaluate("""() => { const b = document.getElementById('sidebar').getBoundingClientRect();
        return {display: getComputedStyle(document.getElementById('sidebar')).display, w: Math.round(b.width), right: Math.round(b.right)}; }""")
    OUT["m_rows"] = pg.evaluate(ROWS)
    OUT["m_acts_other"] = pg.evaluate(ACT, "s_today")
    OUT["m_doc_overflow"] = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    OUT["m_list_overflow"] = pg.evaluate("(() => { const l = document.getElementById('sidebar-body'); return l.scrollWidth - l.clientWidth; })()")
    # tap a conversation: it opens (the overlay closes); reopened, its row offers ✎ and 📌
    row = "[...document.querySelectorAll('#sessions-list .session-row')].find(r => r.dataset.sid === 's_today')"
    pg.tap(f"#sessions-list [data-sid='s_today'] .session-title")
    pg.wait_for_timeout(300)
    OUT["m_after_open"] = pg.evaluate("[S.sessionId, getComputedStyle(document.getElementById('sidebar')).display]")
    pg.tap("#toggle-sidebar")
    pg.wait_for_timeout(250)
    OUT["m_acts_active"] = pg.evaluate(ACT, "s_today")
    x, y = _center(pg, row + ".querySelector('.session-act')")
    pg.touchscreen.tap(x, y)
    pg.wait_for_timeout(150)
    OUT["m_rename"] = pg.evaluate("""() => { const i = document.querySelector('.session-rename'); if (!i) return null;
        const b = i.getBoundingClientRect(); return {focused: document.activeElement === i, w: Math.round(b.width), right: Math.round(b.right)}; }""")
    OUT["m_sidebar_after"] = pg.evaluate("getComputedStyle(document.getElementById('sidebar')).display")
    r.shot(pg, "history_mobile")
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
        for step in (groups_and_expiry, rename, pin, settings, mobile):
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
