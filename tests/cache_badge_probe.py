# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the answer tells the user about caching, measured in a real browser.

The reported bug was a cache that "did not work": three identical questions, nothing
cached. The cache was behaving as designed — a chart request is deliberately never
looked up and never stored — but nothing on screen said so, and the one badge that
could have (`🔍 Live`) never rendered at all, because the meta row is built while
meta.streaming is still true and is never redrawn afterwards.

Runs standalone under whichever interpreter has Playwright; prints one JSON line.
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1200, "height": 900})
        pg.add_init_script(r"""
            window.fetch = () => Promise.resolve(new Response('{}',
              {status:200, headers:{'Content-Type':'application/json'}}));""")
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(600)

        # The three states the server can report, plus a hit for contrast.
        OUT["badges"] = pg.evaluate(r"""() => {
          const read = id => {
            const m = document.querySelector(`#${id} .msg-meta`);
            if (!m) return null;
            const b = m.querySelector('.cache-badge');
            return b ? {text: b.textContent.trim(), title: b.getAttribute('title') || '',
                        cls: b.className,
                        colour: getComputedStyle(b).color,
                        h: b.offsetHeight, first: m.firstElementChild === b} : null;
          };
          const out = {};
          const cases = {
            miss:    {skipped: null, bypassed: false},
            visual:  {skipped: 'it asks for a chart or diagram, which is always generated fresh',
                      label: 'chart request', bypassed: false},
            attach:  {skipped: 'a file is attached to this turn',
                      label: 'file attached', bypassed: false},
            rerun:   {skipped: null, bypassed: true},
          };
          for (const [k, info] of Object.entries(cases)) {
            const id = 'probe-' + k;
            // exactly how a streamed answer is built: streaming true, then finalised
            appendMessage('assistant', 'an answer', {id, streaming: true});
            out[k + '_while_streaming'] = read(id);
            setCacheBadge(id, info);
            out[k] = read(id);
          }
          // a real hit keeps its own badge and must not be overwritten
          appendMessage('assistant', 'cached answer',
                        {id: 'probe-hit', cacheHit: true, score: 0.94, entryId: 'e1', query: 'q'});
          out['hit'] = read('probe-hit');
          setCacheBadge('probe-hit', {skipped: null, bypassed: false});
          out['hit_after_setCacheBadge'] = read('probe-hit');
          return out;
        }""")

        # the badge has to be READABLE, and to carry its reason without a hover
        OUT["legibility"] = {}
        for _theme in ("light", "dark"):
            OUT["legibility"][_theme] = pg.evaluate(r"""(t) => {
              document.documentElement.dataset.theme = t;
              const id = 'legi-' + t;
              appendMessage('assistant', 'x', {id, streaming: true});
              finalizeStreamingMsg(id);
              setCacheBadge(id, {hit: false, label: 'chart request',
                skipped: 'it asks for a chart or diagram, which is always generated fresh'});
              const b = document.querySelector('#' + id + ' .cache-badge');
              const px = c => c.match(/[\d.]+/g).map(Number);
              const over = (f, bg) => { const a = f.length > 3 ? f[3] : 1;
                return [0,1,2].map(i => f[i]*a + bg[i]*(1-a)); };
              const backdrop = el => { let L = [], n = el;
                while (n && n !== document.documentElement) {
                  const c = px(getComputedStyle(n).backgroundColor);
                  if (c.length < 4 || c[3] > 0) L.push(c); n = n.parentElement; }
                L.push(px(getComputedStyle(document.documentElement).backgroundColor
                          || 'rgb(255,255,255)'));
                let base = [255,255,255];
                for (let i = L.length-1; i >= 0; i--) base = over(L[i], base);
                return base; };
              const lum = c => { const f = v => (v/=255) <= .03928 ? v/12.92
                                                : ((v+.055)/1.055)**2.4;
                return .2126*f(c[0]) + .7152*f(c[1]) + .0722*f(c[2]); };
              const cs = getComputedStyle(b), bg = backdrop(b);
              const [hi, lo] = [lum(px(cs.color)), lum(bg)].sort((a,c) => c-a);
              return {text: b.textContent.trim(), colour: cs.color,
                      contrast: +((hi+.05)/(lo+.05)).toFixed(2),
                      reasonVisible: b.textContent.includes('chart')};
            }""", _theme)
        pg.evaluate("() => { document.documentElement.dataset.theme = 'light'; }")

        # what a RESTORED turn shows — switchSession replays meta straight into
        # appendMessage, with no stream_end to follow
        OUT["restored"] = pg.evaluate(r"""() => {
          const read = id => {
            const b = document.querySelector(`#${id} .msg-meta .cache-badge`);
            return b ? {text: b.textContent.trim(), title: b.getAttribute('title')||'',
                        cls: b.className} : null;
          };
          const out = {};
          const cases = {
            unknown:     {},                                     // pre-1.10.0 session
            was_hit:     {cache: {hit: true, score: 0.93}},
            was_skipped: {cache: {hit: false, skipped: 'an image is attached'}},
            was_miss:    {cache: {hit: false, skipped: null}},
          };
          for (const [k, meta] of Object.entries(cases)) {
            const id = 'restored-' + k;
            appendMessage('assistant', 'restored answer', Object.assign({id}, meta));
            out[k] = read(id);
          }
          return out;
        }""")

        # a hostile reason string from the server is data, not markup
        OUT["escaping"] = pg.evaluate(r"""() => {
          appendMessage('assistant', 'x', {id: 'probe-xss', streaming: true});
          setCacheBadge('probe-xss', {skipped: '"><img src=x onerror=window.__CB=1>'});
          const m = document.querySelector('#probe-xss .msg-meta');
          return {xss: !!window.__CB, imgs: m.querySelectorAll('img').length,
                  title: (m.querySelector('.cache-badge') || {}).title || ''};
        }""")
        pg.wait_for_timeout(120)
        OUT["escaping"]["xss_after_wait"] = pg.evaluate("() => !!window.__CB")

        # calling it twice must not stack badges
        OUT["idempotent"] = pg.evaluate(r"""() => {
          appendMessage('assistant', 'y', {id: 'probe-twice', streaming: true});
          setCacheBadge('probe-twice', {skipped: null});
          setCacheBadge('probe-twice', {skipped: 'an image is attached'});
          const m = document.querySelector('#probe-twice .msg-meta');
          return {count: m.querySelectorAll('.cache-badge').length,
                  text: [...m.querySelectorAll('.cache-badge')].map(b => b.textContent.trim())};
        }""")

        OUT["missing_msg_is_survivable"] = pg.evaluate(
            "() => { try { setCacheBadge('does-not-exist', {}); return true; }"
            "        catch(e) { return String(e); } }")
        b.close()
    OUT["ok"] = True


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]))
    except Exception as e:
        OUT["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(OUT))
