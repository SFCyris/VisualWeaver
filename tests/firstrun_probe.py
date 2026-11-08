# SPDX-License-Identifier: AGPL-3.0-or-later
"""The first-run checklist and the chat status strip, measured in a real browser with
canned API answers and a fake WebSocket (the harness of ux_fixes_probe.py). Prints one
JSON object.

Every value recorded is observed: a bounding box, a computed colour or display, the
element a real pointer click reached, the text a screen reader is handed.

    python firstrun_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys

import ux_fixes_probe as U
from ux_fixes_probe import BOX, CONTRAST, _toasts, _wait

OUT: dict = {}

# After FAKE (which clears localStorage): what this probe needs kept between loads.
KEEP = r"""
(() => { const V = window.__UXV || {};
  try { if (V.frHidden) localStorage.setItem('rr.firstrunHidden', '1'); } catch(e) {}
})();
"""

STRIP = r"""() => [...document.querySelectorAll('#health-strip .hs-item')].map(el => {
  const r = el.getBoundingClientRect(), ico = el.querySelector('.hs-ico');
  return {text: el.innerText.replace(/\s+/g, ' ').trim(), cls: el.className, tag: el.tagName,
          aria: el.getAttribute('aria-label') || el.textContent.replace(/\s+/g, ' ').trim(),
          x: Math.round(r.left), w: Math.round(r.width), h: Math.round(r.height), right: Math.round(r.right),
          display: getComputedStyle(el).display, icoColor: ico ? getComputedStyle(ico).color : null}; })"""

CHECK = r"""() => { const el = document.getElementById('welcome-checklist'); if (!el) return null;
  const r = el.getBoundingClientRect();
  return {hidden: el.hidden, display: getComputedStyle(el).display, w: Math.round(r.width), h: Math.round(r.height),
          x: Math.round(r.left), right: Math.round(r.right),
          rows: [...el.querySelectorAll('.fr-item')].map(li => ({cls: li.className,
             text: li.querySelector('.fr-text').innerText.replace(/\s+/g, ' ').trim(),
             tc: li.querySelector('.fr-text').textContent.replace(/\s+/g, ' ').trim(),
             sr: li.querySelector('.sr-only')?.textContent,
             btn: li.querySelector('button')?.innerText || null,
             icoColor: getComputedStyle(li.querySelector('.fr-ico')).color,
             btnRight: li.querySelector('button') ? Math.round(li.querySelector('button').getBoundingClientRect().right) : null}))}; }"""


def _variant(**over):
    v = U._variant(ollamaStatus={"ok": True, "models": 3},
                   inst=[{"name": "default", "chunks": 0, "color": "#7c6ff7", "redis_endpoint": "default",
                          "reachable": True, "enabled": True}])
    v.update(over)
    return v


def _page(r, variant, **kw):
    """ux_fixes_probe's Run.page plus KEEP, waiting for the checklist inputs to settle."""
    width, height, scheme = kw.get("width", 1400), kw.get("height", 900), kw.get("scheme", "light")
    ctx = r.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
    pg = ctx.new_page()
    pg.set_default_timeout(8000)
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(variant) + ";")
    pg.add_init_script(U.FAKE)
    pg.add_init_script(KEEP)
    pg.goto(U.INDEX.as_uri(), wait_until="load")
    _wait(pg, "() => typeof S === 'object' && S.modelsLoaded && Array.isArray(S.ragInstances) && S.providerStatus", 10)
    _wait(pg, "() => S.ws && S.ws.readyState === 1", 4)
    pg.wait_for_timeout(250)
    return pg


def fresh_install(r):
    pg = _page(r, _variant())
    OUT["fresh_checklist"] = pg.evaluate(CHECK)
    # What a screen reader is handed for each row (the accessibility tree, not innerText).
    OUT["fresh_aria"] = [pg.locator("#welcome-checklist .fr-item").nth(i).aria_snapshot() for i in range(3)]
    OUT["fresh_strip"] = pg.evaluate(STRIP)
    OUT["fresh_strip_box"] = pg.evaluate(BOX, "#health-strip")
    OUT["fresh_input_box"] = pg.evaluate(BOX, "#input-area")
    r.shot(pg, "fresh_1400")
    # The knowledge step, by a real click: Settings → RAG, target chosen, drop zone focused
    pg.locator("#welcome-checklist button", has_text="Add documents").click()
    _wait(pg, "() => document.activeElement && document.activeElement.id === 'ingest-drop'", 3)
    OUT["add_docs"] = pg.evaluate("""() => ({open: document.getElementById('settings-overlay').classList.contains('open'),
        tab: document.querySelector('.stab.active')?.dataset.tab, target: document.getElementById('ingest-target-rag').value,
        focus: document.activeElement?.id, dropTop: Math.round(document.getElementById('ingest-drop').getBoundingClientRect().top),
        dropBottom: Math.round(document.getElementById('ingest-drop').getBoundingClientRect().bottom), vh: innerHeight})""")
    # ...and files dropped there land in that instance; back in the chat the list is gone.
    pg.set_input_files("#file-input", files=[{"name": "a.md", "mimeType": "text/markdown", "buffer": b"# hello"}])
    _wait(pg, "() => S.ragInstances.some(i => i.name === 'default' && i.chunks > 0)", 5)
    pg.evaluate("closeSettings(true); newSession();")
    pg.wait_for_timeout(150)
    OUT["after_ingest"] = {"checklist": pg.evaluate(CHECK),
                           "posts": pg.evaluate("window.__calls.filter(c => /ingest\\/files/.test(c))")}
    pg.context.close()


