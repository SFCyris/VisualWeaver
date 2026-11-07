# SPDX-License-Identifier: AGPL-3.0-or-later
"""Settings → Storage for the three installs (Redis, MongoDB, both) in a real browser with
canned API answers (the fake in storage_ui_probe.py, no server, no database): the Built-in
section, the App data & answer cache selector and its switch, the New Instance preselect,
and the Status / Analytics / Cache wording. Prints one JSON object.

    python storage_choice_ui_probe.py <index.html> [chromium|firefox|webkit] [screenshot-dir]
"""
import json
import pathlib
import sys
import time

from storage_ui_probe import FAKE

OUT: dict = {}

_ADDED = [
    {"name": "team-redis", "type": "redis", "host": "10.0.0.5", "port": 6380, "db": 2, "ssl": True},
    {"name": "atlas", "type": "mongodb", "host": "cluster0.ab.mongodb.net", "username": "alice", "database": "kb"},
    {"name": "plain", "type": "mongodb", "host": "10.0.0.9:27017", "database": "visualweaver"},
    {"name": "down", "type": "redis", "host": "10.0.0.7", "port": 6379},
]
_ADDED_CONN = [
    {"name": "team-redis", "type": "redis", "builtin": False, "label": "team-redis", "host": "10.0.0.5:6380",
     "ok": True, "vector_search": True, "is_app": False, "instances": 0},
    {"name": "atlas", "type": "mongodb", "builtin": False, "label": "atlas", "host": "cluster0.ab.mongodb.net",
     "ok": None, "vector_search": None, "is_app": False, "instances": 1},
    {"name": "plain", "type": "mongodb", "builtin": False, "label": "plain", "host": "10.0.0.9:27017",
     "ok": True, "vector_search": False, "is_app": False, "instances": 0},
    {"name": "down", "type": "redis", "builtin": False, "label": "down", "host": "10.0.0.7:6379",
     "ok": False, "vector_search": None, "is_app": False, "instances": 0},
]
_INST = [{"name": "default", "chunks": 3, "redis_endpoint": "default", "reachable": True, "enabled": True, "color": "#7c6ff7"},
         {"name": "kb", "chunks": 12, "redis_endpoint": "atlas", "endpoint_type": "mongodb", "reachable": True,
          "enabled": True, "color": "#f59e0b"}]


_LIBS = [{"name": "redis-py", "version": "6.4.0"}, {"name": "redisvl", "version": "0.8.2"}, {"name": "pymongo", "version": "4.15.1"}]
_C_REDIS = {"connection": "default", "label": "Built-in Redis", "type": "redis", "builtin": True, "ok": True,
            "product": "Redis", "version": "8.8.1", "search": "Redis Query Engine 8.8.1"}
_C_MONGO_DEFAULT = {"connection": "default", "label": "Built-in MongoDB", "type": "mongodb", "builtin": True, "ok": True,
                    "product": "MongoDB Community", "version": "9.0.4", "search": "MongoDB Search 1.78.0"}
_C_MONGO_LOCAL = dict(_C_MONGO_DEFAULT, connection="local-mongodb")
_C_ADDED = [
    {"connection": "team-redis", "label": "team-redis", "type": "redis", "builtin": False, "ok": True,
     "product": "Redis", "version": "7.4.2", "search": "RediSearch 2.10.5"},
    {"connection": "atlas", "label": "atlas", "type": "mongodb", "builtin": False, "ok": False, "product": "MongoDB",
     "version": None, "search": None, "error": "Login failed: bad auth : authentication failed"},
]


def _components(storage: list, containers) -> dict:
    return {"app": {"name": "VisualWeaver", "version": "1.14.0", "python": "3.11.9"}, "storage": storage,
            "libraries": _LIBS, "runtime": {"containers": containers}}


