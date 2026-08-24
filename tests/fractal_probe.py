# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pixel measurements of the four ```fractal renderers, in a real browser.

The offline tests in test_fractal_js.py cover spec normalisation and the L-system
maths, but they stop before _fracDraw because the renderers need a canvas. That
left the drawing itself asserted only in a docstring. This probe draws each
renderer onto an offscreen canvas and reports what actually landed on it: how
much of the frame is inked, the bounding box of that ink, and how many distinct
colours appear.

Emits one line of JSON on stdout. Driven by tests/test_fractal_browser.py.
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}

# Each entry is measured, not eyeballed. The expectations live in the test.
SPECS = {
    "hilbert":    {"type": "hilbert", "order": 5},
    "peano":      {"type": "peano", "order": 4},
    "moore":      {"type": "moore"},
    "gosper":     {"type": "gosper"},
    "lorenz":     {"type": "lorenz"},
    "rossler":    {"type": "rossler"},
    "halvorsen":  {"type": "halvorsen"},
    "clifford":   {"type": "clifford"},
    "fern":       {"type": "fern"},
    "carpet":     {"type": "carpet"},
    "mandelbrot": {"type": "mandelbrot"},
    "julia":      {"type": "julia"},
    "diverged":   {"type": "lorenz", "rho": 9000, "sigma": 900},
}

MEASURE = r"""
async (spec) => {
  const W = 560, H = 380;
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  let s;
  try { s = _fracSpec(JSON.stringify(spec)); }
  catch (e) { return {error: String(e.message || e)}; }
  const inst = {kind:'fractal', spec:s, canvas:c, gen:0, raf:null,
                view:{cx: s.center ? s.center[0] : 0,
                      cy: s.center ? s.center[1] : 0, zoom: s.zoom || 1}};
  const t0 = performance.now();
  _fracDraw(inst, c);
  // drain the chunked batches; each renderer chains itself through setTimeout
  for (let i = 0; i < 8000 && inst.raf; i++) await new Promise(r => setTimeout(r, 0));
  const ms = Math.round(performance.now() - t0);
  const d = c.getContext('2d').getImageData(0, 0, W, H).data;
  const bg = [d[0], d[1], d[2]];
  let ink = 0, minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
  const colours = new Set();
  for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
    const o = (y * W + x) * 4;
    if (Math.abs(d[o]-bg[0]) + Math.abs(d[o+1]-bg[1]) + Math.abs(d[o+2]-bg[2]) > 18) {
      ink++; colours.add((d[o]>>4) + ',' + (d[o+1]>>4) + ',' + (d[o+2]>>4));
      if (x < minx) minx = x; if (x > maxx) maxx = x;
      if (y < miny) miny = y; if (y > maxy) maxy = y;
    }
  }
  return {ms, pending: !!inst.raf, ink,
          inkPct: +(100 * ink / (W * H)).toFixed(2),
          w: ink ? maxx - minx : 0, h: ink ? maxy - miny : 0,
          colours: colours.size, type: s.type, system: s.system || null};
}
"""


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 900, "height": 700})
        pg.add_init_script(r"""
            window.fetch = () => Promise.resolve(new Response('{}',
              {status:200, headers:{'Content-Type':'application/json'}}));""")
        errors: list = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(800)
        OUT["shots"] = {name: pg.evaluate(MEASURE, spec) for name, spec in SPECS.items()}
        OUT["errors"] = errors[:5]
        OUT["ok"] = True
        b.close()


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]).resolve())
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT))
