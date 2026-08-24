# SPDX-License-Identifier: AGPL-3.0-or-later
"""The RAG advice panel, measured in a real browser.

Everything this feature does is visual or DOM-side: which lines appear, what colour
they are, and whether pressing a preset actually moves the controls it names. None of
that is reachable from a Python test of the endpoint, and the one defect that shipped
here — a preset writing only its diff, so a control the user had already dragged stayed
where it was — was invisible to every backend test.

Runs standalone under whichever interpreter has Playwright; prints one JSON line.
"""
import json
import pathlib
import sys

OUT: dict = {"ok": False}

ADVICE = {
    "samples": 340, "cited_answers": 190, "raw_hist": [0] * 20,
    "coverage": {"measured": 340, "replayed": 1080, "asked": 1420, "share": 0.239,
                 "note": "Measured over 340 of 1,420 questions — the other 1,080 were "
                         "answered from cache, which runs no retrieval."},
    "corpora": [
        {"id": "Ubuntu", "queries": 4300, "errors": 0, "sample_size": 500,
         "mean_raw": 0.31, "pass_now": 0.02, "wants": 0.30, "wants_ci": [0.29, 0.31],
         "state": "measured", "votes": True},
        {"id": "saved-answers", "queries": 500, "errors": 0, "sample_size": 500,
         "mean_raw": 0.67, "pass_now": 0.86, "wants": 0.62, "wants_ci": [0.61, 0.63],
         "state": "measured", "votes": True},
        {"id": "default", "queries": 40, "errors": 0, "sample_size": 40, "mean_raw": 0.0,
         "pass_now": 0.0, "wants": None, "wants_ci": None,
         "state": "no_signal", "votes": False},
        {"id": "<img src=x onerror=window.__XSS4=1>", "queries": 6, "errors": 0,
         "sample_size": 6, "mean_raw": 0.44, "pass_now": None, "wants": None,
         "wants_ci": None, "state": "thin", "votes": False},
    ],
    "settings": {
        "similarity_threshold": {"value": 0.62, "status": "bad",
            "headline": "Only 12% of queries clear this",
            "detail": "Across 340 queries the best score averaged 0.53.", "suggested": 0.37,
            "hist": [0, 0, 0, 4, 12, 30, 61, 88, 70, 44, 20, 8, 3, 0, 0, 0, 0, 0, 0, 0],
            "curve": [round(max(0.0, 1.0 - (i / 100) ** 2 * 1.6), 4) for i in range(101)],
            "curve_of": "Ubuntu"},
        "top_k": {"value": 20, "status": "warn",
            "headline": "Citations tail off after rank 4",
            "detail": "95% of citations fall within the top 4.", "suggested": 6},
        "reranker": {"value": False, "status": "warn", "headline": "Off, with a wide top_k",
            "detail": "Reranking lets top_k stay small.", "suggested": True},
        "hybrid_search": {"value": True, "status": "ok", "headline": "On",
            "detail": "Vector and keyword legs both run.", "suggested": None},
        "chunk_size": {"value": 180, "status": "static", "headline": "Not measurable here",
            "detail": "Chunk size cannot be judged from retrieval scores.", "suggested": None},
        "chunk_overlap": {"value": 32, "status": "static", "headline": "Not measurable here",
            "detail": "Overlap cannot be judged from retrieval scores.", "suggested": None},
        "history_max_tokens": {"value": 3000, "status": "unknown",
            "headline": "Not enough data yet", "detail": "No measurement yet.", "suggested": None},
    }}