def _variant(kind: str) -> dict:
    b_redis = {"name": "default", "type": "redis", "builtin": True, "label": "Built-in Redis", "host": "127.0.0.1:6389",
               "ok": True, "vector_search": True, "is_app": True, "instances": 1, "version": "8.8.1"}
    b_mongo_default = {"name": "default", "type": "mongodb", "builtin": True, "label": "Built-in MongoDB",
                       "host": "127.0.0.1:27019", "ok": True, "vector_search": True, "is_app": True, "instances": 1}
    b_mongo_local = {"name": "local-mongodb", "type": "mongodb", "builtin": True, "label": "Built-in MongoDB",
                     "host": "127.0.0.1:27019", "ok": True, "vector_search": True, "is_app": False, "instances": 0}
    refuse = {"atlas": "MongoDB 'atlas' cannot be reached: connection timed out"}
    memory = {"used": 2_202_009, "used_human": "2.10M", "peak": 2_621_440, "peak_human": "2.50M", "max": 0, "max_human": "0B"}
    status_redis = {"ok": True, "version": "8.2.1", "mode": "standalone", "memory_used": "2.1M", "connected_clients": 3,
                    "uptime_days": 4, "search_available": True, "endpoint_health": {"default": True}}
    redis_stats = [{"name": "default", "ok": True, "host": "127.0.0.1", "port": 6389, "db": 0, "version": "8.2.1",
                    "search_available": True, "mem_used": "2.1M", "mem_peak": "2.5M", "mem_rss": "9M",
                    "connected_clients": 3, "ops_per_sec": 1, "total_keys": 42, "uptime_days": 4, "keyspace": [],
                    "rag_instances": [{"name": "default", "chunks": 3, "enabled": True}]}]
    if kind == "redis":
        return {"eps": [{"name": "default", "primary": True, "host": "127.0.0.1", "port": 6389, "type": "redis",
                         "builtin": True}] + _ADDED,
                "inst": _INST, "refuse": refuse, "statusRedis": status_redis, "allStats": redis_stats, "memory": memory,
                "components": _components([_C_REDIS] + _C_ADDED, None),
                "storage": {"app": "default", "default_type": "redis", "connections": [b_redis] + _ADDED_CONN}}
    if kind == "mongodb":
        return {"eps": [{"name": "default", "primary": True, "type": "mongodb", "host": "127.0.0.1:27019",
                         "builtin": True}] + _ADDED,
                "inst": _INST, "refuse": refuse,
                "components": _components([_C_MONGO_DEFAULT] + _C_ADDED, "docker (colima)"),
                # what /api/status/redis answers for a built-in MongoDB
                "statusRedis": {"ok": True, "type": "mongodb", "version": "9.0.4", "search_available": True,
                                "endpoint_health": {"default": True}},
                "allStats": [{"name": "default", "type": "mongodb", "host": "127.0.0.1:27019", "database": "visualweaver",
                              "ok": True, "version": "9.0.4", "data_size": 2_097_152, "storage_size": 4_194_304,
                              "objects": 1234, "instances": [{"name": "default", "chunks": 3, "enabled": True}]}],
                "storage": {"app": "default", "default_type": "mongodb", "connections": [b_mongo_default] + _ADDED_CONN}}
    return {"eps": [{"name": "default", "primary": True, "host": "127.0.0.1", "port": 6389, "type": "redis", "builtin": True},
                    {"name": "local-mongodb", "type": "mongodb", "host": "127.0.0.1:27019", "database": "visualweaver",
                     "builtin": True}] + _ADDED,
            "inst": _INST, "refuse": refuse, "statusRedis": status_redis, "memory": memory,
            "components": _components([_C_REDIS, _C_MONGO_LOCAL] + _C_ADDED, "docker (colima)"),
            "allStats": redis_stats + [{"name": "local-mongodb", "type": "mongodb", "host": "127.0.0.1:27019",
                                        "database": "visualweaver", "ok": True, "version": "9.0.4", "data_size": 0,
                                        "storage_size": 0, "objects": 0, "instances": []}],
            "storage": {"app": "default", "default_type": "redis",
                        "connections": [b_redis, b_mongo_local] + _ADDED_CONN}}


