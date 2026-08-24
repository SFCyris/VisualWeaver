# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cache similarity threshold control, driven in a real browser.

The number is calibrated per embedding model and the models do not share a scale, so
the control has to be able to say "follow the model" rather than a figure. The trap
it guards: _collectSettings builds its payload from the DOM, so a slider that always
reports a number pins the automatic value the first time anyone opens Settings and
saves — after which it stops following the next model change, silently.

Runs standalone under whichever interpreter has Playwright; prints one JSON line.
"""
import json, pathlib, sys
OUT = {"ok": False}
def main(index):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(); pg = b.new_page(viewport={"width":1200,"height":900})
        pg.add_init_script("window.fetch=()=>Promise.resolve(new Response('{}',"
                           "{status:200,headers:{'Content-Type':'application/json'}}));")
        pg.goto(pathlib.Path(index).as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(600)
        OUT["cases"] = pg.evaluate(r"""() => {
          const read = () => {
            const sl = document.getElementById('s-cache-threshold');
            return {value: sl.value, disabled: sl.disabled,
                    auto: document.getElementById('s-cache-threshold-auto').checked,
                    shown: sl.nextElementSibling.textContent,
                    hint: document.getElementById('s-cache-threshold-hint').textContent,
                    posted: _collectSettings().cache.similarity_threshold};
          };
          const apply = c => {
            const a = c.cache?.similarity_threshold == null;
            setRange('s-cache-threshold', c.cache?.similarity_threshold ?? c.cache_threshold_effective ?? 0.97);
            document.getElementById('s-cache-threshold-auto').checked = a;
            _syncCacheThresholdAuto();
          };
          const out = {};
          apply({cache: {similarity_threshold: null}, cache_threshold_effective: 0.97});
          out.auto_e5 = read();
          apply({cache: {similarity_threshold: null}, cache_threshold_effective: 0.93});
          out.auto_minilm = read();
          apply({cache: {similarity_threshold: 0.85}, cache_threshold_effective: 0.97});
          out.explicit = read();
          // and the user un-ticking auto to pin the current value
          document.getElementById('s-cache-threshold-auto').checked = false;
          _syncCacheThresholdAuto();
          out.unticked = read();
          return out;
        }""")
        b.close()
    OUT["ok"] = True
try: main(sys.argv[1])
except Exception as e: OUT["error"] = f"{type(e).__name__}: {e}"
print(json.dumps(OUT))