PRESETS = [
    {"id": "precision", "name": "Precision", "when": "A clean, well-matched corpus.",
     "changes": [{"path": "rag.top_k", "label": "Top-K Chunks", "from": 20, "to": 5},
                 {"path": "reranker.enabled", "label": "Rerank retrieved chunks",
                  "from": None, "to": True}],
     # top_k is ALSO in values while already present in changes; hybrid_search is in
     # values but NOT in changes — the diff-only bug is invisible unless they differ.
     "values": {"rag.top_k": 5, "rag.hybrid_search": True, "reranker.enabled": True,
                "reranker.top_n": 5, "rag.rerank_candidates": 40,
                "rag.similarity_threshold": 0.48},
     "note": None},
    {"id": "recall", "name": "Recall", "when": "A large or uneven corpus.",
     "changes": [{"path": "rag.top_k", "label": "Top-K Chunks", "from": 20, "to": 8}],
     "values": {"rag.top_k": 8, "rag.hybrid_search": True},
     "note": "Similarity Threshold is left unchanged — only 3 of 20 scored searches so "
             "far, not enough to choose one."},
    {"id": "economy", "name": "Economy", "when": "A paid API where tokens are the constraint.",
     "changes": [], "values": {"rag.top_k": 4}, "note": None},
]

# A headline and a label the server could carry through from user-named content.
HOSTILE = {"advice": {"samples": 30, "cited_answers": 30, "raw_hist": [0] * 20,
    "settings": {"top_k": {"value": 5, "status": "bad",
        "headline": "<img src=x onerror=window.__XSS=1>",
        "detail": "<script>window.__XSS2=1</script>", "suggested": 6}}},
    "presets": [{"id": "precision", "name": "Precision", "when": "w",
                 "changes": [{"path": "rag.top_k",
                              "label": "<img src=x onerror=window.__XSS3=1>",
                              "from": 1, "to": 2}],
                 "values": {"rag.top_k": 2}, "note": None}]}