def active_instance_target(r):
    inst = [{"name": "default", "chunks": 0, "color": "#7c6ff7", "redis_endpoint": "default", "reachable": True, "enabled": True},
            {"name": "handbook", "chunks": 0, "color": "#22aa66", "redis_endpoint": "default", "reachable": True, "enabled": True}]
    cfg = json.loads(json.dumps(U.CONFIG)); cfg["active_rag"] = "handbook"
    pg = _page(r, _variant(inst=inst, config=cfg))
    OUT["handbook_row"] = pg.evaluate(CHECK)["rows"][2]
    pg.locator("#welcome-checklist button", has_text="Add documents").click()
    _wait(pg, "() => document.activeElement && document.activeElement.id === 'ingest-drop'", 3)
    OUT["handbook_target"] = pg.evaluate("document.getElementById('ingest-target-rag').value")
    pg.context.close()


def no_model_and_dismiss(r):
    pg = _page(r, _variant(ollama=[]))
    OUT["nomodel_checklist"] = pg.evaluate(CHECK)
    OUT["nomodel_strip"] = pg.evaluate(STRIP)
    pg.locator("#welcome-checklist button", has_text="Set up Ollama").click()
    pg.wait_for_timeout(150)
    OUT["nomodel_fix"] = pg.evaluate("""() => ({open: document.getElementById('settings-overlay').classList.contains('open'),
        tab: document.querySelector('.stab.active')?.dataset.tab, card: document.getElementById('pc-ollama')?.classList.contains('open')})""")
    pg.evaluate("closeSettings(true)")
    pg.locator("#welcome-checklist .fr-hide").click()
    pg.wait_for_timeout(100)
    OUT["dismissed"] = {"checklist": pg.evaluate(CHECK), "stored": pg.evaluate("localStorage.getItem('rr.firstrunHidden')"),
                        "focus": pg.evaluate("document.activeElement?.id"),
                        "setupStillShown": pg.evaluate("!document.getElementById('welcome-setup').hidden")}
    pg.context.close()
    pg = _page(r, _variant(ollama=[], frHidden=True))
    OUT["dismissed_reload"] = pg.evaluate(CHECK)
    pg.context.close()


def all_done_and_storage_down(r):
    pg = _page(r, _variant(inst=U.INST))          # default has 3 chunks
    OUT["alldone_checklist"] = pg.evaluate(CHECK)
    pg.context.close()
    pg = _page(r, _variant(redisDown=True))
    OUT["down_checklist"] = pg.evaluate(CHECK)
    OUT["down_strip"] = pg.evaluate(STRIP)
    pg.locator("#health-strip .hs-item", has_text="Storage").click()
    pg.wait_for_timeout(120)
    OUT["down_strip_click"] = pg.evaluate("document.querySelector('.stab.active')?.dataset.tab")
    pg.context.close()


