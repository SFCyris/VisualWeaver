# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drive Settings → Storage and the New Instance form in a real browser with canned API
answers (no server, no database). Prints one JSON object."""
import json
import pathlib
import sys

OUT: dict = {}

# window.__VARIANT (set by an earlier init script, see storage_choice_ui_probe.py) swaps
# in one install's connections and its /api/storage answer. Without it /api/storage is a
# 404 — an older server — and the page must render the Storage tab as it always did.
FAKE = r"""
(() => {
  const V = window.__VARIANT || {};
  const eps = V.eps || [
    {name:'default', primary:true, host:'127.0.0.1', port:6389, type:'redis'},
    {name:'team-redis', type:'redis', host:'10.0.0.5', port:6380, db:2, ssl:true},
    {name:'atlas', type:'mongodb', host:'cluster0.ab.mongodb.net', username:'alice', database:'kb', password:'__VISUALWEAVER_SECRET_KEPT__'},
    {name:'plain', type:'mongodb', host:'10.0.0.9:27017', database:'visualweaver'},
  ];
  const inst = V.inst || [
    {name:'default', chunks:3, redis_endpoint:'default', endpoint_type:'redis', reachable:true, enabled:true, color:'#7c6ff7'},
    {name:'kb', chunks:12, redis_endpoint:'atlas', endpoint_type:'mongodb', reachable:true, enabled:true, color:'#f59e0b'},
  ];
  window.__posts = [];
  const json = (b, s=200) => Promise.resolve(new Response(JSON.stringify(b), {status:s, headers:{'Content-Type':'application/json'}}));
  window.fetch = (url, opt={}) => {
    const u = String(url);
    if (opt.method === 'POST') window.__posts.push({url:u, body: opt.body ? JSON.parse(opt.body) : null});
    if (u.startsWith('/api/redis/endpoints') && !opt.method) return json(eps);
    if (u === '/api/rag/instances' && opt.method === 'POST') {
      const b = JSON.parse(opt.body);
      return b.endpoint === 'atlas' ? json({name:b.name, redis_endpoint:'atlas'}) : json({detail:'MongoDB \'plain\' has no vector search'}, 400);
    }
    if (u.startsWith('/api/rag/instances')) return json(inst);
    if (u === '/api/storage') {
      if (V.storageNetErr) return Promise.reject(new TypeError('Failed to fetch'));
      if (V.storageFail > 0) { V.storageFail--; return json({detail:'Internal Server Error'}, 500); }
      const answer = () => V.storage ? json(V.storage) : json({detail:'Not Found'}, 404);
      return V.storageDelay ? new Promise(res => setTimeout(res, V.storageDelay)).then(answer) : answer();
    }
    if (u === '/api/components') return V.components ? json(V.components) : json({detail:'Not Found'}, 404);
    if (u === '/api/storage/app' && opt.method === 'POST') {
      const to = JSON.parse(opt.body).connection;
      const refuse = (V.refuse || {})[to];
      return new Promise(res => setTimeout(res, 400)).then(() => {
        if (refuse) return json({detail: refuse}, 400);
        const same = V.storage.app === to;
        V.storage.app = to;
        V.storage.connections.forEach(c => { c.is_app = c.name === to; });
        return json({ok:true, app:to, copied: same ? null : {conversations:12, usage:true, stats:true, watch:40}});
      });
    }
    if (u === '/api/redis/all-stats' && V.allStats) return json(V.allStats);
    if (u === '/api/redis/memory' && V.memory) return json(V.memory);
    if (u === '/api/status/redis') {
      const answer = () => json(V.statusRedis || {ok:true, search_available:true, endpoint_health:{default:true}});
      return V.statusDelay ? new Promise(res => setTimeout(res, V.statusDelay)).then(answer) : answer();
    }
    if (u.startsWith('/api/status/redis/')) {
      const n = decodeURIComponent(u.split('/').pop());
      const sc = ((V.storage || {}).connections || []).find(c => c.name === n);
      if (n === 'down' || (sc && sc.ok === false)) return json({ok:false, error:'Connection refused'});
      return json(n === 'plain' ? {ok:true, search_available:false} : {ok:true, search_available:true});
    }
    if (u === '/api/redis/test') {
      const b = JSON.parse(opt.body);
      return json(b.uri && b.uri.includes('plain') ? {ok:true, version:'8.3.13', search_available:false}
                                                   : {ok:true, version:'8.0.15', search_available:true});
    }
    if (u.startsWith('/api/cache/stats')) return json({count:0, entries:[]});
    if (/templates|sessions|logs|feedback|sources|watch|schedul/.test(u)) return json([]);
    return json({});
  };
})();
"""


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1280, "height": 900})
        errors: list = []
        pg.on("pageerror", lambda e: errors.append((e.stack or str(e))[:400]))
        pg.add_init_script(FAKE)
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(500)
        pg.evaluate("openSettings('redis')")
        pg.evaluate("async()=>{await loadRedisEndpoints(); await loadRagInstances(); await checkLocalStatus();}")
        pg.wait_for_timeout(400)
        pg.evaluate("renderRedisEndpoints()")
        OUT["tab"] = pg.locator('.stab[data-tab="redis"]').inner_text()
        # an older server (no /api/storage): the tab is what it always was
        OUT["legacy"] = pg.evaluate("""()=>{const d=id=>getComputedStyle(document.getElementById(id)).display;
            return {storage:S.storage, title:document.getElementById('storage-builtin-title').innerText,
              banner:document.getElementById('storage-banner-title').innerText,
              form:d('redis-builtin-form'), mem:d('redis-mem-card'), app:d('storage-app-card'),
              mongoNotice:d('storage-banner-mongo'), builtinCards:document.querySelectorAll('#storage-builtin-list .glass-card').length};}""")
        OUT["cards"] = pg.evaluate("""()=>[...document.querySelectorAll('#redis-endpoints-list .glass-card')].map(c=>c.innerText.replace(/\\s+/g,' ').trim())""")
        # add form: type switch
        pg.get_by_role("button", name="＋ Add connection").click()
        disp = "()=>[getComputedStyle(document.getElementById('new-ep-redis-fields')).display, getComputedStyle(document.getElementById('new-ep-mongo-fields')).display]"
        OUT["fields_redis"] = pg.evaluate(disp)
        pg.locator('input[name="new-ep-type"][value="mongodb"]').check()
        OUT["fields_mongo"] = pg.evaluate(disp)
        pg.fill("#new-ep-name", "plain2")
        pg.fill("#new-ep-uri", "mongodb://plain:27017")
        pg.fill("#new-ep-user", "bob")
        pg.fill("#new-ep-mpass", "pw")
        pg.get_by_role("button", name="🔌 Test", exact=True).click()
        pg.wait_for_timeout(300)
        OUT["test_verdict"] = pg.locator("#new-ep-test-result").inner_text()
        OUT["test_post"] = [x for x in pg.evaluate("window.__posts") if x["url"] == "/api/redis/test"][-1]["body"]
        # server + username + password, as the Atlas UI lists them: the string is built
        preview = "()=>document.getElementById('new-ep-uri-preview').innerText"
        pg.fill("#new-ep-uri", "")
        pg.locator("#new-ep-uri").press_sequentially("cluster0.clmrcbn.mongodb.net")
        OUT["preview_atlas"] = pg.evaluate(preview)
        pg.fill("#new-ep-user", "scyris_db_user")
        pg.fill("#new-ep-mpass", "pw")
        pg.get_by_role("button", name="🔌 Test", exact=True).click()
        pg.wait_for_timeout(300)
        OUT["server_post"] = [x for x in pg.evaluate("window.__posts") if x["url"] == "/api/redis/test"][-1]["body"]
        pg.fill("#new-ep-uri", "")
        pg.locator("#new-ep-uri").press_sequentially("10.0.0.9:27017")
        OUT["preview_plain"] = pg.evaluate(preview)
        # an Atlas string pasted as Atlas shows it: credentials move to their fields
        pg.fill("#new-ep-user", "")
        pg.fill("#new-ep-mpass", "")
        pg.fill("#new-ep-uri", "mongodb+srv://scyris_db_user:<db_password>@cluster0.clmrcbn.mongodb.net/?compressors=zlib")
        pg.press("#new-ep-uri", "Tab")
        OUT["atlas_placeholder"] = pg.evaluate("""()=>[document.getElementById('new-ep-uri').value,
            document.getElementById('new-ep-user').value, document.getElementById('new-ep-mpass').value,
            document.activeElement.id, document.getElementById('new-ep-test-result').innerText]""")
        pg.fill("#new-ep-uri", "mongodb+srv://bob:p%40ss@c0.ab.mongodb.net/?retryWrites=true")
        pg.press("#new-ep-uri", "Tab")
        OUT["atlas_password"] = pg.evaluate("""()=>[document.getElementById('new-ep-uri').value,
            document.getElementById('new-ep-user').value, document.getElementById('new-ep-mpass').value]""")
        OUT["preview_pasted"] = pg.evaluate(preview)
        # a connection whose last check failed
        OUT["down_card"] = pg.evaluate("""()=>{const was=S.epOk; S.epOk={...(was||{}),atlas:false};
            renderRedisEndpoints(); const t=document.querySelectorAll('#redis-endpoints-list .glass-card')[1].innerText;
            S.epOk=was; renderRedisEndpoints(); return t.replace(/\\s+/g,' ').trim();}""")
        # new instance
        pg.evaluate("switchTab('rag')")
        pg.get_by_role("button", name="＋ New Instance").click()
        OUT["label"] = pg.locator('label[for="new-rag-endpoint"]').inner_text()
        OUT["options"] = pg.evaluate("[...document.querySelectorAll('#new-rag-endpoint option')].map(o=>[o.value,o.textContent,o.disabled])")
        OUT["instance_badge"] = pg.evaluate("""()=>[...document.querySelectorAll('.rag-inst-card .ep-badge')].map(e=>e.innerText)""")
        pg.fill("#new-rag-name", "kb2")
        pg.select_option("#new-rag-endpoint", "atlas")
        pg.get_by_role("button", name="Create", exact=True).click()
        pg.wait_for_timeout(300)
        OUT["create_post"] = [x for x in pg.evaluate("window.__posts") if x["url"] == "/api/rag/instances"][-1]["body"]
        # a refused creation reaches the user
        pg.get_by_role("button", name="＋ New Instance").click()
        pg.fill("#new-rag-name", "kb3")
        pg.evaluate("document.getElementById('new-rag-endpoint').innerHTML+='<option value=\"plain\">plain</option>'; document.getElementById('new-rag-endpoint').value='plain'")
        pg.get_by_role("button", name="Create", exact=True).click()
        pg.wait_for_timeout(300)
        OUT["refusal_toast"] = pg.evaluate("[...document.querySelectorAll('#toast-container *')].map(e=>e.innerText).join(' | ')")
        OUT["errors"] = errors[:5]
        OUT["ok"] = True
        b.close()


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]).resolve())
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT, ensure_ascii=False))