_SNAP = """()=>{
  const vis=el=>!!el&&getComputedStyle(el).display!=='none'&&el.getBoundingClientRect().height>0;
  const d=id=>getComputedStyle(document.getElementById(id)).display;
  const txt=el=>el?el.innerText.replace(/\\s+/g,' ').trim():'';
  return {
    banner: txt(document.getElementById('storage-banner-title')),
    bannerNeutral: document.getElementById('storage-banner').classList.contains('neutral'),
    mongoNotice: vis(document.getElementById('storage-banner-mongo')),
    redisNotice: document.getElementById('tab-redis').innerText.includes('registered trademark of Redis Ltd'),
    title: txt(document.getElementById('storage-builtin-title')),
    builtins: [...document.querySelectorAll('#storage-builtin-list .glass-card')].map(txt),
    form: d('redis-builtin-form'), mem: d('redis-mem-card'), app: d('storage-app-card'),
    formTitle: d('redis-builtin-form-title'),
    appOptions: [...document.querySelectorAll('#storage-app-select option')].map(o=>[o.value,o.textContent,o.disabled]),
    appValue: document.getElementById('storage-app-select').value,
    appBtn: [document.getElementById('storage-app-btn').innerText, document.getElementById('storage-app-btn').disabled],
    appHint: txt(document.getElementById('storage-app-hint')),
    cards: [...document.querySelectorAll('#redis-endpoints-list .glass-card')].map(txt),
    removable: [...document.querySelectorAll('#tab-redis [data-ep-del]')].map(b=>b.dataset.epDel),
    tabText: document.getElementById('tab-redis').innerText,
  };
}"""

_MEASURE = """()=>{
  const m=el=>{const r=el.getBoundingClientRect(), cs=getComputedStyle(el);
    return {w:Math.round(r.width), h:Math.round(r.height), display:cs.display, color:cs.color, bg:cs.backgroundColor,
            border:cs.borderTopColor, text:el.innerText.trim()};};
  return {
    pills: [...document.querySelectorAll('#storage-builtin-list .status-pill, #redis-endpoints-list .status-pill')].map(m),
    appCard: m(document.getElementById('storage-app-card')),
    appSelect: m(document.getElementById('storage-app-select')),
    banner: m(document.getElementById('storage-banner')),
    bannerTitleSpans: [...document.querySelectorAll('#storage-banner-title span')].map(m),
  };
}"""


# Headless file:// pages have no usable clipboard: record what would be copied.
_CLIPBOARD = """Object.defineProperty(navigator, 'clipboard', {configurable:true,
  value:{writeText:t=>{window.__copied=t;return Promise.resolve();}}});"""

_ABOUT = """()=>{const txt=el=>el?el.innerText.replace(/\\s+/g,' ').trim():'';
  return {rows:[...document.querySelectorAll('#about-storage tbody tr')].map(txt),
    app:txt(document.getElementById('about-app')), libs:txt(document.getElementById('about-libs')),
    hint:txt(document.querySelector('#about-content > .form-hint')),
    err:[...document.querySelectorAll('#about-storage .about-err')].map(txt)};}"""


def _toasts(pg) -> list:
    return pg.evaluate("[...document.querySelectorAll('#toast-container .toast')].map(e=>e.innerText.replace(/\\s+/g,' ').trim())")


def _wait(pg, js: str, timeout: float = 5.0):
    """Poll a page expression from Python — the page's CSP blocks string-eval, so
    Playwright's wait_for_function cannot be used here."""
    end = time.time() + timeout
    while time.time() < end:
        v = pg.evaluate(js)
        if v:
            return v
        pg.wait_for_timeout(50)
    return pg.evaluate(js)


def _open_storage(pg):
    pg.evaluate("openSettings('redis')")
    pg.evaluate("async()=>{await loadRedisEndpoints(); await loadRagInstances(); await checkLocalStatus(); await loadStorage();}")
    pg.wait_for_timeout(300)                  # the per-connection probes are fire-and-forget
    pg.evaluate("renderStorage()")


def _shoot(pg, shots, name):
    if not shots:
        return
    pg.evaluate("document.getElementById('tab-redis').scrollIntoView()")
    pg.locator("#tab-redis").screenshot(path=str(shots / f"{name}.png"))


