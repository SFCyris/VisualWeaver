# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render the answer-cache table with canned TTLs (Redis codes: -1 never, -2 gone). Prints JSON."""
import json, pathlib, sys
from playwright.sync_api import sync_playwright
FAKE = """(()=>{const json=(b)=>Promise.resolve(new Response(JSON.stringify(b),{status:200,headers:{'Content-Type':'application/json'}}));
window.fetch=(u)=>{u=String(u); if(u.startsWith('/api/cache/stats'))return json({count:6,entries:[
 {query:'never q',ttl:-1},{query:'gone q',ttl:-2},{query:'short',ttl:45},{query:'mins',ttl:3540},{query:'hours',ttl:86000},{query:'days',ttl:604800}]});
 if(/templates|sessions|logs|feedback|sources|watch|schedul|instances|endpoints/.test(u))return json([]); return json({});};})();"""
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(); pg.add_init_script(FAKE)
    pg.goto(pathlib.Path(sys.argv[1]).resolve().as_uri()); pg.wait_for_timeout(400)
    pg.evaluate("loadCacheAnalytics()"); pg.wait_for_timeout(300)
    rows=pg.evaluate("[...document.querySelectorAll('#cache-analytics tbody tr')].map(r=>[...r.cells].map(c=>c.innerText))")
    col=pg.evaluate("getComputedStyle(document.querySelector('#cache-analytics tbody tr:nth-child(2) td:nth-child(2) span')).color")
    print(json.dumps({"rows":rows,"expired_color":col})); b.close()
