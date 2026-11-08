# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second review of 1.16.0, measured in a real browser with canned API answers and a fake
WebSocket (uxfix_probe's harness, no server). Prints one JSON object.

  * a Documents only follow-up answered from the previous answer's sources says so in its
    source list and badge;
  * an answer citing numbers when nothing was retrieved is badged "Cites missing sources",
    and a made-up "[9]" with 3 sources is drawn as a broken citation;
  * until /api/me has answered (it fails, hangs or answers 5xx) no administrator control is
    shown; a single-user install gets them once a retry is answered.

    python rereview_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys

import ux_fixes_probe as U
import uxfix_probe as X
from ux_fixes_probe import CONTRAST, _wait

OUT: dict = {}

# Outermost fetch wrapper: /api/me as the variant says (meMode), counting the requests.
ME = r"""
(() => {
  const inner = window.fetch;
  window.__meCalls = 0;
  window.fetch = (url, opt={}) => {
    const u = String(url), V = window.__UXV;
    if (u !== '/api/me' || !V.meMode) return inner(url, opt);
    window.__meCalls++;
    const json = (b, s=200) => Promise.resolve(new Response(JSON.stringify(b), {status:s, headers:{'Content-Type':'application/json'}}));
    const m = V.meMode;
    if (m === 'hang') return new Promise(() => {});
    if (m === '503') return json({detail:'Sign-in is misconfigured on the server; see its log.'}, 503);
    if (m === 'html') return Promise.resolve(new Response('<html>login</html>', {status:200, headers:{'Content-Type':'text/html'}}));
    if (m === 'failThenOk') {
      if (window.__meCalls <= (V.meFails || 2)) return Promise.reject(new TypeError('Failed to fetch'));
      return json({auth:false, admin:true});
    }
    if (m === 'user') return json({auth:true, user:'bob', admin:false});
    return json({auth:false, admin:true});
  };
})();
"""


def _page(r, width=1400, height=900, scheme="light", wait_ready=True, **over):
    v = U._variant(**over)
    ctx = r.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(v) + ";")
    pg.add_init_script(U.FAKE)
    pg.add_init_script(X.EXTRA)
    pg.add_init_script(ME)
    pg.goto(U.INDEX.as_uri(), wait_until="load")
    if wait_ready:
        _wait(pg, "() => typeof S === 'object' && S.modelsLoaded && Array.isArray(S.ragInstances)", 10)
        _wait(pg, "() => S.ws && S.ws.readyState === 1", 4)
    return pg


BOX = r"""(sel) => { const el = document.querySelector(sel); if (!el) return null;
  const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
  return {display: cs.display, visibility: cs.visibility, w: Math.round(r.width), h: Math.round(r.height),
          left: Math.round(r.left), right: Math.round(r.right), text: el.textContent.trim().slice(0, 80)}; }"""


# ── M1: reused sources are labelled ──────────────────────────────────────────
def reused(r):
    msgs = [X._u("how long is the warranty?"),
            X._a("The warranty lasts two years [1].", chunks=[X.A], rag_used=True, docs_only=True, kbs=["default"]),
            X._u("shorten that"),
            X._a("Two years [1].", chunks=[X.A], rag_used=True, docs_only=True, kbs=["default"], reused_sources=True)]
    for w in (1400, 375):
        pg = _page(r, width=w, height=900, sessions=[X._row("s_r", "Reused", messages=4)],
                   mine=["s_r"], sessionMsgs={"s_r": msgs})
        if w == 1400:
            X._open(pg, "s_r", 2)
        else:
            pg.evaluate("openSession('s_r')")
            _wait(pg, "() => document.querySelectorAll('#chat-area .rag-inspector').length >= 2", 5)
            pg.wait_for_timeout(250)
        OUT[f"reused_{w}"] = pg.evaluate(r"""() => [...document.querySelectorAll('#chat-area .msg-wrap:not(.user)')].map(w => {
          const h = w.querySelector('.rag-inspector-header'), s = h && h.querySelector('strong'), b = w.querySelector('.ground-badge');
          const hr = h ? h.getBoundingClientRect() : null, sr = s ? s.getBoundingClientRect() : null;
          return {header: s ? s.textContent : null, title: h ? h.getAttribute('title') : null,
                  icon: h ? h.querySelector('span').textContent : null,
                  display: h ? getComputedStyle(h).display : null, hw: hr ? Math.round(hr.width) : 0,
                  sw: sr ? Math.round(sr.width) : 0, sright: sr ? Math.round(sr.right) : 0,
                  hright: hr ? Math.round(hr.right) : 0, badgeAria: b ? b.getAttribute('aria-label') : null}; })""")
        OUT[f"reused_overflow_{w}"] = pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        OUT[f"reused_vw_{w}"] = pg.evaluate("document.documentElement.clientWidth")
        if w == 1400:
            # a real click on the reused list's header opens it: the source is there
            hdr = pg.locator("#chat-area .msg-wrap:not(.user)").nth(1).locator(".rag-inspector-header")
            hdr.click()
            pg.wait_for_timeout(200)
            OUT["reused_open"] = pg.evaluate(r"""() => { const i = [...document.querySelectorAll('#chat-area .rag-inspector')][1];
              const b = i.querySelector('.rag-inspector-body'); const r = b.getBoundingClientRect();
              return {open: i.classList.contains('open'), bodyH: Math.round(r.height), display: getComputedStyle(b).display,
                      src: i.textContent.includes('a.html')}; }""")
            r.shot(pg, "reused")
        pg.context.close()