def live_changes(r):
    pg = _page(r, _variant(inst=U.INST))
    OUT["live_initial"] = pg.evaluate(STRIP)
    # The 30-s poll finds the storage gone, then back.
    pg.evaluate("window.__UXV.redisDown = true")
    pg.evaluate("checkLocalStatus()")
    pg.wait_for_timeout(200)
    OUT["live_storage_down"] = {"strip": pg.evaluate(STRIP), "said": pg.evaluate("document.getElementById('sr-status').textContent")}
    pg.evaluate("window.__UXV.redisDown = false")
    pg.evaluate("checkLocalStatus()")
    pg.wait_for_timeout(200)
    OUT["live_storage_back"] = {"strip": pg.evaluate(STRIP), "said": pg.evaluate("document.getElementById('sr-status').textContent")}
    # Ollama stops answering: the provider item turns, and the model list is reloaded.
    pg.evaluate("window.__UXV.ollamaStatus = {ok:false, error:'Ollama is not reachable at http://localhost:11434'}")
    pg.evaluate("checkLocalStatus()")
    pg.wait_for_timeout(300)
    OUT["live_ollama_down"] = pg.evaluate(STRIP)[1]
    pg.evaluate("window.__UXV.ollamaStatus = {ok:true, models:3}")
    pg.evaluate("checkLocalStatus()")
    _wait(pg, "() => S.currentModel && !S.modelsLoading", 3)
    pg.wait_for_timeout(100)
    OUT["live_ollama_back"] = pg.evaluate(STRIP)[1]
    # The socket drops: "reconnecting" at once, a message sent now waits and says so.
    pg.evaluate("window.__wsDown = true; S.ws._drop();")
    pg.wait_for_timeout(100)
    OUT["ws_drop"] = {"strip": pg.evaluate(STRIP)[2], "said": pg.evaluate("document.getElementById('sr-status').textContent"),
                      "retry": pg.evaluate(BOX, "#health-strip .hs-retry")}
    pg.fill("#msg-input", "Is it back?")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(150)
    OUT["ws_queued"] = {"strip": pg.evaluate(STRIP)[2], "phase": pg.evaluate("document.getElementById('typing-phase')?.innerText")}
    # Still away after 15 s: "server unreachable"; storage and model are no longer claimed.
    pg.evaluate("window.__wsDown = true")
    _wait(pg, "() => S.wsState === 'offline'", 18, 250)
    # A browser fires "error" before each failed reconnect's "close" (the fake does not).
    pg.evaluate("S.ws.onerror && S.ws.onerror({})")
    pg.wait_for_timeout(100)
    OUT["ws_offline"] = {"strip": pg.evaluate(STRIP), "said": pg.evaluate("document.getElementById('sr-status').textContent"),
                         "input": pg.evaluate("document.getElementById('msg-input').value"),
                         "streaming": pg.evaluate("S.streaming"),
                         "toasts": _toasts(pg)}
    # Back: the strip says connected; "Retry now" by a real click reconnects at once.
    pg.evaluate("window.__wsDown = false")
    n = pg.evaluate("window.__sockets.length")
    pg.locator("#health-strip .hs-retry").click()
    _wait(pg, "() => S.wsState === 'open'", 3)
    OUT["ws_back"] = {"strip": pg.evaluate(STRIP), "said": pg.evaluate("document.getElementById('sr-status').textContent"),
                      "newSockets": pg.evaluate("window.__sockets.length") - n,
                      "wsErrorToasts": [t for t in _toasts(pg) if "WebSocket error" in t]}
    # A drop then a send that is delivered by the reconnect: the waiting note clears.
    pg.evaluate("S.ws._drop()")
    pg.fill("#msg-input", "Second")
    pg.keyboard.press("Enter")
    _wait(pg, "() => window.__sockets.at(-1).sent.length > 0", 5)
    pg.wait_for_timeout(100)
    OUT["ws_delivered"] = {"strip": pg.evaluate(STRIP)[2],
                           "sent": pg.evaluate("window.__sockets.at(-1).sent.map(d => JSON.parse(d).content)")}
    pg.context.close()
    # The browser itself goes offline.
    pg = _page(r, _variant(inst=U.INST))
    pg.context.set_offline(True)
    pg.evaluate("S.ws._drop(); window.dispatchEvent(new Event('offline'))")
    pg.wait_for_timeout(150)
    OUT["net_offline"] = pg.evaluate(STRIP)[2]
    pg.context.set_offline(False)
    pg.context.close()


def layout_and_theme(r):
    res = {}
    for w, scheme in ((1400, "light"), (375, "light"), (375, "dark"), (320, "light")):
        pg = _page(r, _variant(theme=scheme, redisDown=(w == 320)), width=w, height=800, scheme=scheme)
        if scheme == "dark":
            pg.evaluate("applyTheme('dark')")
            pg.wait_for_timeout(100)
        key = f"{w}_{scheme}"
        res[key] = {
            "strip": pg.evaluate(BOX, "#health-strip"),
            "items": pg.evaluate(STRIP),
            "msg": pg.evaluate(BOX, "#msg-input"),
            "wrap": pg.evaluate(BOX, "#input-wrap"),
            "send": pg.evaluate(BOX, "#send-btn"),
            "checklist": pg.evaluate(CHECK),
            "docOverflow": pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"),
            # Scrolled to the very top: is the start of the welcome screen on screen?
            "welcomeTop": pg.evaluate("""() => { const a = document.getElementById('chat-area'); a.scrollTop = 0;
                return {area: Math.round(a.getBoundingClientRect().top),
                        logo: Math.round(document.querySelector('.welcome-logo').getBoundingClientRect().top),
                        overflow: a.scrollHeight - a.clientHeight}; }"""),
            "stripOverflow": pg.evaluate("(() => { const s = document.getElementById('health-strip'); return s.scrollWidth - s.clientWidth; })()"),
            "contrast": {
                "stripName": pg.evaluate(CONTRAST, pg.query_selector("#health-strip .hs-name")),
                "stripDetail": pg.evaluate(CONTRAST, pg.query_selector("#health-strip .hs-detail")) if pg.query_selector("#health-strip .hs-detail") else None,
                "frDetail": pg.evaluate(CONTRAST, pg.query_selector("#welcome-checklist .fr-detail")) if pg.query_selector("#welcome-checklist .fr-detail") else None,
                "frTitle": pg.evaluate(CONTRAST, pg.query_selector("#welcome-checklist .fr-title")) if pg.query_selector("#welcome-checklist .fr-title") else None,
            },
        }
        r.shot(pg, f"layout_{key}")
        pg.context.close()
    OUT["layout"] = res


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
        for step in (fresh_install, active_instance_target, no_model_and_dismiss,
                     all_done_and_storage_down, live_changes, layout_and_theme):
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
