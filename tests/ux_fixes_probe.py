# SPDX-License-Identifier: AGPL-3.0-or-later
"""The usability-review fixes, measured in a real browser with canned API answers (no
server, no database) and a fake WebSocket. Prints one JSON object.

Everything recorded is a computed or observed value: a pixel width, the body a request
was sent with, which element a key press reached, the contrast of two measured colours.

    python ux_fixes_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys
import time

OUT: dict = {}

_SECRET = "__VISUALWEAVER_SECRET_KEPT__"
CONFIG = {
    "storage": {"default_type": "redis", "app": "default"},
    "redis": {"host": "127.0.0.1", "port": 6389, "db": 0, "password": _SECRET, "ssl": False},
    "redis_endpoints": [{"name": "default", "primary": True, "host": "127.0.0.1", "port": 6389,
                         "type": "redis", "builtin": True}],
    "ollama": {"host": "http://localhost", "port": 11434, "model": ""},
    "provider": "ollama",
    "embedding": {"model": "intfloat/multilingual-e5-base", "max_image_dim": 1024},
    "rag": {"chunk_size": 180, "chunk_overlap": 32, "top_k": 5, "similarity_threshold": 0.35,
            "hybrid_search": True, "rerank_candidates": 40},
    "cache": {"enabled": True, "similarity_threshold": None, "ttl": 3600},
    "ui": {"theme": "light", "show_rag_matches": False},
    "security": {"enabled": True, "password": _SECRET},
    "claude": {"api_key": _SECRET, "model": "claude-sonnet-4-6"}, "claude_key_from_env": False,
    "openai": {"api_key": _SECRET, "model": "gpt-4o"}, "openai_key_from_env": True,
    "web_sources": ["https://a.example/docs", "https://b.example/wiki"],
    "watch_folders": {"enabled": True, "interval_minutes": 5,
                      "folders": [{"path": "/data/notes", "instance": "default"}]},
    "crawl": {"concurrency": 10, "js_concurrency": 3, "js_render": False, "smart_mode": True},
    "recrawl": {"enabled": False, "interval_minutes": 60},
    "base_instruction": "BI", "base_instruction_default": "BI", "cache_threshold_effective": 0.95,
}
OLLAMA = [
    # First in the list, and the model this browser last used: it must still not be picked.
    {"name": "nomic-embed-text:latest", "size": 274e6, "vision": False,
     "details": {"family": "nomic-bert", "families": ["nomic-bert"]}},
    {"name": "llama3:latest", "size": 4.7e9, "vision": False, "details": {"family": "llama"}},
    {"name": "mxbai-embed-large:latest", "size": 670e6, "vision": False, "details": {"family": "bert"}},
]
STORAGE = {"app": "default", "default_type": "redis", "connections": [
    {"name": "default", "type": "redis", "builtin": True, "label": "Built-in Redis", "host": "127.0.0.1:6389",
     "ok": True, "vector_search": True, "is_app": True, "instances": 1}]}
INST = [{"name": "default", "chunks": 3, "color": "#7c6ff7", "redis_endpoint": "default",
         "reachable": True, "enabled": True}]

FAKE = r"""
(() => {
  const V = window.__UXV || {};
  window.__posts = []; window.__calls = [];
  const json = (b, s=200) => Promise.resolve(new Response(JSON.stringify(b),
                 {status:s, headers:{'Content-Type':'application/json'}}));
  const later = (ms, f) => new Promise(r => setTimeout(r, ms)).then(f);
  const sse = evs => new Response(new ReadableStream({start(c){const e=new TextEncoder();
                 for(const ev of evs)c.enqueue(e.encode('data: '+JSON.stringify(ev)+'\n\n')); c.close();}}),
                 {status:200, headers:{'Content-Type':'text/event-stream'}});
  const cfg = V.config;
  const inst = V.inst;
  window.fetch = (url, opt={}) => {
    const u = String(url), m = (opt.method||'GET').toUpperCase();
    window.__calls.push(m+' '+u);
    let body = null;
    try { body = typeof opt.body === 'string' ? JSON.parse(opt.body) : null; } catch(e) {}
    if (m !== 'GET') window.__posts.push({url:u, method:m, body});
    if (u === '/api/config' && m === 'GET') return json(cfg);
    if (u === '/api/config') return json({ok:true});
    if (u === '/api/health') return json({status:'ok', version:'9.9.9', license:'AGPL-3.0-or-later',
                                          source:'', services:{js_crawl: V.jsCrawl}});
    if (u === '/api/ollama/models') return later(V.modelsDelay||0, () => json(V.ollama));
    if (/^\/api\/(claude|openai|qwen|mistral|groq|gemini)\/models/.test(u)) return json([]);
    if (u === '/api/status/redis') return later(V.statusDelay||0, () => json(V.statusRedis ? V.statusRedis : V.redisDown
        ? {ok:false, error:'Connection refused'}
        : {ok:true, version:'8.2.1', memory_used:'2.1M', connected_clients:3, uptime_days:4,
           search_available:true}));
    if (u.startsWith('/api/status/redis/')) return json((V.epStatus||{})[decodeURIComponent(u.split('/').pop())] || {ok:true, search_available:true});
    if (u === '/api/status/ollama' && V.ollamaStatus) return json(V.ollamaStatus);
    if (u.startsWith('/api/status/')) return json({ok:false, configured:false});
    if (u === '/api/storage/app') {
      if (V.copyFail && body.copy !== false)
        return json({detail:`Could not copy the app data from '${V.storage.app}' to '${body.connection}' (Error 61 connecting to 10.0.0.5:6380. Connection refused.). Nothing was switched. To switch without copying, send "copy": false.`}, 400);
      V.storage.app = body.connection;
      return json({ok:true, app:body.connection, copied: body.copy === false ? null : {conversations:3}});
    }
    if (u.startsWith('/api/rag/stats') && V.ragStats) return json(V.ragStats);
    if (u.startsWith('/api/redis/all-stats') && V.allStats) return json(V.allStats);
    if (u === '/api/rag/instances' && m === 'POST') {
      if (inst.some(i => i.name === body.name))
        return json({detail:`An instance named '${body.name}' already exists`}, 409);
      inst.push({name:body.name, chunks:0, color:body.color||'#6366f1',
                 redis_endpoint:body.endpoint||'default', reachable:true, enabled:true});
      return later(V.createDelay||0, () => json({name:body.name}));
    }
    if (u === '/api/rag/instances') return later(V.listDelay||0, () => json(inst.map(x => ({...x}))));
    if (/\/ingest\/files\/stream/.test(u)) {
      if (V.ingestRefuse) return json({detail:V.ingestRefuse}, 413);
      const n = u.match(/\/api\/rag\/([^/]+)\//)[1];
      return later(20, () => { const i = inst.find(x => x.name === decodeURIComponent(n)); if (i) i.chunks += 3;
        return sse([{job:'j1', total:1}, {file:'a.md', status:'ok', chunks:3, index:0, total:1},
                    {done:true, total:1, ok:1, errors:0}]); });
    }
    if (/\/ingest\/url\/stream/.test(u)) return V.crawlRefuse ? json({detail:V.crawlRefuse}, 400) : sse([{done:true}]);
    if (u === '/api/templates') return json(V.templates);
    if (u === '/api/sessions') return json(V.sessions||[]);
    if (u.startsWith('/api/sessions/')) {
      const id = decodeURIComponent(u.split('/').pop());
      return json((V.sessionMsgs||{})[id]||[]);
    }
    if (u === '/api/storage') return V.storage ? json(V.storage) : json({detail:'Not Found'}, 404);
    if (u.startsWith('/api/redis/endpoints')) return json(V.eps || cfg.redis_endpoints);
    if (u === '/api/chat/upload-file') {
      const name = opt.body.get('file').name;
      return later((V.uploadDelay||{})[name]||0, () => json({filename:name, text:'T:'+name}));
    }
    if (u.startsWith('/api/cache/stats')) return json({count:7, entries:[]});
    if (u.startsWith('/api/redis/memory')) return json(V.memory || {used:1, used_human:'1M', peak_human:'1M', max:0, max_human:'0B'});
    if (/\/api\/(rag\/stats|redis\/all-stats|crawl\/active|ingest\/active|recrawl|logs|rag\/logs)/.test(u)) return json([]);
    if (u.startsWith('/api/usage')) return json({});
    return json({});
  };

  // A WebSocket that never touches the network. send() on CONNECTING throws and on
  // CLOSED drops the frame silently — exactly what the browser's own does.
  window.__sockets = [];
  class FakeWS {
    constructor(url){ this.url=url; this.readyState=0; this.sent=[]; this.lost=[];
      window.__sockets.push(this); setTimeout(() => this._open(), 15); }
    _open(){ if (this.readyState !== 0) return;
      if (window.__wsDown){ this.readyState=3; this.onclose && this.onclose({}); return; }
      this.readyState=1; this.onopen && this.onopen({}); }
    send(d){ if (this.readyState === 0) throw new DOMException('Still in CONNECTING state', 'InvalidStateError');
      if (this.readyState !== 1){ this.lost.push(d); return; } this.sent.push(d); }
    close(){ if (this.readyState === 3) return; this.readyState=3;
      setTimeout(() => this.onclose && this.onclose({}), 0); }
    _drop(){ this.readyState=3; this.onclose && this.onclose({}); }
    _msg(o){ this.onmessage && this.onmessage({data:JSON.stringify(o)}); }
  }
  FakeWS.CONNECTING=0; FakeWS.OPEN=1; FakeWS.CLOSING=2; FakeWS.CLOSED=3;
  window.WebSocket = FakeWS;

  if (V.speech) {
    window.__recognisers = [];
    class FakeSR {
      constructor(){ this.lang=''; this.continuous=true; window.__recognisers.push(this); }
      start(){} stop(){}
    }
    // A browser offering on-device recognition: the page must not switch it on (with it set,
    // and on available(), Chromium's renderer crashed where no on-device recogniser exists).
    FakeSR.prototype.processLocally = false;
    FakeSR.available = async (o) => { window.__availableAsked = o; return 'available'; };
    window.SpeechRecognition = FakeSR;
  } else { try { delete window.SpeechRecognition; delete window.webkitSpeechRecognition; } catch(e) {} }

  try {
    localStorage.clear();
    localStorage.setItem('theme', V.theme || 'light');
    localStorage.setItem('rr.prefs', JSON.stringify({provider:'ollama', model:'nomic-embed-text:latest'}));
    if (V.mine) localStorage.setItem('mySessionIds', JSON.stringify(V.mine));
  } catch(e) {}
})();
"""

# ── measurement helpers (run in the page) ──────────────────────────────────────
CONTRAST = r"""(el) => {
  const parse = c => { const m = c && c.match(/rgba?\(([^)]+)\)/); if (!m) return null;
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number);
    return {r:p[0], g:p[1], b:p[2], a:p.length > 3 ? p[3] : 1}; };
  const over = (t, b) => { const a = t.a + b.a*(1-t.a); if (!a) return {r:0,g:0,b:0,a:0};
    return {r:(t.r*t.a+b.r*b.a*(1-t.a))/a, g:(t.g*t.a+b.g*b.a*(1-t.a))/a, b:(t.b*t.a+b.b*b.a*(1-t.a))/a, a}; };
  const lum = c => { const f = v => { v/=255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c.r)+0.7152*f(c.g)+0.0722*f(c.b); };
  const stack = [];
  for (let e = el; e; e = e.parentElement) { const c = parse(getComputedStyle(e).backgroundColor);
    if (c && c.a > 0) stack.push(c); if (c && c.a >= 1) break; }
  const root = getComputedStyle(document.documentElement).getPropertyValue('--bg0').trim();
  let base = /^#/.test(root) ? {r:parseInt(root.slice(1,3),16), g:parseInt(root.slice(3,5),16), b:parseInt(root.slice(5,7),16), a:1}
                             : {r:255,g:255,b:255,a:1};
  for (let i = stack.length - 1; i >= 0; i--) base = over(stack[i], base);
  let fg = parse(getComputedStyle(el).color); if (fg.a < 1) fg = over(fg, base);
  const L1 = lum(fg), L2 = lum(base);
  return {ratio: +(((Math.max(L1,L2)+0.05)/(Math.min(L1,L2)+0.05)).toFixed(2)),
          color: getComputedStyle(el).color, size: parseFloat(getComputedStyle(el).fontSize),
          text: el.textContent.trim().slice(0, 30)};
}"""

BOX = r"""(sel) => { const el = typeof sel === 'string' ? document.querySelector(sel) : sel; if (!el) return null;
  const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
  return {x:Math.round(r.left), y:Math.round(r.top), w:Math.round(r.width), h:Math.round(r.height),
          right:Math.round(r.right), bottom:Math.round(r.bottom), display:cs.display}; }"""


def _variant(**over):
    v = {"config": json.loads(json.dumps(CONFIG)), "inst": json.loads(json.dumps(INST)),
         "ollama": OLLAMA, "jsCrawl": True, "storage": STORAGE,
         "templates": [{"name": "Default", "system": ""},
                       {"name": "Redis Expert", "system": "You are a Redis expert."}]}
    v.update(over)
    return v


def _wait(pg, js, timeout=8.0, step=100):
    """Poll a page expression from Python until it is truthy (no string-eval in the page)."""
    end = time.time() + timeout
    val = None
    while time.time() < end:
        val = pg.evaluate(js)
        if val:
            return val
        pg.wait_for_timeout(step)
    return val


def _toasts(pg):
    return pg.evaluate("[...document.querySelectorAll('#toast-container .toast')]"
                       ".filter(t=>!t.style.animation).map(t=>t.dataset.toastType+': '+t.dataset.toastMsg)")


def _modal(pg):
    return pg.evaluate("""() => document.getElementById('modal-overlay').classList.contains('open')
      ? {title: document.getElementById('modal-title').innerText,
         body: document.getElementById('modal-body').innerText,
         buttons: [...document.querySelectorAll('#modal-actions button')].map(b => b.innerText)} : null""")


class Run:
    def __init__(self, p, engine, shots):
        self.p, self.engine, self.shots = p, engine, shots
        self.browser = getattr(p, engine).launch()
        self.errors = []

    def page(self, variant, width=1400, height=900, scheme="light"):
        ctx = self.browser.new_context(viewport={"width": width, "height": height}, color_scheme=scheme)
        pg = ctx.new_page()
        pg.set_default_timeout(8000)
        pg.on("pageerror", lambda e: self.errors.append((e.stack or str(e))[:300]))
        pg.add_init_script("window.__UXV = " + json.dumps(variant) + ";")
        pg.add_init_script(FAKE)
        pg.goto(INDEX.as_uri(), wait_until="load")
        _wait(pg, "() => typeof S === 'object' && S.modelsLoaded && Array.isArray(S.ragInstances) && S.ragInstances.length > 0", 10)
        pg.wait_for_timeout(250)
        return pg

    def shot(self, pg, name):
        if self.shots:
            pg.screenshot(path=str(self.shots / f"{self.engine}_{name}.png"))


def boot_and_models(r: Run):
    # B12: the first paint, while the model list is still on its way
    ctx = r.browser.new_context(viewport={"width": 1400, "height": 900})
    pg = ctx.new_page()
    pg.on("pageerror", lambda e: r.errors.append((e.stack or str(e))[:300]))
    pg.add_init_script("window.__UXV = " + json.dumps(_variant(modelsDelay=2500, statusDelay=1500)) + ";")
    pg.add_init_script(FAKE)
    pg.goto(INDEX.as_uri(), wait_until="load")
    _wait(pg, "() => !!document.getElementById('welcome-setup-why')", 5)
    pg.wait_for_timeout(300)
    snap = """() => { const vis = el => !!el && getComputedStyle(el).display !== 'none' && el.getBoundingClientRect().height > 0;
      return {title: document.getElementById('welcome-setup-title')?.innerText,
              why: document.getElementById('welcome-setup-why')?.innerText,
              setupVisible: vis(document.getElementById('welcome-setup')),
              actionsVisible: vis(document.getElementById('welcome-setup-actions')),
              chipsVisible: vis(document.getElementById('welcome-chips')),
              model: S.currentModel}; }"""
    OUT["loading"] = pg.evaluate(snap)
    _wait(pg, "() => S.modelsLoaded", 8)
    pg.wait_for_timeout(200)
    OUT["loaded"] = pg.evaluate(snap)
    # B17
    OUT["models"] = pg.evaluate("""() => ({current: S.currentModel, list: S.models.map(m => m.id)})""")
    pg.evaluate("openSettings('providers')")
    pg.evaluate("S.ollamaModels = %s; renderModelsTable();" % json.dumps(OLLAMA))
    OUT["models_table"] = pg.evaluate("""() => [...document.querySelectorAll('#models-table tr')].slice(1)
        .map(tr => [tr.cells[0].textContent.trim(), tr.cells[3].textContent.replace(/\s+/g, ' ').trim()])""")
    pg.evaluate("closeSettings(true)")
    # only embedding models on Ollama
    pg.evaluate("""async () => { window.__UXV.ollama.splice(1, 1); await loadModels(); }""")
    OUT["embed_only"] = pg.evaluate(snap)
    ctx.close()


def welcome_chips_and_misc(r: Run):
    pg = r.page(_variant(speech="remote"))
    # B10 chips are buttons in the tab order
    OUT["chips"] = pg.evaluate("""() => [...document.querySelectorAll('#welcome-chips .chip')]
        .map(c => [c.tagName, c.tabIndex])""")
    # B15: one "Default" in the composer picker
    OUT["template_options"] = pg.evaluate("[...document.getElementById('template-select').options].map(o => o.textContent)")
    # B22: an install that has "default" never re-creates it; concurrent loads POST nothing
    pg.evaluate("async () => { await Promise.all([loadRagInstances(), loadRagInstances(), checkLocalStatus()]); }")
    OUT["default_posts_existing"] = pg.evaluate("window.__posts.filter(p => p.url === '/api/rag/instances').length")
    # B19 touch targets
    pg.evaluate("S.sessions['s_t'] = {title:'Touch target', messages:[]}; renderSessionList();")
    OUT["targets"] = {k: pg.evaluate(BOX, sel) for k, sel in {
        "session_del": "#sessions-list .session-del", "doc_attach": ".attach-btn:not(#attach-btn)",
        "img_attach": "#attach-btn", "mic": "#mic-btn"}.items()}
    # B16
    OUT["drop_hint"] = pg.evaluate("document.getElementById('ingest-drop-hint').innerText")
    # B18
    pg.evaluate("openSettings('analytics')")
    _wait(pg, "() => !!document.querySelector('#analytics-content .stat-grid')", 5)
    OUT["analytics_labels"] = pg.evaluate("[...document.querySelectorAll('#analytics-content .stat-label')].map(e => e.innerText.split('\\n')[0])")
    # F1: no Save/Cancel on read-only tabs, back on a settings tab
    foot = "() => getComputedStyle(document.querySelector('.settings-footer')).display"
    OUT["footer"] = {"analytics": pg.evaluate(foot)}
    for t in ("status", "about", "general"):
        pg.evaluate(f"switchTab('{t}')")
        OUT["footer"][t] = pg.evaluate(foot)
    # an unsaved edit keeps Save in reach on a read-only tab
    pg.fill("#s-max-img-dim", "2048")
    # The edit registers on the field's input/change event; a user's click on the tab
    # comes after it, so wait for it rather than racing it (Firefox fires it later).
    _wait(pg, "() => _settingsDirty()", 5)
    pg.evaluate("switchTab('status')")
    OUT["footer"]["status_dirty"] = pg.evaluate(foot)
    pg.evaluate("closeSettings(true)")
    # B14 Status tiles (light)
    pg.evaluate("openSettings('status')")
    _wait(pg, "() => document.querySelectorAll('#status-content .glass-card div[style*=\"font-size:16px\"]').length >= 3", 6)
    tiles = pg.query_selector_all('#status-content .glass-card div[style*="font-size:16px"]')
    OUT["contrast_status_light"] = [pg.evaluate(CONTRAST, t) for t in tiles[:3]]
    pg.evaluate("closeSettings(true)")
    # B14 the Playwright notice: hidden when the server has the JS crawler, legible otherwise
    pg.evaluate("openSettings('websources')")
    OUT["js_warn_available"] = pg.evaluate("getComputedStyle(document.getElementById('crawl-js-warn')).display")
    pg.evaluate("S.appInfo.services.js_crawl = false; _updateJsWarn();")
    OUT["js_warn_missing"] = pg.evaluate("getComputedStyle(document.getElementById('crawl-js-warn')).display")
    OUT["contrast_js_warn_light"] = pg.evaluate(CONTRAST, pg.query_selector("#crawl-js-warn"))
    pg.evaluate("closeSettings(true)")
    # B14 the Redis wordmark on the storage banner (light)
    pg.evaluate("openSettings('redis')")
    pg.wait_for_timeout(300)
    span = pg.query_selector("#storage-banner-title .redis-brand") or pg.query_selector("#storage-banner-title")
    OUT["contrast_banner_light"] = pg.evaluate(CONTRAST, span)
    pg.evaluate("closeSettings(true)")
    # B14 sidebar count pill
    OUT["contrast_pill_light"] = pg.evaluate(CONTRAST, pg.query_selector("#rag-sidebar-list .rag-pill"))
    OUT["contrast_pinned_light"] = pg.evaluate(CONTRAST, pg.query_selector("#pinned-count"))
    # B11 template fields are labelled
    pg.evaluate("openSettings('templates')")
    OUT["template_labels"] = pg.evaluate("""() => [...document.querySelectorAll('#templates-list input, #templates-list textarea')]
        .map(e => e.labels.length ? e.labels[0].innerText : null)""")
    pg.evaluate("closeSettings(true)")
    # B20: every confirmation puts Cancel first
    orders = {}
    for fn in ("resetRag(0)", "optimizeRag(0)", "unloadRag(0)"):
        pg.evaluate(fn)
        orders[fn] = (_modal(pg) or {}).get("buttons")
        pg.evaluate("closeModal()")
    OUT["dialog_orders"] = orders
    r.shot(pg, "home_light")
    pg.context.close()


def dark_contrast(r: Run):
    pg = r.page(_variant(theme="dark", config=dict(json.loads(json.dumps(CONFIG)), ui={"theme": "dark"})),
                scheme="dark")
    OUT["theme_dark"] = pg.evaluate("document.documentElement.dataset.theme")
    OUT["contrast_pill_dark"] = pg.evaluate(CONTRAST, pg.query_selector("#rag-sidebar-list .rag-pill"))
    pg.evaluate("openSettings('redis')")
    pg.wait_for_timeout(300)
    pg.evaluate("renderStorage && renderStorage()")
    span = pg.query_selector("#storage-banner-title .redis-brand") or pg.query_selector("#storage-banner-title")
    OUT["contrast_banner_dark"] = pg.evaluate(CONTRAST, span)
    pg.evaluate("closeSettings(true)")
    pg.evaluate("openSettings('status')")
    _wait(pg, "() => document.querySelectorAll('#status-content .glass-card div[style*=\"font-size:16px\"]').length >= 3", 6)
    tiles = pg.query_selector_all('#status-content .glass-card div[style*="font-size:16px"]')
    OUT["contrast_status_dark"] = [pg.evaluate(CONTRAST, t) for t in tiles[:3]]
    pg.evaluate("closeSettings(true)")
    pg.evaluate("openSettings('websources')")
    pg.evaluate("S.appInfo.services.js_crawl = false; _updateJsWarn();")
    OUT["contrast_js_warn_dark"] = pg.evaluate(CONTRAST, pg.query_selector("#crawl-js-warn"))
    r.shot(pg, "websources_dark")
    pg.evaluate("closeSettings(true)")
    pg.context.close()


def reset_and_discard(r: Run):
    pg = r.page(_variant())
    pg.evaluate("openSettings('general')")
    pg.get_by_role("button", name="Reset Defaults").click()
    OUT["reset_modal"] = _modal(pg)
    pg.locator("#modal-actions button", has_text="Reset to defaults").click()
    pg.wait_for_timeout(100)
    OUT["after_reset"] = pg.evaluate("""() => ({form: [document.getElementById('s-redis-host').value, document.getElementById('s-redis-port').value],
        collected: _collectSettings().redis ?? null, dirty: !document.getElementById('settings-dirty').hidden,
        embed: document.getElementById('s-embed-model').value})""")
    # B2: the close button asks, and Discard puts everything back
    pg.click(".settings-close >> nth=0")
    OUT["close_after_reset_modal"] = _modal(pg)
    pg.locator("#modal-actions button", has_text="Discard changes").click()
    pg.wait_for_timeout(100)
    OUT["after_discard"] = pg.evaluate("""() => ({redis: S.config.redis, web: S.config.web_sources,
        folders: S.config.watch_folders.folders.length, key: S.config.claude.api_key, keys: Object.keys(S.config).length})""")
    pg.evaluate("openSettings('redis')")
    OUT["reopen_form"] = pg.evaluate("[document.getElementById('s-redis-host').value, document.getElementById('s-redis-port').value]")
    pg.evaluate("closeSettings(true)")
    # B1: Reset then Save posts the stored built-in Redis, and no edit flag
    pg.evaluate("openSettings('general')")
    pg.get_by_role("button", name="Reset Defaults").click()
    pg.locator("#modal-actions button", has_text="Reset to defaults").click()
    pg.evaluate("window.__posts = []")
    pg.get_by_role("button", name="💾 Save Settings").click()
    _wait(pg, "() => window.__posts.some(p => p.url === '/api/config')", 5)
    OUT["reset_save_post"] = pg.evaluate("window.__posts.find(p => p.url === '/api/config').body")
    _wait(pg, "() => !document.getElementById('settings-overlay').classList.contains('open')", 5)
    # the Built-in Redis is shown read-only, as installed, and never posted
    pg.evaluate("openSettings('redis')")
    OUT["builtin_redis_form"] = pg.evaluate("""() => ({readonly: ['s-redis-host','s-redis-port','s-redis-db','s-redis-pass']
        .map(id => document.getElementById(id).readOnly), ssl: document.getElementById('s-redis-ssl').disabled,
        note: document.getElementById('redis-builtin-note').innerText,
        values: [document.getElementById('s-redis-host').value, document.getElementById('s-redis-port').value]})""")
    pg.evaluate("closeSettings(true)")
    # a config with no redis section never gets one built from the form's fallbacks
    OUT["no_redis_section"] = pg.evaluate("""() => { const was = S.config.redis; delete S.config.redis;
        populateSettingsForm(); const c = _collectSettings(); S.config.redis = was;
        return {form: document.getElementById('s-redis-port').value, posts: 'redis' in c,
                postsWithIt: (populateSettingsForm(), 'redis' in _collectSettings())}; }""")
    pg.context.close()

    # B3: list edits are unsaved changes, and Discard undoes them
    pg = r.page(_variant())
    pg.evaluate("openSettings('websources')")
    pg.fill("#crawl-url", "https://c.example/new")
    pg.evaluate("document.getElementById('settings-dirty').hidden = true; _settingsTouched = false;")
    pg.evaluate("addWebSource()")
    OUT["dirty_after_add_source"] = pg.evaluate("!document.getElementById('settings-dirty').hidden")
    pg.evaluate("switchTab('templates')")
    pg.locator("#templates-list button", has_text="Remove").nth(1).click()
    pg.locator("#modal-actions button", has_text="Remove").click()
    pg.evaluate("switchTab('rag')")
    pg.evaluate("removeWatchFolder(0)")
    OUT["during"] = pg.evaluate("({web: S.config.web_sources.length, tpl: S.templates.map(t => t.name), folders: S.config.watch_folders.folders.length})")
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(150)
    OUT["discard_modal"] = _modal(pg)
    if OUT["discard_modal"]:
        pg.locator("#modal-actions button", has_text="Discard changes").click()
    pg.wait_for_timeout(100)
    OUT["after_list_discard"] = pg.evaluate("""() => ({web: S.config.web_sources, tpl: S.templates.map(t => t.name),
        folders: S.config.watch_folders.folders.length,
        composer: [...document.getElementById('template-select').options].map(o => o.textContent),
        open: document.getElementById('settings-overlay').classList.contains('open')})""")
    pg.context.close()


def ingest_crawl_create(r: Run):
    # B4: a refused upload stops at once and says why
    pg = r.page(_variant(ingestRefuse="'big.pdf' is 120 MB; the limit is 100 MB (raise VISUALWEAVER_MAX_UPLOAD_MB to change it)",
                         crawlRefuse="URL must start with http:// or https://"))
    pg.evaluate("openSettings('rag')")
    pg.evaluate("async () => { await ingestFiles([new File(['x'], 'big.pdf')]); }")
    OUT["ingest_refused"] = pg.evaluate("""() => ({status: document.getElementById('ingest-status').innerText,
        bar: document.getElementById('ingest-bar').style.width})""")
    OUT["ingest_refused_toasts"] = _toasts(pg)
    pg.evaluate("switchTab('websources')")
    pg.wait_for_timeout(300)
    pg.fill("#crawl-url", "ftp://nope")
    url = pg.evaluate("async () => { const v = document.getElementById('crawl-url').value; await startCrawl(); return v; }")
    OUT["crawl_refused"] = {"toasts": _toasts(pg), "url": url,
                            "start_enabled": pg.evaluate("!document.getElementById('crawl-start-btn').disabled")}
    pg.context.close()

    # B7: a new instance becomes the target, the form clears, the messages name it
    pg = r.page(_variant())
    pg.evaluate("openSettings('rag')")
    pg.get_by_role("button", name="＋ New Instance").click()
    pg.fill("#new-rag-name", "handbook")
    pg.locator("#create-rag-form button", has_text="Create").click()
    _wait(pg, "() => S.ragInstances.some(r => r.name === 'handbook')", 5)
    pg.wait_for_timeout(100)
    OUT["after_create"] = pg.evaluate("""() => ({ingest: document.getElementById('ingest-target-rag').value,
        crawl: document.getElementById('crawl-target-rag').value,
        watch: document.getElementById('watch-add-instance').value,
        chat: S.activeRag, topbar: document.getElementById('rag-select').value,
        name: document.getElementById('new-rag-name').value, dirty: _settingsDirty()})""")
    OUT["create_toasts"] = _toasts(pg)
    # a second Create straight after is not a 409 on the old name
    pg.get_by_role("button", name="＋ New Instance").click()
    OUT["second_create_name"] = pg.evaluate("document.getElementById('new-rag-name').value")
    pg.evaluate("document.getElementById('create-rag-form').style.display='none'")
    # empty-instance answer (before ingest): the badge says empty, not "no match"
    pg.evaluate("closeSettings(true)")
    pg.evaluate("""() => { S.currentRagTargets = ['handbook'];
        handleWSMsg({type:'stream_start'}); handleWSMsg({type:'rag_context', chunks:[], rag_used:true, latency:{}});
        handleWSMsg({type:'token', content:'An answer.'}); handleWSMsg({type:'stream_end', latency:{total:.1}}); }""")
    OUT["empty_badge"] = pg.evaluate("[...document.querySelectorAll('.rag-nomatch-badge')].pop()?.innerText")
    pg.evaluate("openSettings('rag')")
    pg.evaluate("async () => { await ingestFiles([new File(['# Hi'], 'a.md')]); }")
    OUT["ingest_into_new"] = {"status": pg.evaluate("document.getElementById('ingest-status').innerText"),
                              "toasts": _toasts(pg),
                              "url": pg.evaluate("window.__posts.filter(p => /ingest/.test(p.url)).pop()?.url")}
    pg.evaluate("closeSettings(true)")
    # with documents in it, an empty retrieval is "no match" again
    pg.evaluate("""() => { S.currentRagTargets = ['handbook'];
        handleWSMsg({type:'stream_start'}); handleWSMsg({type:'rag_context', chunks:[], rag_used:true, latency:{}});
        handleWSMsg({type:'stream_end', latency:{total:.1}}); }""")
    OUT["nomatch_badge"] = pg.evaluate("[...document.querySelectorAll('.rag-nomatch-badge')].pop()?.innerText")
    pg.context.close()

    # B22: a fresh install with no "default", listed by boot and the poll at once
    v = _variant(inst=[], listDelay=60, createDelay=80)
    ctx = r.browser.new_context(viewport={"width": 1200, "height": 800})
    pg = ctx.new_page()
    pg.add_init_script("window.__UXV = " + json.dumps(v) + ";")
    pg.add_init_script(FAKE)
    pg.goto(INDEX.as_uri(), wait_until="load")
    _wait(pg, "() => S.modelsLoaded && S.ragInstances.length > 0", 8)
    pg.evaluate("async () => { await Promise.all([loadRagInstances(), loadRagInstances()]); }")
    OUT["fresh_default_posts"] = pg.evaluate("window.__posts.filter(p => p.url === '/api/rag/instances').map(p => p.body.name)")
    # three loads that all see no "default" before any create lands: still one create
    OUT["concurrent_default_posts"] = pg.evaluate("""async () => {
        window.__UXV.inst.splice(0); _defaultEnsured = false; window.__posts = [];
        await Promise.all([loadRagInstances(), loadRagInstances(), loadRagInstances()]);
        return window.__posts.filter(p => p.url === '/api/rag/instances').map(p => p.body.name); }""")
    ctx.close()


def websocket(r: Run):
    pg = r.page(_variant())
    _wait(pg, "() => S.ws && S.ws.readyState === 1", 3)
    # B5 a: the socket drops, the user sends straight away, the reconnect delivers it
    pg.evaluate("S.ws._drop()")
    pg.fill("#msg-input", "Thanks!")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(150)
    OUT["ws_queued"] = pg.evaluate("""() => ({lost: window.__sockets[0].lost.length, streaming: S.streaming,
        phase: document.getElementById('typing-phase')?.innerText})""")
    _wait(pg, "() => window.__sockets.length > 1 && window.__sockets.at(-1).sent.length > 0", 5)
    OUT["ws_delivered"] = pg.evaluate("""() => ({sockets: window.__sockets.length,
        sent: window.__sockets.at(-1).sent.map(d => JSON.parse(d).content)})""")
    pg.evaluate("""() => { const w = window.__sockets.at(-1);
        w._msg({type:'stream_start'}); w._msg({type:'token', content:'You are welcome.'}); w._msg({type:'stream_end', latency:{total:.2}}); }""")
    OUT["ws_after_answer"] = pg.evaluate("({streaming: S.streaming, typing: !!document.getElementById('typing-indicator')})")
    # B5 b: the server stays away; the send fails visibly and the text comes back
    pg.evaluate("window.__wsDown = true; S.ws._drop();")
    pg.fill("#msg-input", "Second question")
    pg.keyboard.press("Enter")
    _wait(pg, "() => !S.streaming", 14, 250)
    OUT["ws_failed"] = pg.evaluate("""() => ({streaming: S.streaming, typing: !!document.getElementById('typing-indicator'),
        input: document.getElementById('msg-input').value,
        stored: S.sessions[S.sessionId].messages.filter(m => m.content === 'Second question').length,
        sendDisabled: document.getElementById('send-btn').disabled})""")
    OUT["ws_failed_toasts"] = _toasts(pg)
    # B5 c: the connection drops in the middle of an answer
    pg.evaluate("window.__wsDown = false;")
    _wait(pg, "() => S.ws && S.ws.readyState === 1", 6)
    pg.evaluate("document.getElementById('msg-input').value = ''")
    pg.fill("#msg-input", "Third")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(100)
    pg.evaluate("""() => { S.ws._msg({type:'stream_start'}); S.ws._msg({type:'token', content:'Half an ans'}); S.ws._drop(); }""")
    pg.wait_for_timeout(100)
    OUT["ws_mid_stream"] = pg.evaluate("({streaming: S.streaming, cursor: !!document.querySelector('.stream-cursor')})")
    OUT["ws_mid_toasts"] = _toasts(pg)
    pg.context.close()


def layout(r: Run):
    res = {}
    for w in (320, 375, 390, 768, 1024):
        pg = r.page(_variant(), width=w, height=800)
        res[w] = {
            "msg": pg.evaluate(BOX, "#msg-input"),
            "wrap": pg.evaluate(BOX, "#input-wrap"),
            "tpl": pg.evaluate(BOX, "#template-select"),
            "send": pg.evaluate(BOX, "#send-btn"),
            "title": pg.evaluate(BOX, "#session-title"),
            "gear": pg.evaluate(BOX, "#topbar-actions .icon-btn:last-child"),
            "mp": pg.evaluate(BOX, "#mp-btn"),
            "rag": pg.evaluate(BOX, "#rag-select"),
            "topbarOverflow": pg.evaluate("(() => { const t = document.getElementById('topbar'); return t.scrollWidth - t.clientWidth; })()"),
            "docOverflow": pg.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth"),
        }
        if w in (375, 768):
            r.shot(pg, f"composer_{w}")
        if w == 390:
            # C2: the overlay sidebar closes once a conversation is picked from it
            pg.evaluate("S.sessions['s_other'] = {title:'Other chat', messages:[{role:'user',content:'hi',meta:{id:'msg_900'}}]}; renderSessionList();")
            pg.click("#toggle-sidebar")
            res[w]["sidebar_open"] = pg.evaluate("getComputedStyle(document.getElementById('sidebar')).display")
            pg.click("#sessions-list [data-sid='s_other']")
            pg.wait_for_timeout(200)
            res[w]["sidebar_after_pick"] = pg.evaluate("getComputedStyle(document.getElementById('sidebar')).display")
            res[w]["session_after_pick"] = pg.evaluate("S.sessionId")
        pg.context.close()
    OUT["layout"] = res


def tabs(r: Run):
    pg = r.page(_variant(), width=1400, height=900)
    pg.evaluate("openSettings('about')")
    pg.wait_for_timeout(200)
    OUT["tabs_desktop"] = pg.evaluate("""() => { const t = document.querySelector('.settings-tabs'), pr = document.getElementById('settings-panel').getBoundingClientRect();
        const tr = t.getBoundingClientRect();
        const tabs = [...t.querySelectorAll('[role=tab]')];
        return {overflow: t.scrollWidth - t.clientWidth,
                allInside: tabs.every(b => { const r = b.getBoundingClientRect(); return r.left >= tr.left - 1 && r.right <= tr.right + 1 && r.left >= pr.left && r.right <= pr.right; }),
                rows: new Set(tabs.map(b => Math.round(b.getBoundingClientRect().top))).size,
                role: t.getAttribute('role'), n: tabs.length,
                selected: tabs.filter(b => b.getAttribute('aria-selected') === 'true').map(b => b.dataset.tab),
                tabStops: tabs.filter(b => b.tabIndex === 0).map(b => b.dataset.tab),
                panel: (() => { const p = document.getElementById('tab-about'); return [p.getAttribute('role'), p.getAttribute('aria-labelledby')]; })()}; }""")
    # keyboard: arrows move between tabs and open them
    pg.wait_for_timeout(250)
    pg.focus("#stab-about")
    pg.keyboard.press("ArrowLeft")
    OUT["tabs_arrow"] = pg.evaluate("[document.activeElement.dataset.tab, document.querySelector('.stab.active').dataset.tab, document.getElementById('tab-logs').classList.contains('active')]")
    pg.keyboard.press("Home")
    OUT["tabs_home"] = pg.evaluate("document.activeElement.dataset.tab")
    r.shot(pg, "tabs_desktop")
    pg.context.close()
    pg = r.page(_variant(), width=375, height=760)
    out = {}
    for tab in ("about", "analytics", "status"):
        pg.evaluate(f"openSettings('{tab}')")
        pg.wait_for_timeout(250)
        out[tab] = pg.evaluate("""() => { const t = document.querySelector('.settings-tabs'), a = t.querySelector('.stab.active');
            const tr = t.getBoundingClientRect(), ar = a.getBoundingClientRect();
            return {inView: ar.left >= tr.left - 1 && ar.right <= tr.right + 1, scrollable: t.scrollWidth > t.clientWidth,
                    fades: [t.classList.contains('more-left'), t.classList.contains('more-right')],
                    mask: getComputedStyle(t).webkitMaskImage || getComputedStyle(t).maskImage}; }""")
        pg.evaluate("closeSettings(true)")
    OUT["tabs_phone"] = out
    pg.evaluate("openSettings('about')")
    pg.wait_for_timeout(250)
    r.shot(pg, "tabs_phone_about")
    pg.context.close()


def keyboard_and_storage(r: Run):
    pg = r.page(_variant(inst=INST + [{"name": "kb", "chunks": 5, "color": "#f59e0b", "redis_endpoint": "team",
                                       "reachable": True, "enabled": True}]))
    pg.evaluate("openSettings('rag')")
    pg.wait_for_timeout(250)      # openSettings moves focus to its close button on the next tick
    pg.evaluate("""() => { window.__clicked = [];
        const orig = HTMLInputElement.prototype.click;
        HTMLInputElement.prototype.click = function(){ window.__clicked.push(this.id || this.closest('[id]')?.id); }; }""")
    pg.focus("#ingest-drop")
    pg.keyboard.press("Enter")
    pg.focus("#rag-import-1")
    pg.keyboard.press(" ")
    OUT["kb_clicks"] = pg.evaluate("window.__clicked")
    OUT["kb_drop_role"] = pg.evaluate("[document.getElementById('ingest-drop').getAttribute('role'), document.getElementById('ingest-drop').tabIndex]")
    pg.focus("#rag-pick-1")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(100)
    OUT["kb_card"] = pg.evaluate("""({active: S.activeRag.name, focus: document.activeElement.id,
        pressed: document.getElementById('rag-pick-1').getAttribute('aria-pressed'),
        cardIsPlain: !document.getElementById('rag-card-1').hasAttribute('role')})""")
    pg.evaluate("closeSettings(true)")
    pg.context.close()

    # B13: the connection holding the app data offers no Remove
    storage = {"app": "team", "default_type": "redis", "connections": [
        {"name": "default", "type": "redis", "builtin": True, "label": "Built-in Redis", "host": "127.0.0.1:6389",
         "ok": True, "vector_search": True, "is_app": False, "instances": 1},
        {"name": "team", "type": "redis", "builtin": False, "label": "team", "host": "10.0.0.5:6380",
         "ok": True, "vector_search": True, "is_app": True, "instances": 1},
        {"name": "spare", "type": "redis", "builtin": False, "label": "spare", "host": "10.0.0.6:6379",
         "ok": True, "vector_search": True, "is_app": False, "instances": 0}]}
    eps = [{"name": "default", "primary": True, "host": "127.0.0.1", "port": 6389, "type": "redis", "builtin": True},
           {"name": "team", "type": "redis", "host": "10.0.0.5", "port": 6380},
           {"name": "spare", "type": "redis", "host": "10.0.0.6", "port": 6379}]
    pg = r.page(_variant(storage=storage, eps=eps,
                         inst=INST + [{"name": "kb", "chunks": 5, "color": "#f59e0b", "redis_endpoint": "team",
                                       "reachable": True, "enabled": True}]))
    pg.evaluate("openSettings('redis')")
    _wait(pg, "() => document.querySelectorAll('#redis-endpoints-list .glass-card').length >= 2", 5)
    OUT["ep_remove"] = pg.evaluate("""() => ({removable: [...document.querySelectorAll('[data-ep-del]')].map(b => b.dataset.epDel),
        locked: [...document.querySelectorAll('[data-ep-locked]')].map(b => [b.dataset.epLocked, b.getAttribute('aria-disabled'), b.title])})""")
    pg.wait_for_timeout(600)      # the list re-renders as /api/storage and the status probes land
    pg.click("[data-ep-locked='team']", force=True)
    pg.wait_for_timeout(100)
    OUT["ep_locked_click"] = {"modal": _modal(pg), "toasts": _toasts(pg)}
    pg.click("[data-ep-del='spare']")
    _wait(pg, "() => document.getElementById('modal-overlay').classList.contains('open')", 3)
    OUT["ep_spare_modal"] = _modal(pg)
    pg.evaluate("closeModal()")
    # B20: an error from the add-connection form goes once the connection is added
    pg.get_by_role("button", name="＋ Add connection").click()
    pg.locator("#add-redis-endpoint-form button", has_text="Add connection").click()
    OUT["ep_error_first"] = _toasts(pg)
    pg.fill("#new-ep-name", "third")
    pg.fill("#new-ep-host", "10.0.0.7")
    pg.evaluate("document.getElementById('new-ep-autodiscover').checked = false")
    pg.locator("#add-redis-endpoint-form button", has_text="Add connection").click()
    _wait(pg, "() => [...document.querySelectorAll('#toast-container .toast')].some(t => t.dataset.toastType === 'success')", 4)
    pg.wait_for_timeout(400)
    OUT["ep_error_after_success"] = _toasts(pg)
    pg.evaluate("closeSettings(true)")
    pg.context.close()

    # F3: the chat view says when the store holding conversations is down
    pg = r.page(_variant(redisDown=True))
    _wait(pg, "() => !document.getElementById('storage-down-note').hidden", 4)
    OUT["storage_down"] = pg.evaluate("""() => { const n = document.getElementById('storage-down-note');
        return {hidden: n.hidden, display: getComputedStyle(n).display, h: n.getBoundingClientRect().height, text: n.innerText}; }""")
    pg.click("#storage-down-note button")
    OUT["storage_down_link"] = pg.evaluate("document.querySelector('.stab.active')?.dataset.tab")
    pg.evaluate("closeSettings(true)")
    r.shot(pg, "storage_down")
    pg.context.close()
    pg = r.page(_variant())
    OUT["storage_up"] = pg.evaluate("getComputedStyle(document.getElementById('storage-down-note')).display")
    pg.context.close()


def sessions_pins_feedback(r: Run):
    msgs = {"s_old": [{"role": "user", "content": "What is the hotel limit?"},
                      {"role": "assistant", "content": "The limit is 150 EUR.", "meta": {"provider": "ollama", "model": "llama3:latest"}}],
            "s_new": [{"role": "user", "content": "Newest question"},
                      {"role": "assistant", "content": "Newest answer.", "meta": {"provider": "ollama", "model": "llama3:latest"}}]}
    sessions = [{"id": "s_new", "messages": 2, "preview": "Newest answer.", "title": "Travel policy", "updated": 2000},
                {"id": "s_old", "messages": 2, "preview": "The limit is 150 EUR.", "title": None, "updated": 1000}]
    pg = r.page(_variant(sessions=sessions, sessionMsgs=msgs, mine=["s_old", "s_new"]))
    OUT["session_rows"] = pg.evaluate("[...document.querySelectorAll('#sessions-list .session-row')].map(r => r.dataset.sid + ':' + r.querySelector('div > div').innerText.trim())")
    # C1: reopening a stored conversation shows it without the welcome hero above it; the
    # restore scrolls once, not once per message (each scroll forced a layout)
    pg.evaluate("async () => { await hydrateSession('s_old'); const f = scrollToBottom; window.__scrolls = 0;"
                " window.scrollToBottom = function () { window.__scrolls++; return f.apply(this, arguments); }; }")
    pg.evaluate("async () => { await switchSession('s_old'); }")
    OUT["reopen_scrolls"] = pg.evaluate("window.__scrolls")
    # clicking the conversation that is already open: no new socket, no reconnect loop
    _wait(pg, "() => S.ws && S.ws.readyState === 1", 3)
    pg.evaluate("window.__sockBefore = window.__sockets.length")
    pg.click("#sessions-list [data-sid='s_old']")
    pg.wait_for_timeout(2600)
    OUT["active_click_sockets"] = pg.evaluate("[window.__sockets.length - window.__sockBefore, S.ws.readyState]")
    # a socket replaced on purpose must not schedule its own reconnect when it closes
    pg.evaluate("window.__sockBefore = window.__sockets.length; window.__oldSock = S.ws; connectWS(S.sessionId);")
    pg.wait_for_timeout(2600)
    OUT["replaced_socket_reconnects"] = pg.evaluate("window.__sockets.length - window.__sockBefore")
    # a late frame from the replaced socket must not reach this conversation
    OUT["replaced_socket_frame"] = pg.evaluate("""() => { const n = document.querySelectorAll('#chat-area .msg-wrap').length;
        window.__oldSock._msg({type:'stream_start'}); window.__oldSock._msg({type:'token', content:'stale'});
        const after = document.querySelectorAll('#chat-area .msg-wrap').length;
        return {added: after - n, streaming: S.streaming}; }""")
    OUT["reopen"] = pg.evaluate("""() => ({hero: !!document.querySelector('#chat-area .welcome-screen'),
        messages: document.querySelectorAll('#chat-area .msg-wrap').length,
        firstTop: document.querySelector('#chat-area .msg-wrap')?.getBoundingClientRect().top - document.getElementById('chat-area').getBoundingClientRect().top})""")
    # C6: a rating credits the model that wrote the answer, not the one selected now
    answer = pg.evaluate("S.sessions['s_old'].messages.find(m => m.role === 'assistant').meta.id")
    pg.evaluate("S.provider = 'claude'; S.currentModel = 'claude-sonnet-4-6'; S.activeRag = {name:'other', ep:'default'};")
    pg.evaluate(f"async () => {{ await thumbs('{answer}', -1, document.querySelector('#{answer} .msg-action-btn')); }}")
    fb = pg.evaluate("window.__posts.filter(p => p.url === '/api/feedback').pop().body")
    OUT["feedback"] = {k: fb.get(k) for k in ("provider", "model", "rag_instances", "query")}
    pg.evaluate("S.provider = 'ollama'; S.currentModel = 'llama3:latest';")
    # C5: a pin from another conversation opens that conversation
    pg.evaluate(f"() => pinMsg('{answer}', document.querySelector('#{answer} .msg-action-btn'))")
    pg.evaluate("async () => { await switchSession('s_new'); }")
    pg.evaluate("async () => { await openPin(0); }")
    pg.wait_for_timeout(300)
    OUT["pin"] = pg.evaluate(f"({{session: S.sessionId, present: !!document.getElementById('{answer}')}})")
    pg.context.close()
    # an older server: no titles, no times — the order it always had
    old = [{"id": "s_old", "messages": 2, "preview": "The limit is 150 EUR."},
           {"id": "s_new", "messages": 2, "preview": "Newest answer."}]
    pg = r.page(_variant(sessions=old, sessionMsgs=msgs, mine=["s_old", "s_new"]))
    OUT["session_rows_old_server"] = pg.evaluate("[...document.querySelectorAll('#sessions-list .session-row')].map(r => r.dataset.sid + ':' + r.querySelector('div > div').innerText.trim())")
    pg.context.close()


def attachments_and_voice(r: Run):
    pg = r.page(_variant(uploadDelay={"slow.txt": 600}, speech="remote"))
    # C3 images: add A, B, C; remove A then C — only B is left, on screen and in the queue
    pg.evaluate("""() => { ['A','B','C'].forEach(x => addImagePreview('data:image/gif;base64,R0lGODlhAQABAAAAACw=#' + x)); }""")
    pg.locator("#img-preview-row .img-thumb-remove").nth(0).click()
    pg.locator("#img-preview-row .img-thumb-remove").nth(1).click()
    OUT["images_left"] = pg.evaluate("({queued: S.pendingImages.map(u => u.split('#')[1]), shown: document.querySelectorAll('#img-preview-row .img-thumb').length})")
    pg.evaluate("S.pendingImages = []; document.getElementById('img-preview-row').innerHTML = '';")
    # C3 files: remove the first while the second is still uploading
    pg.evaluate("""() => { const dt = new DataTransfer();
        ['one.txt','slow.txt','three.txt'].forEach(n => dt.items.add(new File(['x'], n)));
        const inp = document.getElementById('doc-file-input'); inp.files = dt.files; window.__up = handleDocFile(inp); }""")
    _wait(pg, "() => S.pendingFiles.length === 2 && S.pendingFiles[0].text !== null", 3, 30)
    pg.locator("#file-preview-row .file-chip-remove").nth(0).click()
    pg.evaluate("async () => { await window.__up; }")
    OUT["files_left"] = pg.evaluate("({queued: S.pendingFiles.map(f => f.name + '=' + f.text), shown: [...document.querySelectorAll('#file-preview-row .file-chip-name')].map(e => e.innerText)})")
    pg.locator("#file-preview-row .file-chip-remove").nth(1).click()
    OUT["files_after_remove_last"] = pg.evaluate("S.pendingFiles.map(f => f.name)")
    # C3 drag and drop: no image reaches a model without vision
    pg.evaluate("""() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'pic.png', {type:'image/png'}));
        handleDrop({preventDefault(){}, dataTransfer: dt}); }""")
    pg.wait_for_timeout(200)
    OUT["drop_no_vision"] = {"queued": pg.evaluate("S.pendingImages.length"), "toasts": _toasts(pg)}
    # C7 voice: the language is set, phrases are spaced, the tooltip says where audio goes
    OUT["voice"] = pg.evaluate("""() => { const rec = window.__recognisers[0];
        document.getElementById('msg-input').value = 'hello';
        rec.onresult({results:[[{transcript:'world'}]]}); rec.onresult({results:[[{transcript:' again '}]]});
        return {lang: rec.lang, nav: navigator.language, value: document.getElementById('msg-input').value,
                title: document.getElementById('mic-btn').title, local: rec.processLocally,
                asked: window.__availableAsked || null}; }""")
    pg.context.close()


XSS_IMG = '<img src=x onerror="window.__xss=(window.__xss||[]).concat(\'%s\')">'
XSS_COLOR = '#f00"><img src=x onerror="window.__xss=(window.__xss||[]).concat(\'color\')">'


def xss(r: Run):
    """Stored values that reach markup: instance name/colour/created date, template name,
    session title, connection name and host, server error and version texts."""
    bad = lambda tag: XSS_IMG % tag   # noqa: E731
    inst = [{"name": "default", "chunks": 3, "color": XSS_COLOR, "redis_endpoint": "default",
             "reachable": True, "enabled": True, "created": bad("created")},
            {"name": bad("name"), "chunks": 1, "color": "#abc", "redis_endpoint": "default",
             "reachable": True, "enabled": True}]
    storage = {"app": "default", "default_type": "redis", "connections": [
        dict(STORAGE["connections"][0]),
        {"name": bad("conn"), "type": "redis", "builtin": False, "label": bad("label"), "host": bad("host"),
         "ok": False, "vector_search": None, "is_app": False, "instances": 0, "error": bad("error")}]}
    eps = [{"name": "default", "primary": True, "host": "127.0.0.1", "port": 6389, "type": "redis", "builtin": True},
           {"name": bad("conn"), "type": "redis", "host": bad("host"), "port": 6379}]
    v = _variant(inst=inst, storage=storage, eps=eps,
                 templates=[{"name": bad("template"), "system": "x"}],
                 sessions=[{"id": "s_x", "messages": 2, "preview": bad("preview"), "title": bad("title"), "updated": 5}],
                 mine=["s_x"],
                 ollamaStatus={"ok": False, "error": bad("ollama-error")},
                 ragStats=[{"name": "default", "queries": 2, "hits": 1, "hit_rate": .5, "avg_top_score": .6,
                            "avg_best_raw_score": .6, "avg_chunks": 2}],
                 allStats=[{"name": "default", "ok": True, "host": "127.0.0.1", "port": 6389, "version": bad("version"),
                            "role": bad("role"), "rdb_last_save_status": bad("rdb"), "cluster_enabled": True,
                            "cluster_state": bad("cstate"), "cluster_known_nodes": bad("nodes"),
                            "mem_used": bad("mem"), "mem_peak": bad("peak"), "mem_rss": bad("rss"),
                            "connected_clients": bad("clients"), "uptime_days": bad("uptime"), "ops_per_sec": bad("ops"),
                            "total_keys": bad("keys"), "expired_keys": bad("expired"), "mode": bad("mode"),
                            "keyspace": [{"db": bad("db"), "keys": bad("dbkeys"), "expires": bad("expires")}],
                            "rag_instances": [{"name": "default", "chunks": bad("chunks"), "enabled": True}]}],
                 memory={"used": 1, "used_human": bad("used_human"), "peak_human": bad("peak_human"), "max": 0,
                         "max_human": bad("max_human")},
                 statusRedis={"ok": True, "version": "8", "memory_used": bad("memory_used"),
                              "connected_clients": bad("sclients"), "uptime_days": bad("sdays"), "search_available": True})
    pg = r.page(v)
    for tab in ("rag", "analytics", "status", "redis", "templates", "providers", "about"):
        pg.evaluate(f"openSettings('{tab}')")
        pg.wait_for_timeout(500)
        if tab == "providers":
            pg.evaluate("async () => { await testOllama(); }")
        pg.evaluate("closeSettings(true)")
    pg.wait_for_timeout(500)
    OUT["xss"] = pg.evaluate("""() => ({fired: window.__xss || [], injected: document.querySelectorAll('img[src="x"]').length,
        pillBg: getComputedStyle(document.querySelector('#rag-sidebar-list .rag-pill')).backgroundColor,
        dot: getComputedStyle(document.querySelector('#rag-sidebar-list button span')).backgroundColor,
        nameShown: document.getElementById('rag-sidebar-list').innerText.includes('<img src=x'),
        titleShown: document.getElementById('sessions-list').innerText.includes('<img src=x')})""")
    pg.context.close()


def storage_switch(r: Run):
    # the store the app data is on cannot be read: the copy fails, the dialog offers the way out
    storage = {"app": "default", "default_type": "redis", "connections": [
        dict(STORAGE["connections"][0]),
        {"name": "team", "type": "redis", "builtin": False, "label": "team", "host": "10.0.0.5:6380",
         "ok": True, "vector_search": True, "is_app": False, "instances": 0},
        {"name": "gone", "type": "redis", "builtin": False, "label": "gone", "host": "10.0.0.9:6379",
         "ok": False, "vector_search": None, "is_app": False, "instances": 0}]}
    eps = [{"name": "default", "primary": True, "host": "127.0.0.1", "port": 6389, "type": "redis", "builtin": True},
           {"name": "team", "type": "redis", "host": "10.0.0.5", "port": 6380},
           {"name": "gone", "type": "redis", "host": "10.0.0.9", "port": 6379}]
    pg = r.page(_variant(storage=storage, eps=eps, copyFail=True, epStatus={"gone": {"ok": False, "error": "refused"}}))
    pg.evaluate("openSettings('redis')")
    _wait(pg, "() => [...document.querySelectorAll('#storage-app-select option')].some(o => o.value === 'team')", 5)
    pg.select_option("#storage-app-select", "team")
    pg.click("#storage-app-btn")
    pg.locator("#modal-actions button", has_text="Move app data").click()
    _wait(pg, "() => document.getElementById('modal-overlay').classList.contains('open')", 5)
    OUT["switch_copy_failed"] = _modal(pg)
    pg.locator("#modal-actions button", has_text="Switch without copying").click()
    _wait(pg, "() => window.__posts.filter(p => p.url === '/api/storage/app').length >= 2", 5)
    pg.wait_for_timeout(200)
    OUT["switch_posts"] = pg.evaluate("window.__posts.filter(p => p.url === '/api/storage/app').map(p => p.body)")
    OUT["switch_toasts"] = _toasts(pg)
    # Remove on a connection that cannot be reached: it says so instead of "holds nothing"
    pg.click("[data-ep-del='gone']")
    _wait(pg, "() => document.getElementById('modal-overlay').classList.contains('open')", 3)
    OUT["remove_unreachable"] = _modal(pg)
    pg.evaluate("closeModal(); closeSettings(true)")
    pg.context.close()


def main(index: pathlib.Path, engine: str, shots) -> None:
    global INDEX
    INDEX = index
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    with sync_playwright() as p:
        try:
            r = Run(p, engine, shots)
        except Exception as exc:                  # noqa: BLE001 — engine not installed
            OUT["skip"] = f"{engine} not available: {exc}"[:300]
            return
        for step in (boot_and_models, welcome_chips_and_misc, dark_contrast, reset_and_discard,
                     ingest_crawl_create, websocket, layout, tabs, keyboard_and_storage,
                     sessions_pins_feedback, attachments_and_voice, xss, storage_switch):
            try:
                step(r)
            except Exception as exc:              # noqa: BLE001 — reported per step
                OUT.setdefault("step_errors", {})[step.__name__] = f"{type(exc).__name__}: {exc}"[:600]
        OUT["errors"] = r.errors[:8]
        OUT["ok"] = True
        r.browser.close()


INDEX = None

if __name__ == "__main__":
    shots = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else None
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    try:
        main(pathlib.Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv) > 2 else "chromium", shots)
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT, ensure_ascii=False, default=str))