def main(index: pathlib.Path) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return

    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1400, "height": 1000})
        pg.add_init_script(
            "window.__ADV=" + json.dumps({"advice": ADVICE, "presets": PRESETS}) + ";"
            "window.__HOSTILE=" + json.dumps(HOSTILE) + ";" + r"""
            window.__calls = [];
            window.fetch = (u, o) => {
              window.__calls.push({url: String(u), method: (o && o.method) || 'GET',
                                   body: (o && o.body) || null});
              const body = String(u).includes('/api/rag/advice')
                         ? JSON.stringify(window.__ADV) : '{}';
              return Promise.resolve(new Response(body,
                {status: 200, headers: {'Content-Type': 'application/json'}}));
            };""")
        pg.goto(index.as_uri(), wait_until="domcontentloaded")
        pg.wait_for_timeout(700)

        pg.evaluate("""() => {
          document.getElementById('settings-overlay').classList.add('open');
          switchTab('rag');
          S.ragAdvice = window.__ADV;
          _renderRagAdvice(window.__ADV.advice);
          _renderScoreHistogram(window.__ADV.advice.settings.similarity_threshold);
          _renderRagCoverage(window.__ADV.advice.coverage);
          _renderRagPresets(window.__ADV.presets);
        }""")
        pg.wait_for_timeout(200)

        # ── what is rendered, and in what colour ─────────────────────────────
        OUT["lines"] = pg.evaluate(r"""() => {
          const px = c => c.match(/[\d.]+/g).map(Number);
          const over = (f, b) => { const a = f.length > 3 ? f[3] : 1;
            return [0,1,2].map(i => f[i]*a + b[i]*(1-a)); };
          const backdrop = el => { let L = [], n = el;
            while (n && n !== document.documentElement) {
              const c = px(getComputedStyle(n).backgroundColor);
              if (c.length < 4 || c[3] > 0) L.push(c); n = n.parentElement; }
            let base = [255,255,255];
            for (let i = L.length-1; i >= 0; i--) base = over(L[i], base);
            return base; };
          const lum = c => { const f = v => (v/=255) <= .03928 ? v/12.92 : ((v+.055)/1.055)**2.4;
                             return .2126*f(c[0])+.7152*f(c[1])+.0722*f(c[2]); };
          const out = {};
          document.querySelectorAll('[data-advice]').forEach(g => {
            const line = g.querySelector('.adv-line');
            if (!line) return;
            const head = line.querySelector('.adv-head');
            const cs = head ? getComputedStyle(head) : null;
            const bg = head ? backdrop(head) : null;
            const [hi, lo] = cs ? [lum(px(cs.color)), lum(bg)].sort((a,b)=>b-a) : [1,1];
            out[g.dataset.advice] = {
              hidden: !!line.hidden, height: line.offsetHeight,
              cls: line.className,
              glyph: (line.querySelector('.adv-mark')||{}).textContent || '',
              sr: (line.querySelector('.sr-only')||{}).textContent || '',
              head: head ? head.textContent : '',
              color: cs ? cs.color : '', fontSize: cs ? cs.fontSize : '',
              contrast: cs ? +((hi+.05)/(lo+.05)).toFixed(2) : 0,
              applyLabel: (line.querySelector('.adv-apply')||{}).textContent || null,
            };
          });
          return out;
        }""")

        # ── the ⓘ must not end up inside the control's accessible name ───────
        # Read the COMPUTED name from the accessibility tree, not a wrapper's
        # textContent: the toggles name their input with aria-labelledby pointing at an
        # inner span, so the wrapper legitimately contains the button while the name
        # does not. Measuring the wrapper reported a defect that was not there.
        # (this Playwright build exposes no accessibility snapshot, so the name is
        # computed here the way the spec resolves it: aria-labelledby, then aria-label,
        # then the associated label element.)
        OUT["names"] = pg.evaluate("""() => {
          const named = el => {
            const by = el.getAttribute('aria-labelledby');
            if (by) return (document.getElementById(by) || {}).textContent || '';
            if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
            const l = el.id && document.querySelector(`label[for="${el.id}"]`);
            return l ? l.textContent : '';
          };
          const out = {};
          document.querySelectorAll('[data-advice]').forEach(g => {
            const c = g.querySelector('input, select, textarea');
            if (c) out[g.dataset.advice] = named(c).trim();
          });
          out['_info_count'] = document.querySelectorAll('.adv-info').length;
          out['_info_min'] = Math.min(...[...document.querySelectorAll('.adv-info')]
            .map(b => Math.min(b.offsetWidth, b.offsetHeight)));
          out['_apply_min'] = Math.min(...[...document.querySelectorAll('.adv-apply')]
            .map(b => b.offsetHeight));
          return out;
        }""")

        # ── a preset writes every value it names, not only its diff ──────────
        OUT["preset_apply"] = pg.evaluate("""() => {
          // stage values that DIFFER from the saved config, as a user dragging would
          _setByPath('rag.top_k', 18);
          _setByPath('rag.hybrid_search', false);
          _setByPath('reranker.top_n', 3);
          const read = () => ({
            top_k:  +document.getElementById('s-top-k').value,
            hybrid: document.getElementById('s-hybrid').checked,
            topn:   +document.getElementById('s-rerank-topn').value,
            cands:  +document.getElementById('s-rerank-candidates').value,
            thr:    +document.getElementById('s-rag-threshold').value,
            rerank: document.getElementById('s-rerank-enabled').checked,
          });
          const before = read();
          const n = window.__calls.length;
          _applyPreset(S.ragAdvice.presets[0]);
          return {before, after: read(), writes: window.__calls.slice(n)
                    .filter(c => c.method !== 'GET')};
        }""")

        # ── _setByPath across control types, and an unknown path ─────────────
        OUT["set_by_path"] = pg.evaluate("""() => {
          _setByPath('rag.hybrid_search', true);
          _setByPath('rag.top_k', 7);
          _setByPath('rag.similarity_threshold', 0.44);
          let threw = false;
          try { _setByPath('rag.no_such_setting', 1); } catch(e) { threw = true; }
          const r = document.getElementById('s-rag-threshold');
          return {checkbox: document.getElementById('s-hybrid').checked,
                  number: document.getElementById('s-top-k').value,
                  range: r.value,
                  // setRange also maintains the number printed beside the slider, and
                  // reads it back off the input so it reflects the step the control
                  // snapped to. Assigning .value alone leaves that readout stale.
                  rangeReadout: (r.nextElementSibling || {}).textContent || '',
                  unknownPathThrew: threw};
        }""")

        # ── previewPreset stages, and says what it will not touch ────────────
        def modal_after(js):
            # One reusable dialog (#modal-overlay), so close it between cases rather
            # than removing nodes — otherwise the previous body reads as this one's.
            pg.evaluate("() => closeModal()")
            pg.evaluate(js)
            pg.wait_for_timeout(150)
            return pg.evaluate("""() => {
              const ov = document.getElementById('modal-overlay');
              if (!ov || !ov.classList.contains('open')) return null;
              const body = document.getElementById('modal-body');
              return {title: document.getElementById('modal-title').textContent,
                      text: body.textContent,
                      html: body.innerHTML,
                      rows: [...body.querySelectorAll('.preset-diff li')]
                              .map(li => li.textContent.replace(/\s+/g,' ').trim()),
                      buttons: [...document.getElementById('modal-actions')
                                  .querySelectorAll('button')].map(b=>b.textContent.trim())};
            }""")

        OUT["preview_with_changes"] = modal_after("() => previewPreset('precision')")
        OUT["preview_with_note"]    = modal_after("() => previewPreset('recall')")
        OUT["preview_no_changes"]   = modal_after("() => previewPreset('economy')")
        OUT["info_modal"]           = modal_after("() => showAdviceInfo('similarity_threshold')")
        OUT["info_modal_nosuggest"] = modal_after("() => showAdviceInfo('hybrid_search')")

        # ── the apply button on a line writes the control it names ───────────
        OUT["apply_button"] = pg.evaluate("""() => {
          _setByPath('rag.similarity_threshold', 0.9);
          const before = document.getElementById('s-rag-threshold').value;
          document.querySelector('[data-advice="similarity_threshold"] .adv-apply').click();
          return {before, after: document.getElementById('s-rag-threshold').value};
        }""")
        OUT["apply_button_bool"] = pg.evaluate("""() => {
          _setByPath('reranker.enabled', false);
          const b = document.querySelector('[data-advice="reranker"] .adv-apply');
          const label = b.textContent;
          b.click();
          return {label, after: document.getElementById('s-rerank-enabled').checked};
        }""")

        # ── the histogram, and whether the slider reads live ─────────────────
        OUT["hist"] = pg.evaluate("""() => {
          const el = document.querySelector('.score-hist');
          if (!el) return null;
          const bars = [...el.querySelectorAll('.sh-bar')];
          const set = v => { const i = document.getElementById('s-rag-threshold');
                             i.value = v; i.dispatchEvent(new Event('input')); };
          const read = () => ({left: el.querySelector('.sh-mark').style.left,
                               now: el.querySelector('.sh-now').textContent});
          const at = {};
          for (const v of [0.10, 0.50, 0.90]) { set(v); at[v] = read(); }
          return {bars: bars.length,
                  heights: bars.map(b => b.offsetHeight),
                  titles: bars.slice(5, 8).map(b => b.getAttribute('title')),
                  drawnBeforeVerdict: !!(el.compareDocumentPosition(
                      document.querySelector('[data-advice="similarity_threshold"] .adv-line'))
                      & Node.DOCUMENT_POSITION_FOLLOWING),
                  at};
        }""")
        OUT["hist_empty"] = pg.evaluate("""() => {
          _renderScoreHistogram({hist: new Array(20).fill(0), curve: null});
          return document.querySelectorAll('.score-hist').length;
        }""")
        pg.evaluate("""() => _renderScoreHistogram(
            window.__ADV.advice.settings.similarity_threshold)""")

        # ── the coverage line, and the per-corpus table behind the ⓘ ─────────
        OUT["coverage"] = pg.evaluate("""() => {
          const e = document.querySelector('.adv-coverage');
          if (!e) return null;
          const cs = getComputedStyle(e);
          return {text: e.textContent, colour: cs.color, h: e.offsetHeight,
                  beforePresets: !!(e.compareDocumentPosition(
                      document.getElementById('rag-presets')) & Node.DOCUMENT_POSITION_FOLLOWING)};
        }""")
        OUT["corpus_table"] = modal_after("() => showAdviceInfo('similarity_threshold')")
        OUT["corpus_table_topk"] = modal_after("() => showAdviceInfo('top_k')")

        # ── taking the advice must move the chart and stale the verdicts ─────
        OUT["after_apply"] = pg.evaluate("""() => {
          const slider = document.getElementById('s-rag-threshold');
          const mark = () => document.querySelector('.score-hist .sh-mark').style.left;
          const now  = () => document.querySelector('.score-hist .sh-now').textContent;
          const stale = () => [...document.querySelectorAll('[data-advice] .adv-line:not([hidden])')]
                                .map(l => l.classList.contains('adv-stale'));
          setRange('s-rag-threshold', 0.62);
          _updateHistMarker(window.__ADV.advice.settings.similarity_threshold);
          _staleAdvice(false);                              // clean baseline
          const before = {slider: slider.value, mark: mark(), now: now(), stale: stale()};
          document.querySelector('[data-advice="similarity_threshold"] .adv-apply').click();
          return {before, after: {slider: slider.value, mark: mark(), now: now(),
                                  stale: stale()}};
        }""")
        OUT["after_manual_edit"] = pg.evaluate("""() => {
          _renderRagAdvice(window.__ADV.advice);           // clears stale
          const before = document.querySelector('[data-advice="top_k"] .adv-line')
                           .classList.contains('adv-stale');
          const el = document.getElementById('s-top-k');
          el.value = 9; el.dispatchEvent(new Event('input', {bubbles: true}));
          return {before, after: document.querySelector('[data-advice="top_k"] .adv-line')
                                   .classList.contains('adv-stale')};
        }""")
        OUT["template_rebind"] = pg.evaluate("""() => {
          S.templates = [{name:'Legal',system:'LEGAL'},{name:'Medical',system:'MEDICAL'},
                         {name:'Code',system:'CODE'}];
          populateTemplateSelect();
          const sel = document.getElementById('template-select');
          sel.value = '1';                                  // Medical
          sel.dispatchEvent(new Event('change', {bubbles: true}));
          const before = S.templates[parseInt(sel.value)];
          S.templates.splice(0, 1);                         // delete Legal
          populateTemplateSelect();
          const after = S.templates[parseInt(sel.value)];
          return {before: before && before.name, after: after && after.name,
                  value: sel.value};
        }""")

        # ── the diagnosis you can paste into a ticket ────────────────────────
        OUT["diagnosis"] = pg.evaluate("""() => {
          let copied = null;
          navigator.clipboard.writeText = t => { copied = t; return Promise.resolve(); };
          const text = copyRagDiagnosis();
          const btn = document.querySelector('.preset-copy');
          return {text, copied, hasButton: !!btn,
                  buttonKeepsPresetsFirst: !!btn && btn ===
                    [...document.querySelectorAll('#rag-presets .preset-btn')].pop()};
        }""")

        # ── hostile strings from the server are data, not markup ─────────────
        OUT["escaping"] = pg.evaluate("""() => {
          S.ragAdvice = window.__HOSTILE;
          _renderRagAdvice(window.__HOSTILE.advice);
          _renderRagPresets(window.__HOSTILE.presets);
          previewPreset('precision');
          const line = document.querySelector('[data-advice="top_k"] .adv-head');
          return {xss: !!(window.__XSS || window.__XSS2 || window.__XSS3),
                  imgs: document.querySelectorAll('.adv-line img, .preset-diff img').length,
                  rendered: line ? line.textContent : ''};
        }""")
        pg.wait_for_timeout(150)
        OUT["escaping"]["xss_after_wait"] = pg.evaluate(
            "() => !!(window.__XSS || window.__XSS2 || window.__XSS3)")

        b.close()
    OUT["ok"] = True


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]))
    except Exception as e:                                   # the harness reads error
        OUT["error"] = f"{type(e).__name__}: {e}"
    print(json.dumps(OUT))