def run_variant(p, engine: str, index: pathlib.Path, kind: str, shots) -> dict:
    out: dict = {}
    b = getattr(p, engine).launch()
    pg = b.new_page(viewport={"width": 1280, "height": 2400})
    errors: list = []
    pg.on("pageerror", lambda e: errors.append((e.stack or str(e))[:400]))
    pg.add_init_script("window.__VARIANT = " + json.dumps(_variant(kind)) + ";")
    pg.add_init_script(_CLIPBOARD)
    pg.add_init_script(FAKE)
    pg.goto(index.as_uri(), wait_until="domcontentloaded")
    pg.wait_for_timeout(600)
    out["boot_storage"] = pg.evaluate("S.storage && S.storage.default_type")   # loaded at start, not only in Settings
    _open_storage(pg)
    out["snap"] = pg.evaluate(_SNAP)
    for theme in ("light", "dark"):
        pg.evaluate(f"applyTheme('{theme}')")
        pg.wait_for_timeout(150)
        out[f"measure_{theme}"] = pg.evaluate(_MEASURE)
        _shoot(pg, shots, f"storage-{kind}-{theme}-{engine}")
    pg.evaluate("applyTheme('light')")
    # a server with versions on /api/storage but no /api/components
    out["storage_version_only"] = pg.evaluate("(()=>{const c=S.components;S.components=null;const v=_versionLine('default','redis');S.components=c;return v;})()")
    out["cards_versions"] = pg.evaluate("[...document.querySelectorAll('#tab-redis .ep-version')].map(e=>[e.closest('[data-builtin]')?.dataset.builtin||e.closest('.glass-card').innerText.split(/\\s/)[0],e.innerText])")

    # About: components and versions, and the plain-text copy
    pg.evaluate("switchTab('about')")
    _wait(pg, "!!document.getElementById('about-storage')")
    out["about"] = pg.evaluate(_ABOUT)
    pg.click("#about-copy-btn")
    _wait(pg, "!!window.__copied")
    out["copied"] = pg.evaluate("window.__copied")
    out["copy_btn"] = pg.evaluate("document.getElementById('about-copy-btn').innerText")
    out["copy_redacted"] = pg.evaluate("""(()=>{const keep=S.components;S.components=JSON.parse(JSON.stringify(keep));
        S.components.storage.find(x=>x.connection==='atlas').error='ac-x-shard-00-01.y.mongodb.net:27017, mongo2.lan:27017, [2001:db8::5]:27017, mongo2:27017 and 10.0.0.9:27017 timed out for user alice';
        const t=_componentsText();S.components=keep;return t;})()""")
    out["saves_redis_section"] = pg.evaluate("'redis' in _collectSettings()")
    for theme in ("light", "dark"):
        pg.evaluate(f"applyTheme('{theme}')")
        pg.wait_for_timeout(150)
        out[f"about_measure_{theme}"] = pg.evaluate("""()=>[...document.querySelectorAll('#about-storage .status-pill, #about-copy-btn')].map(el=>{
            const r=el.getBoundingClientRect(),cs=getComputedStyle(el);return {text:el.innerText.trim(),w:Math.round(r.width),h:Math.round(r.height),display:cs.display,color:cs.color,bg:cs.backgroundColor};})""")
        if shots:
            pg.locator("#tab-about").screenshot(path=str(shots / f"about-{kind}-{theme}-{engine}.png"))
    pg.evaluate("applyTheme('light')")
    # the welcome line leads here
    pg.evaluate("closeSettings(); S.appInfo={version:'1.14.0',license:'AGPL-3.0-or-later'}; _fillWelcomeVersion()")
    out["welcome_chip"] = pg.evaluate("(()=>{const b=document.querySelector('#welcome-version .wv-about');return b&&[b.innerText,getComputedStyle(b).display,Math.round(b.getBoundingClientRect().width)];})()")
    if out["welcome_chip"]:
        pg.click("#welcome-version .wv-about")
        pg.wait_for_timeout(200)
        out["welcome_chip_opens"] = pg.evaluate("document.getElementById('tab-about').classList.contains('active') && document.getElementById('settings-overlay').classList.contains('open')")
        pg.evaluate("closeSettings()")
    _open_storage(pg)

    # New Instance: the app-data store is preselected; built-ins go by their label
    pg.evaluate("switchTab('rag')")
    pg.evaluate("showCreateRag()")
    out["new_instance"] = pg.evaluate("""()=>{const s=document.getElementById('new-rag-endpoint');
        return {value:s.value, options:[...s.options].map(o=>[o.value,o.textContent,o.disabled])};}""")
    # Status, Analytics and Cache wording
    pg.evaluate("switchTab('status')")
    out["status_card"] = _wait(pg, "(()=>{const e=document.getElementById('status-storage-card');return e&&e.innerText.replace(/\\s+/g,' ').trim();})()")
    pg.evaluate("switchTab('analytics')")
    out["analytics"] = _wait(pg, "(()=>{const e=document.getElementById('analytics-content');const t=e.innerText;return t.includes('Storage Servers')&&t.replace(/\\s+/g,' ');})()")
    pg.evaluate("switchTab('cache')")
    out["cache_hint"] = pg.evaluate("document.getElementById('cache-store-hint').innerText")
    pg.evaluate("S.searchAvail[_appConn()]=false; _applySearchWarnings()")
    out["cache_warn"] = pg.evaluate("[getComputedStyle(document.getElementById('cache-search-warn')).display, document.getElementById('cache-search-warn').innerText]")
    pg.evaluate("S.searchAvail[_appConn()]=true; _applySearchWarnings()")

    if kind == "redis":
        pg.evaluate("switchTab('redis')")
        _wait(pg, "!!(S.storage&&document.getElementById('storage-app-select').options.length)")
        pg.evaluate("renderStorage()")
        # switch to team-redis: ask first, then copy, then the new current one shows
        pg.select_option("#storage-app-select", "team-redis")
        out["before_confirm"] = pg.evaluate(_SNAP)
        pg.click("#storage-app-btn")
        out["modal"] = pg.evaluate("""()=>[document.getElementById('modal-overlay').classList.contains('open'),
            document.getElementById('modal-title').innerText, document.getElementById('modal-body').innerText.replace(/\\s+/g,' '),
            [...document.querySelectorAll('#modal-actions button')].map(b=>b.innerText)]""")
        out["posts_before_confirm"] = [x for x in pg.evaluate("window.__posts") if x["url"] == "/api/storage/app"]
        pg.locator("#modal-actions button", has_text="Move app data").click()
        pg.wait_for_timeout(100)
        out["busy"] = pg.evaluate("""()=>[document.getElementById('storage-app-btn').innerText,
            document.getElementById('storage-app-btn').disabled, document.getElementById('storage-app-select').disabled]""")
        _wait(pg, "!S.storageBusy && S.storage.app==='team-redis'")
        pg.wait_for_timeout(200)
        out["switch_post"] = [x for x in pg.evaluate("window.__posts") if x["url"] == "/api/storage/app"][-1]["body"]
        out["switch_toasts"] = _toasts(pg)
        out["after_switch"] = pg.evaluate(_SNAP)
        pg.evaluate("switchTab('rag')")
        pg.evaluate("showCreateRag()")
        out["new_instance_after"] = pg.evaluate("document.getElementById('new-rag-endpoint').value")
        # a refused switch: the server's reason reaches the user, nothing changes
        pg.evaluate("switchTab('redis')")
        _wait(pg, "!!document.getElementById('storage-app-select').options.length")
        pg.select_option("#storage-app-select", "atlas")
        pg.click("#storage-app-btn")
        pg.locator("#modal-actions button", has_text="Move app data").click()
        _wait(pg, "!S.storageBusy && [...document.querySelectorAll('#toast-container .toast')].some(t=>t.innerText.includes('cannot be reached'))")
        out["refused_toasts"] = _toasts(pg)
        out["after_refusal"] = pg.evaluate(_SNAP)
        out["app_after_refusal"] = pg.evaluate("S.storage.app")
        # Cancel leaves everything as it was
        pg.select_option("#storage-app-select", "default")
        pg.click("#storage-app-btn")
        n = len([x for x in pg.evaluate("window.__posts") if x["url"] == "/api/storage/app"])
        pg.locator("#modal-actions button", has_text="Cancel").click()
        out["cancel"] = {"posts_added": len([x for x in pg.evaluate("window.__posts") if x["url"] == "/api/storage/app"]) - n,
                         "value": pg.evaluate("document.getElementById('storage-app-select').value"),
                         "btn_disabled": pg.evaluate("document.getElementById('storage-app-btn').disabled")}
        # Escape and a click outside the dialog undo the choice just as Cancel does
        for way in ("escape", "outside"):
            pg.select_option("#storage-app-select", "default")
            pg.click("#storage-app-btn")
            _wait(pg, "document.getElementById('modal-overlay').classList.contains('open')")
            if way == "escape":
                pg.keyboard.press("Escape")
            else:
                pg.mouse.click(5, 5)
            _wait(pg, "!document.getElementById('modal-overlay').classList.contains('open')")
            out[f"dismiss_{way}"] = {
                "posts_added": len([x for x in pg.evaluate("window.__posts") if x["url"] == "/api/storage/app"]) - n,
                "value": pg.evaluate("document.getElementById('storage-app-select').value"),
                "btn_disabled": pg.evaluate("document.getElementById('storage-app-btn').disabled")}

    if kind == "both":
        pg.evaluate("switchTab('redis')")
        _wait(pg, "!!document.getElementById('storage-app-select').options.length")
        pg.select_option("#storage-app-select", "local-mongodb")
        pg.click("#storage-app-btn")
        pg.locator("#modal-actions button", has_text="Move app data").click()
        _wait(pg, "!S.storageBusy && S.storage.app==='local-mongodb'")
        pg.wait_for_timeout(200)
        out["both_after"] = pg.evaluate(_SNAP)
        out["both_toasts"] = _toasts(pg)
        pg.evaluate("switchTab('rag')")
        pg.evaluate("showCreateRag()")
        out["both_new_instance"] = pg.evaluate("document.getElementById('new-rag-endpoint').value")
        out["both_cache_hint"] = pg.evaluate("document.getElementById('cache-store-hint').innerText")
        # the server reports the app-data store down (set on the fake, then re-read)
        pg.evaluate("async()=>{window.__VARIANT.storage.connections.find(c=>c.name===_appConn()).ok=false; await loadStorage();}")
        pg.evaluate("openSettings('cache')")             # computed style needs the pane shown (WebKit)
        out["app_down_cache"] = pg.evaluate("[getComputedStyle(document.getElementById('cache-search-warn')).display, document.getElementById('cache-search-warn').innerText]")
        pg.evaluate("switchTab('status')")
        out["app_down_status"] = _wait(pg, "(()=>{const e=document.getElementById('status-storage-card');return e&&e.innerText.includes('cannot be reached')&&e.innerText.replace(/\\s+/g,' ').trim();})()")

    out["errors"] = errors[:5]
    b.close()
    return out