# ── minor 7: citations with nothing retrieved; a made-up [9] ─────────────────
def missing(r):
    msgs = [X._u("q"), X._a("Streams keep their entries in insertion order [1] and trim them [2].", rag_used=True),
            X._u("q2"), X._a("Nothing in the knowledge base matched, so this is a general answer.", rag_used=True),
            X._u("q3"), X._a("Streams keep their entries in insertion order [1] and they scale [9].",
                             chunks=[X.A, dict(X.C, n=2), dict(X.B, n=3)], rag_used=True)]
    for scheme in ("light", "dark"):
        pg = _page(r, scheme=scheme, theme=scheme, sessions=[X._row("s_m", "Missing", messages=6)],
                   mine=["s_m"], sessionMsgs={"s_m": msgs})
        if scheme == "dark":
            pg.evaluate("applyTheme('dark')")
        X._open(pg, "s_m", 3)
        got = pg.evaluate(f"(() => {{ const contrast = {X.CONTRAST_OP}; return ({X.BADGES})(contrast); }})()")
        OUT[f"missing_badges_{scheme}"] = got
        OUT[f"missing_broken_{scheme}"] = pg.evaluate(r"""() => [...document.querySelectorAll('#chat-area .msg-wrap:not(.user)')].map(w =>
          [...w.querySelectorAll('.msg-bubble .cite-broken')].map(e => { const r = e.getBoundingClientRect();
            return {text: e.textContent, w: Math.round(r.width), h: Math.round(r.height), deco: getComputedStyle(e).textDecorationLine}; }))""")
        r.shot(pg, f"missing_{scheme}")
        pg.context.close()
    pg = _page(r, width=375, height=800, sessions=[X._row("s_m", "Missing", messages=6)], mine=["s_m"],
               sessionMsgs={"s_m": msgs})
    pg.evaluate("openSession('s_m')")
    _wait(pg, "() => document.querySelectorAll('#chat-area .ground-badge').length >= 3", 5)
    OUT["missing_375"] = pg.evaluate(r"""() => ({vw: document.documentElement.clientWidth,
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        badges: [...document.querySelectorAll('#chat-area .ground-badge')].map(b => { const r = b.getBoundingClientRect();
          return {text: b.textContent, left: Math.round(r.left), right: Math.round(r.right)}; })})""")
    pg.context.close()


# ── minor 9: no administrator control while sign-in is unknown ──────────────
ADMIN = "#project-new-btn"


def unknown_me(r):
    # /api/me never answers: the first frame and every one after it hides them
    pg = _page(r, wait_ready=False, meMode="hang")
    pg.wait_for_timeout(1500)
    OUT["me_hang"] = {"btn": pg.evaluate(BOX, ADMIN), "me": pg.evaluate("S.me"),
                      "cls": pg.evaluate("document.documentElement.className")}
    pg.context.close()
    # 503 (sign-in misconfigured): hidden, and asked again
    pg = _page(r, wait_ready=False, meMode="503")
    pg.wait_for_timeout(3500)
    OUT["me_503"] = {"btn": pg.evaluate(BOX, ADMIN), "me": pg.evaluate("S.me"), "calls": pg.evaluate("window.__meCalls")}
    pg.context.close()
    # a proxy's login page instead of JSON: hidden
    pg = _page(r, wait_ready=False, meMode="html")
    pg.wait_for_timeout(1500)
    OUT["me_html"] = {"btn": pg.evaluate(BOX, ADMIN), "me": pg.evaluate("S.me")}
    pg.context.close()
    # single-user install, /api/me fails twice and then answers: hidden, then shown
    pg = _page(r, wait_ready=False, meMode="failThenOk", meFails=2)
    pg.wait_for_timeout(400)
    OUT["me_retry_before"] = {"btn": pg.evaluate(BOX, ADMIN), "calls": pg.evaluate("window.__meCalls")}
    _wait(pg, "() => S.me && S.me.admin === true", 8)
    pg.wait_for_timeout(200)
    OUT["me_retry_after"] = {"btn": pg.evaluate(BOX, ADMIN), "calls": pg.evaluate("window.__meCalls"),
                             "me": pg.evaluate("S.me")}
    # the control works with a real click once shown
    pg.locator(ADMIN).click()
    pg.wait_for_timeout(250)
    OUT["me_retry_click"] = pg.evaluate("""() => { const d = [...document.querySelectorAll('.modal-overlay.open, [role=dialog]')]
        .filter(e => getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().height > 0); return d.length; }""")
    pg.context.close()
    # sign-in off answering at once, and a signed-in non-administrator
    for mode in ("off", "user"):
        pg = _page(r, meMode=mode)
        pg.wait_for_timeout(300)
        OUT[f"me_{mode}"] = {"btn": pg.evaluate(BOX, ADMIN), "me": pg.evaluate("S.me"), "calls": pg.evaluate("window.__meCalls")}
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
        for step in (reused, missing, unknown_me):
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
