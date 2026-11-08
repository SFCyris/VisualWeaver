# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sign-in in a real browser with canned API answers (ux_fixes_probe's fake fetch and
WebSocket, no server): who is signed in, and what someone who is not an administrator
is offered. Prints one JSON object of measured values: computed display, bounding
boxes, text, the requests the page sent.

    python signin_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys
import time

import ux_fixes_probe as ux

OUT: dict = {}
NOW = int(time.time())
XSS_NAME = "<img src=x onerror=window.__xss=1> Bob Builder-Longname-From-Engineering"

# /api/me and a non-empty usage tally (so the token card with its Reset button renders).
ME = r"""
(() => {
  const inner = window.fetch;
  window.fetch = (url, opt={}) => {
    const u = String(url), V = window.__UXV;
    const json = (b, s=200) => Promise.resolve(new Response(JSON.stringify(b), {status:s, headers:{'Content-Type':'application/json'}}));
    if (u === '/api/me') return V.me === 401 ? json({detail:'Sign-in required'}, 401) : json(V.me || {});
    if (u === '/api/usage' && (opt.method||'GET') === 'GET') return json({'ollama:llama3:latest': {in: 1200, out: 340}});
    return inner(url, opt);
  };
})();
"""

TABS = r"""() => [...document.querySelectorAll('.settings-tabs .stab, .settings-tabs .stab-sep')].map(t => ({
  tab: t.dataset.tab || 'sep', display: getComputedStyle(t).display,
  w: Math.round(t.getBoundingClientRect().width), active: t.classList.contains('active')}))"""

SIGNED = r"""() => { const el = document.getElementById('signed-in'), n = el.querySelector('.signed-in-name'),
  role = el.querySelector('.signed-in-role'), sb = document.getElementById('sidebar').getBoundingClientRect();
  const b = el.getBoundingClientRect(), nb = n.getBoundingClientRect(), rb = role.getBoundingClientRect();
  return {display: getComputedStyle(el).display, hidden: el.hidden, x: Math.round(b.left), right: Math.round(b.right),
          w: Math.round(b.width), h: Math.round(b.height), top: Math.round(b.top), bottom: Math.round(b.bottom),
          name: n.textContent, nameW: Math.round(nb.width), nameRight: Math.round(nb.right),
          nameClipped: n.scrollWidth > n.clientWidth, nameOverflow: getComputedStyle(n).textOverflow,
          role: role.textContent, roleDisplay: getComputedStyle(role).display, roleW: Math.round(rb.width),
          title: el.title, imgs: el.querySelectorAll('img').length, xss: window.__xss === 1,
          sbLeft: Math.round(sb.left), sbRight: Math.round(sb.right), sbBottom: Math.round(sb.bottom),
          vw: innerWidth, vh: innerHeight, docOverflow: document.documentElement.scrollWidth - innerWidth}; }"""


def _sessions():
    def row(sid, title, updated):
        return {"id": sid, "messages": 2, "preview": "", "title": title, "updated": updated,
                "created": updated - 60, "model": "llama3:latest", "kbs": ["manuals"],
                "pinned": False, "expires": None}
    return [row("s_one", "First chat", NOW - 3600), row("s_two", "Second chat", NOW - 7200)]


def _page(r, me, width=1400, height=900, ctx_kw=None):
    inst = [{"name": "manuals", "chunks": 3, "color": "#7c6ff7", "redis_endpoint": "default",
             "reachable": True, "enabled": True}]          # no "default": an admin's page creates it
    v = ux._variant(me=me, inst=inst, sessions=_sessions(), mine=[],
                    ragStats=[{"name": "manuals", "queries": 4, "hits": 3, "misses": 1, "hit_rate": 0.75,
                               "avg_top_score": 0.8, "avg_best_raw_score": 0.7, "avg_chunks": 3}],
                    sessionMsgs={s["id"]: [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
                                 for s in _sessions()})
    ctx = r.browser.new_context(viewport={"width": width, "height": height}, **(ctx_kw or {}))
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(v) + ";")
    pg.add_init_script(ux.FAKE)
    pg.add_init_script(ME)
    pg.goto(ux.INDEX.as_uri(), wait_until="load")
    ux._wait(pg, "() => typeof S === 'object' && S.modelsLoaded && S.ragInstances.length > 0", 10)
    pg.wait_for_timeout(300)
    return pg


def _settings_from_sidebar(pg):
    pg.locator("#sidebar-footer .sidebar-btn", has_text="Settings").click()
    pg.wait_for_timeout(250)


def _view(r, key, me):
    pg = _page(r, me)
    o = OUT[key] = {}
    o["signed"] = pg.evaluate(SIGNED)
    o["listed"] = pg.evaluate("() => [...document.querySelectorAll('#sessions-list .session-row')].map(r => r.dataset.sid)")
    o["posts"] = pg.evaluate("() => window.__posts.map(p => p.method + ' ' + p.url)")
    _settings_from_sidebar(pg)
    o["tabs"] = pg.evaluate(TABS)
    o["footer"] = pg.evaluate(ux.BOX, ".settings-footer")
    # keyboard: the arrows move among the tabs on offer only
    pg.focus(".settings-tabs .stab.active")
    seen = []
    for _ in range(4):
        pg.keyboard.press("ArrowRight")
        pg.wait_for_timeout(60)
        seen.append(pg.evaluate("document.activeElement.dataset.tab || ''"))
    o["arrow_walk"] = seen
    # the Analytics reset buttons
    pg.locator("#stab-analytics").click()
    ux._wait(pg, "() => document.querySelector('[onclick^=\"resetTokenUsage\"]') && document.querySelector('[onclick^=\"resetRagStats\"]')", 6)
    o["reset_btns"] = pg.evaluate("""() => ['resetTokenUsage', 'resetRagStats'].map(f => {
        const b = document.querySelector(`[onclick^="${f}"]`); return b ? getComputedStyle(b).display : 'absent'; })""")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(200)
    # Ctrl/⌘+K opens Settings on its first tab for this user
    pg.keyboard.press("Control+k")
    pg.wait_for_timeout(250)
    o["ctrl_k_tab"] = pg.evaluate("document.querySelector('.settings-tabs .stab.active')?.dataset.tab")
    r.shot(pg, f"signin_{key}")
    pg.context.close()


def views(r):
    _view(r, "user", {"auth": True, "user": XSS_NAME, "id": "bob", "admin": False})
    _view(r, "admin", {"auth": True, "user": "Root", "id": "root", "admin": True})
    _view(r, "off", {"auth": False, "user": None, "id": None, "admin": True})


def mobile(r):
    pg = _page(r, {"auth": True, "user": XSS_NAME, "id": "bob", "admin": False}, width=375, height=812,
               ctx_kw={"has_touch": True, "is_mobile": True})
    pg.tap("#toggle-sidebar")
    pg.wait_for_timeout(300)
    OUT["m_signed"] = pg.evaluate(SIGNED)
    # the footer scrolls into view inside the sidebar if it does not fit
    pg.evaluate("document.getElementById('signed-in').scrollIntoView({block:'nearest'})")
    pg.wait_for_timeout(100)
    OUT["m_signed_scrolled"] = pg.evaluate(SIGNED)
    r.shot(pg, "signin_mobile")
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
        for step in (views, mobile):
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