def run_failing(p, engine: str, index: pathlib.Path, mode: str) -> dict:
    """A MongoDB install whose /api/storage fails (500 once, or a network error every time),
    and one whose /api/storage is slow: never shown as Redis; start-up does not wait."""
    v = _variant("mongodb")
    if mode == "500":
        v["storageFail"] = 99
    elif mode == "net":
        v["storageNetErr"] = True
    else:
        # storage answers last; the built-in's type (from /api/status/redis) arrives in between
        v["storageDelay"] = 3000
        v["statusDelay"] = 1200
    b = getattr(p, engine).launch()
    pg = b.new_page(viewport={"width": 1280, "height": 2400})
    errors: list = []
    pg.on("pageerror", lambda e: errors.append((e.stack or str(e))[:400]))
    pg.add_init_script("window.__VARIANT = " + json.dumps(v) + ";")
    pg.add_init_script(FAKE)
    # start-up ends by arming the 30-s status poll: record when that happens
    pg.add_init_script("""(()=>{const si=window.setInterval;window.setInterval=function(f,ms,...a){
        if(ms===30000&&!window.__initDone)window.__initDone=performance.now();return si.call(this,f,ms,...a);};})();""")
    t0 = time.time()
    pg.goto(index.as_uri(), wait_until="domcontentloaded")
    out: dict = {}
    if mode == "slow":
        # start-up finished (provider probe ran after the awaited loaders) before storage answered
        pg.wait_for_timeout(300)
        pg.evaluate("openSettings('general')")          # baseline taken before anything names the built-in
        out["baseline_has_redis"] = pg.evaluate("'redis' in JSON.parse(_settingsBaseline)")
        out["ready_before_storage"] = bool(_wait(pg, "!!window.__initDone", 2.5)) and pg.evaluate("S.storage===undefined")
        out["init_ms"] = pg.evaluate("window.__initDone&&Math.round(window.__initDone)")
        _wait(pg, "!!S.storage", 6.0)
        pg.evaluate("document.getElementById('s-theme').dispatchEvent(new Event('change',{bubbles:true}))")
        out["dirty_after_storage"] = pg.evaluate("_settingsDirty()")
        out["errors"] = errors[:5]
        b.close()
        return out
    pg.wait_for_timeout(600)
    _open_storage(pg)
    out["snap"] = pg.evaluate(_SNAP)
    out["storage"] = pg.evaluate("S.storage")
    out["saves_redis_section"] = pg.evaluate("'redis' in _collectSettings()")
    pg.evaluate("switchTab('rag')")
    pg.evaluate("showCreateRag()")
    out["new_instance"] = pg.evaluate("[...document.getElementById('new-rag-endpoint').options].map(o=>o.textContent)")
    pg.evaluate("switchTab('status')")
    out["status_card"] = _wait(pg, "(()=>{const e=document.getElementById('status-storage-card');return e&&e.innerText.replace(/\\s+/g,' ').trim();})()")
    # the 30-s poll retries a failed /api/storage
    pg.evaluate("window.__VARIANT.storageFail=0; window.__VARIANT.storageNetErr=false; checkLocalStatus()")
    out["retried"] = _wait(pg, "!!S.storage", 3.0)
    out["errors"] = errors[:5]
    b.close()
    return out


