# SPDX-License-Identifier: AGPL-3.0-or-later
"""Render a mermaid diagram with music notation in a real browser, twice: with the Noto
Music webfont reachable, and with its CDN blocked. Prints one JSON object.

index.html is served over HTTP with the app's own Content-Security-Policy header (read
from appcore._CSP), so a font host missing from font-src fails here as it would in use.
"""
import functools
import http.server
import json
import pathlib
import sys
import threading

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT: dict = {}

SRC = ('graph LR\n'
       '    A["♩♩♩ Three short Gs\\n(anacrusis)"] -->|"tension"| B["\U0001D157\U0001D165 One long E♭\\n(downbeat)"]\n'
       '    B --> C["\U0001D160 eighth"]')

JS = r"""async (src)=>{
  const host=document.createElement('div'); host.className='msg-bubble ai';
  host.style.cssText='width:1200px;position:fixed;left:0;top:0;z-index:99999;background:#fff';
  document.body.appendChild(host);
  host.innerHTML=renderMarkdown('```mermaid\n'+src+'\n```',{math:true}); renderRichBlocks(host);
  const out=()=>host.querySelector('.rich-output');
  for(let i=0;i<300;i++){ const o=out(); if(o&&o.dataset.rendered==='1'&&!o.classList.contains('rich-loading'))break;
    await new Promise(r=>setTimeout(r,50)); }
  const o=out(), svg=o&&o.querySelector('svg');
  const tspans=svg?[...svg.querySelectorAll('tspan')].map(t=>t.textContent):[];
  const half=svg&&[...svg.querySelectorAll('tspan')].find(t=>/One long/.test(t.textContent));
  const faces=[...document.fonts].filter(f=>f.family.replace(/["']/g,'')==='VW Music').map(f=>f.status);
  // PNG export: capture the SVG text the exporter rasterises
  let exported=null;
  if(svg){ const ser=XMLSerializer.prototype.serializeToString;
    XMLSerializer.prototype.serializeToString=function(n){ const s=ser.call(this,n); exported=s; return s; };
    try{ downloadSvgAsPng(svg); } finally { XMLSerializer.prototype.serializeToString=ser; } }
  const family=half?getComputedStyle(half).fontFamily:null;
  host.remove();
  return {svg:!!svg, err:o&&o.querySelector('.rich-err')?o.textContent.slice(0,150):null,
          mermaid: typeof mermaid!=='undefined', tspans, family, faces,
          exportedHasHalf: exported?exported.includes('\u{1D157}'):null,
          exportedHasCommon: exported?exported.includes('○'):null};
}"""


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    src = (ROOT / "visualweaver" / "appcore.py").read_text(encoding="utf-8")
    blk = src[src.index("_CSP = ("):]
    ns: dict = {}
    exec(blk[:blk.index("\n)\n") + 3], ns)
    csp = ns["_CSP"]

    class H(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Content-Security-Policy", csp)
            super().end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(H, directory=str(ROOT / "visualweaver")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            for mode in ("online", "font_blocked", "font_slow"):
                pg = b.new_page(viewport={"width": 1240, "height": 400})
                csp_errors: list = []
                pg.on("console", lambda m: csp_errors.append(m.text)
                      if "Content Security Policy" in m.text else None)
                pg.add_init_script("window.fetch=()=>Promise.resolve(new Response('{}',"
                                   "{status:200,headers:{'Content-Type':'application/json'}}));")
                if mode == "font_blocked":
                    pg.route("**/*.woff2", lambda r: r.abort())
                if mode == "font_slow":
                    # the font arrives after the diagram's 4 s wait has given up
                    def slow(route):
                        import time
                        time.sleep(6)
                        route.continue_()
                    pg.route("**/*.woff2", slow)
                pg.goto(url, wait_until="domcontentloaded")
                pg.wait_for_timeout(300)
                OUT[mode] = {**pg.evaluate(JS, SRC), "csp_errors": csp_errors[:3]}
                if mode == "font_slow":
                    # poll from here: wait_for_function evaluates a string, which the CSP forbids
                    for _ in range(80):
                        if pg.evaluate("()=>[...document.fonts].some(f=>f.family.replace(/[\"']/g,'')==='VW Music'&&f.status==='loaded')"):
                            break
                        pg.wait_for_timeout(250)
                    OUT["font_slow_second"] = pg.evaluate(JS, SRC)
                pg.close()
            b.close()
        OUT["ok"] = True
    finally:
        srv.shutdown()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT, ensure_ascii=False))