def run_degraded(p, engine: str, index: pathlib.Path) -> dict:
    """An older server: no /api/storage, though its endpoint list already flags a built-in."""
    b = getattr(p, engine).launch()
    pg = b.new_page(viewport={"width": 1280, "height": 1200})
    errors: list = []
    pg.on("pageerror", lambda e: errors.append((e.stack or str(e))[:400]))
    v = {"eps": [{"name": "default", "primary": True, "host": "127.0.0.1", "port": 6389, "type": "redis"},
                 {"name": "local-mongodb", "type": "mongodb", "host": "127.0.0.1:27019", "builtin": True}] + _ADDED[:1],
         "inst": _INST}
    pg.add_init_script("window.__VARIANT = " + json.dumps(v) + ";")
    pg.add_init_script(FAKE)
    pg.goto(index.as_uri(), wait_until="domcontentloaded")
    pg.wait_for_timeout(600)
    _open_storage(pg)
    out = pg.evaluate(_SNAP)
    out["storage"] = pg.evaluate("S.storage")
    pg.evaluate("switchTab('about')")
    _wait(pg, "!!document.getElementById('about-storage')")
    out["about"] = pg.evaluate(_ABOUT)
    out["components"] = pg.evaluate("S.components")
    out["errors"] = errors[:5]
    b.close()
    return out


def main(index: pathlib.Path, engine: str, shots) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        try:
            getattr(p, engine).launch().close()
        except Exception as exc:                  # noqa: BLE001 — an engine that is not installed
            OUT["skip"] = f"{engine} not available: {str(exc)[:120]}"
            return
        for kind in ("redis", "mongodb", "both"):
            OUT[kind] = run_variant(p, engine, index, kind, shots)
        OUT["degraded"] = run_degraded(p, engine, index)
        for mode in ("500", "net", "slow"):
            OUT["fail_" + mode] = run_failing(p, engine, index, mode)
    OUT["ok"] = True


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv) > 2 else "chromium",
             pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else None)
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT, ensure_ascii=False))
