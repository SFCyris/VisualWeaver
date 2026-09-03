# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ```fractal lane's pure helpers, extracted from index.html and run under
node: spec normalisation with hard caps (a hallucinated spec must not be able to
spin the tab), preset resolution, L-system expansion with its size guard, and the
charset rules that keep the lane data-only. Offline — no browser, no canvas;
the renderers themselves are measured in pixels by tests/test_fractal_browser.py."""
import json
import pathlib
import shutil

import pytest

from _jsrun import run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _helpers() -> str:
    html = _INDEX.read_text(encoding="utf-8")
    start = html.index("const _FRAC_MAX_ITER")
    end = html.index("function _fracDraw(")   # renderers need a canvas; stop before
    return html[start:end]


def _val(expr: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = _helpers() + f"\nconsole.log(JSON.stringify((()=>{{try{{return {expr};}}catch(e){{return {{__err:String(e.message||e)}};}}}})()));\n"
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── presets ──────────────────────────────────────────────────────────────────
def test_fern_preset_resolves_to_barnsleys_four_maps():
    s = _val("_fracSpec('{\"type\":\"fern\"}')")
    assert s["type"] == "ifs" and len(s["maps"]) == 4
    assert s["maps"][1][:6] == [0.85, 0.04, -0.04, 0.85, 0, 1.6]   # the stem-to-tip map


def test_julia_preset_supplies_c_and_user_c_overrides_it():
    s = _val("_fracSpec('{\"type\":\"julia\"}')")
    assert s["c"] == [-0.8, 0.156]
    s2 = _val("_fracSpec('{\"type\":\"julia\",\"c\":[0.285,0.01]}')")
    assert s2["c"] == [0.285, 0.01]


def test_lsystem_presets_resolve():
    for name in ("dragon", "koch", "plant"):
        s = _val(f"_fracSpec('{{\"type\":\"{name}\"}}')")
        assert s["type"] == "lsystem" and s["rules"], name


# ── caps: a hallucinated spec must be bounded ────────────────────────────────
def test_iter_points_and_depth_are_clamped():
    s = _val("_fracSpec('{\"type\":\"mandelbrot\",\"iter\":999999,\"zoom\":1e99}')")
    assert s["iter"] == 1000 and s["zoom"] == 1e12
    s2 = _val("_fracSpec('{\"type\":\"ifs\",\"maps\":[[0.5,0,0,0.5,0,0]],\"points\":99999999}')")
    assert s2["points"] == 200000
    s3 = _val("_fracSpec('{\"type\":\"lsystem\",\"axiom\":\"F\",\"rules\":{\"F\":\"FF\"},\"depth\":99}')")
    assert s3["depth"] == 14


def test_lsystem_expansion_size_guard_throws():
    # F -> FFFF quadruples per level: depth 14 blows through the cap and must abort
    r = _val("_fracLExpand('F',{F:'FFFF'},14,120000)")
    assert isinstance(r, dict) and "exceeds" in r["__err"]


def test_lsystem_expansion_is_correct():
    assert _val("_fracLExpand('F',{F:'F+F'},2,120000)") == "F+F+F+F"
    # letters without a rule pass through unchanged (X/Y structure carriers)
    assert _val("_fracLExpand('X',{X:'FX'},3,120000)") == "FFFX"


# ── data-only charset rules ──────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    '{"type":"lsystem","axiom":"F;alert(1)","rules":{}}',
    '{"type":"lsystem","axiom":"F","rules":{"F":"F<script>"}}',
    '{"type":"lsystem","axiom":"F","rules":{"FX":"F"}}',
])
def test_lsystem_rejects_non_turtle_strings(bad):
    r = _val(f"_fracSpec('{bad}')")
    assert isinstance(r, dict) and "__err" in r, f"accepted: {bad}"


def test_ifs_rejects_non_numeric_maps():
    r = _val("_fracSpec('{\"type\":\"ifs\",\"maps\":[[\"a\",0,0,0.5,0,0]]}')")
    assert isinstance(r, dict) and "numbers only" in r["__err"]


def test_unknown_type_and_garbage_json_error_clearly():
    r = _val("_fracSpec('{\"type\":\"donut\"}')")
    assert "unknown fractal type" in r["__err"]
    r2 = _val("_fracSpec('not json')")
    assert "must be JSON" in r2["__err"]


def test_palette_is_whitelisted():
    s = _val("_fracSpec('{\"type\":\"mandelbrot\",\"palette\":\"<img onerror=x>\"}')")
    assert s["palette"] == "viridis"


# ── palette maths ────────────────────────────────────────────────────────────
def test_palette_interpolates_within_rgb_bounds():
    v = _val("[0,0.25,0.5,0.75,1].map(t=>_fracPalette('fire',false).at(t))")
    for c in v:
        assert len(c) == 3 and all(0 <= x <= 255 for x in c)
    assert v[0] != v[-1], "palette endpoints must differ"


# ── prompt steering: the lane existing is not enough — the model must be told
# NOT to fake a fractal with ```geometry or ```svg (a real report: asked for an
# IFS + Julia set example, the model hand-drew a 5-polygon "Sierpinski" in
# ```geometry and a static blob in ```svg captioned "Julia Set Approximation" —
# ```fractal was never used even though it was available and documented). ──────
def test_prompt_directs_fractals_and_ifs_and_julia_to_the_fractal_lane():
    from visualweaver import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    i = t.index("```fractal —")
    bullet = t[i:t.index("\n", i)]
    assert "REQUIRED" in bullet
    for kw in ("Mandelbrot", "Julia", "IFS", "L-system"):
        assert kw in bullet, f"{kw!r} missing from the fractal bullet"
    assert "```geometry" in bullet and "```svg" in bullet, \
        "the fractal bullet must name the two lanes the model reached for instead"


def test_geometry_and_svg_bullets_point_back_to_fractal():
    from visualweaver import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    geo = t[t.index("```geometry —"):t.index("\n", t.index("```geometry —"))]
    assert "```fractal" in geo, "the geometry bullet must disclaim fractals in favour of ```fractal"
    svg = t[t.index("```abc —"):t.index("\n", t.index("```abc —"))]
    assert "```fractal" in svg, "the svg fallback bullet must disclaim fractals too"


def test_prompt_tells_the_model_to_use_decimals_not_fractions():
    from visualweaver import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    bullet = t[t.index("```fractal —"):t.index("\n", t.index("```fractal —"))]
    assert "1/3" in bullet and "0.333" in bullet, \
        "the fractal bullet must show the fraction-vs-decimal example that provoked this"


# ── fraction-literal reshape: a real report ──────────────────────────────────
# The model wrote IFS probabilities as fractions — [...,1/3] — valid maths,
# invalid JSON (`JSON.parse` threw "Expected ',' or ']' after array element"
# and blanked the card). Rewrite NUMBER/NUMBER to its decimal value first,
# via parseFloat arithmetic only — never eval — so this can't become a code path.
def test_bare_fraction_becomes_its_decimal_value():
    assert _val("_fracFixFractions('[1/3]')") == "[0.3333333333333333]"
    assert _val("_fracFixFractions('[1/2, 3/4]')") == "[0.5, 0.75]"


def test_fixed_source_actually_parses_and_the_real_reported_spec_now_renders():
    spec = ('{"type":"ifs","maps":[[0.5,0,0,0.5,0,0,1/3],'
            '[0.5,0,0,0.5,0.5,0,1/3],[0.5,0,0,0.5,0.25,0.433,1/3]],"iter":50000}')
    out = _val(f"_fracSpec({json.dumps(spec)})")
    assert not isinstance(out, dict) or "__err" not in out, out
    assert out["type"] == "ifs" and len(out["maps"]) == 3
    for m in out["maps"]:
        assert abs(m[6] - 1 / 3) < 1e-9, m


def test_quoted_strings_are_never_touched_by_the_fraction_rewrite():
    # no field in this schema is a "N/N"-shaped string, but the protection must
    # hold regardless — a slash inside quotes is never arithmetic
    assert _val("_fracFixFractions('{\"palette\":\"1/3\"}')") == '{"palette":"1/3"}'


def test_division_by_a_literal_zero_is_left_alone():
    # 1/0 stays untouched so JSON.parse still reports a clear, honest error
    # instead of silently emitting Infinity into a numeric field
    assert _val("_fracFixFractions('[1/0]')") == "[1/0]"


def test_ordinary_numbers_and_negative_numbers_are_unaffected():
    assert _val("_fracFixFractions('[0.5, -0.25, 12]')") == "[0.5, -0.25, 12]"


# ── curve + attractor presets ────────────────────────────────────────────────
# Added after a real report: the model answered "visualize the math behind fern
# fractals", "Hilbert curve", "Peano curve" and "Lorenz attractor" with
# {"type":"hilbert"}, {"type":"peano"} and {"type":"lorenz"} — reaching for
# ```fractal correctly, since every one of those IS an L-system or an attractor
# — and got a red "unknown fractal type" box. The lane could always draw the
# first two; only the NAME was missing.

def _preset_names() -> list[str]:
    """Every key of _FRAC_PRESETS, read from index.html.

    Discovered rather than listed, so a preset added later is covered by these
    tests without anyone remembering to extend a literal.
    """
    return _val("Object.keys(_FRAC_PRESETS)")


def test_every_preset_resolves_to_a_renderable_spec():
    for name in _preset_names():
        s = _val(f"_fracSpec('{{\"type\":\"{name}\"}}')")
        assert "__err" not in s, f"{name}: {s.get('__err')}"
        assert s["type"] in ("mandelbrot", "julia", "ifs", "lsystem", "attractor"), name


def test_every_alias_resolves_to_a_real_preset():
    pairs = _val("Object.entries(_FRAC_ALIASES)")
    known = set(_preset_names())
    for alias, target in pairs:
        assert target in known, f"alias {alias!r} points at unknown preset {target!r}"


def test_the_three_types_from_the_report_now_resolve():
    assert _val("_fracSpec('{\"type\":\"hilbert\",\"order\":5}')")["type"] == "lsystem"
    assert _val("_fracSpec('{\"type\":\"peano\",\"order\":4}')")["type"] == "lsystem"
    lorenz = _val('_fracSpec(\'{"type":"lorenz","iter":5000,"sigma":10,"rho":28,"beta":2.67}\')')
    assert lorenz["type"] == "attractor" and lorenz["system"] == "lorenz"
    assert lorenz["steps"] == 5000 and lorenz["params"]["beta"] == 2.67


def test_order_overrides_the_presets_own_depth():
    # The merge is {...preset, ...spec}. Folding `order` onto the spec AFTER the
    # preset merge would let hilbert's depth:5 win and silently ignore the number
    # the spec actually asked for.
    assert _val("_fracSpec('{\"type\":\"hilbert\"}')")["depth"] == 5
    assert _val("_fracSpec('{\"type\":\"hilbert\",\"order\":2}')")["depth"] == 2
    assert _val("_fracSpec('{\"type\":\"hilbert\",\"depth\":3}')")["depth"] == 3


def test_steps_is_accepted_as_a_synonym_for_iter():
    assert _val("_fracSpec('{\"type\":\"lorenz\",\"steps\":7000}')")["steps"] == 7000


@pytest.mark.parametrize("written,expect", [
    ("Hilbert Curve", "lsystem"), ("hilbert_curve", "lsystem"),
    ("KochSnowflake", "lsystem"), ("Sierpinski Carpet", "ifs"),
    ("lorenz_attractor", "attractor"), ("Flowsnake", "lsystem"),
    ("  DRAGON  ", "lsystem"), ("de-jong", "attractor"),
])
def test_type_names_are_separator_and_case_insensitive(written, expect):
    s = _val(f"_fracSpec('{{\"type\":\"{written}\"}}')")
    assert "__err" not in s, f"{written}: {s.get('__err')}"
    assert s["type"] == expect


def test_hilbert_and_peano_rules_really_are_those_curves():
    """A space-filling curve has two signatures: an exact segment count and a
    square bounding box. Both are checked here on the turtle walk itself, so a
    typo in the rule strings cannot pass as 'some L-system that renders'."""
    js = """(()=>{
      const walk=s=>{let x=0,y=0,a=-Math.PI/2,n=0,
        mnx=1e9,mxx=-1e9,mny=1e9,mxy=-1e9;
        const see=(u,v)=>{if(u<mnx)mnx=u;if(u>mxx)mxx=u;if(v<mny)mny=v;if(v>mxy)mxy=v;};
        see(0,0);
        const pts=new Set(['0,0']);
        for(const c of s){
          if(c==='F'||c==='G'){x+=Math.cos(a);y+=Math.sin(a);n++;see(x,y);
                               pts.add(Math.round(x)+','+Math.round(y));}
          else if(c==='+')a+=Math.PI/2; else if(c==='-')a-=Math.PI/2;}
        return {n, u:pts.size, w:Math.round(mxx-mnx), h:Math.round(mxy-mny)};};
      const P=_FRAC_PRESETS;
      const H=o=>walk(_fracLExpand(P.hilbert.axiom,P.hilbert.rules,o,120000));
      const N=o=>walk(_fracLExpand(P.peano.axiom,P.peano.rules,o,120000));
      return {h1:H(1),h2:H(2),h4:H(4),p1:N(1),p2:N(2)};
    })()"""
    r = _val(js)
    # Distinct lattice points, not just the step count: a rule string that
    # retraces its own path has the right number of segments and the right box
    # while visiting only part of the grid, and the counts alone would pass it.
    assert (r["h1"]["u"], r["h2"]["u"], r["h4"]["u"]) == (4, 16, 256)     # 4^n
    assert (r["p1"]["u"], r["p2"]["u"]) == (9, 81)                       # 9^n
    # Hilbert of order n: 4^n - 1 unit steps across an (2^n - 1) square grid
    assert (r["h1"]["n"], r["h1"]["w"], r["h1"]["h"]) == (3, 1, 1)
    assert (r["h2"]["n"], r["h2"]["w"], r["h2"]["h"]) == (15, 3, 3)
    assert (r["h4"]["n"], r["h4"]["w"], r["h4"]["h"]) == (255, 15, 15)
    # Peano of order n: 9^n - 1 steps across a (3^n - 1) square grid
    assert (r["p1"]["n"], r["p1"]["w"], r["p1"]["h"]) == (8, 2, 2)
    assert (r["p2"]["n"], r["p2"]["w"], r["p2"]["h"]) == (80, 8, 8)


def test_every_curve_preset_expands_inside_the_symbol_cap_at_its_default_depth():
    # A preset that throws "reduce depth" out of the box would ship a red box.
    js = """(()=>{const out={};
      for(const [k,p] of Object.entries(_FRAC_PRESETS)){
        if(p.type!=='lsystem')continue;
        try{out[k]=_fracLExpand(p.axiom,p.rules,p.depth,_FRAC_MAX_LSYS).length;}
        catch(e){out[k]='THREW: '+e.message;}}
      return out;})()"""
    for name, size in _val(js).items():
        assert isinstance(size, int), f"{name} -> {size}"
        assert 0 < size <= 120000, f"{name} expanded to {size}"


# ── attractors ───────────────────────────────────────────────────────────────
def test_attractor_coefficients_are_clamped_and_unknown_keys_dropped():
    s = _val('_fracSpec(\'{"type":"lorenz","rho":1e9,"nonsense":5}\')')
    assert s["params"]["rho"] == 10000            # clamped, not passed through
    assert "nonsense" not in s["params"]          # only declared coefficients reach f()
    assert set(s["params"]) == {"sigma", "rho", "beta"}


def test_unknown_attractor_names_the_ones_that_exist():
    r = _val('_fracSpec(\'{"type":"attractor","system":"chua"}\')')
    assert "unknown attractor" in r["__err"] and "lorenz" in r["__err"]


def test_attractor_plane_is_whitelisted_and_defaults_per_system():
    assert _val("_fracSpec('{\"type\":\"lorenz\"}')")["plane"] == "xz"
    assert _val("_fracSpec('{\"type\":\"rossler\"}')")["plane"] == "xy"
    assert _val('_fracSpec(\'{"type":"lorenz","plane":"yz"}\')')["plane"] == "yz"
    assert _val('_fracSpec(\'{"type":"lorenz","plane":"; drop"}\')')["plane"] == "xz"


def test_a_diverging_orbit_keeps_its_bounded_prefix_and_drops_the_blow_up():
    """The earlier version of this test asked for coefficients that overflow
    immediately, so it measured n=0 and every assertion held vacuously — an empty
    array is trivially all-finite and trivially shorter than what was asked for.
    beta=-1 is the useful case: the orbit stays on the attractor for a while and
    only then runs away, which is what the retention logic is actually for."""
    r = _val('(()=>{const s=_fracSpec(\'{"type":"lorenz","beta":-1}\');'
             'const o=_fracOrbit(s); let mx=0;'
             'for(let i=0;i<o.n;i++)mx=Math.max(mx,Math.abs(o.xs[i]),Math.abs(o.ys[i]));'
             'return {n:o.n, asked:s.steps, peak:mx,'
             ' finite:[...o.xs.slice(0,o.n)].every(Number.isFinite)};})()')
    assert 0 < r["n"] < r["asked"], f"expected a partial orbit, got {r['n']}"
    assert r["finite"]
    # the retained part must be attractor-scale, not the runaway tail: before the
    # magnitude cut-off this same spec kept points out at 1e76 and the bounding
    # box they produced squashed the real orbit onto a single pixel.
    assert r["peak"] < 1e6, f"a runaway point survived (peak {r['peak']:g})"


def test_an_orbit_that_runs_away_before_it_starts_reports_instead_of_drawing():
    r = _val('(()=>{const o=_fracOrbit(_fracSpec(\'{"type":"thomas","b":-1}\'));'
             'return {n:o.n};})()')
    assert r["n"] < 2, "this spec should leave nothing to draw"


def test_every_attractor_preset_produces_a_bounded_non_degenerate_orbit():
    js = """(()=>{const out={};
      for(const [k,p] of Object.entries(_FRAC_PRESETS)){
        if(p.type!=='attractor')continue;
        const o=_fracOrbit(_fracSpec(JSON.stringify({type:k})));
        let mnx=1e9,mxx=-1e9;
        for(let i=0;i<o.n;i++){if(o.xs[i]<mnx)mnx=o.xs[i];if(o.xs[i]>mxx)mxx=o.xs[i];}
        out[k]={n:o.n,span:+(mxx-mnx).toFixed(3)};}
      return out;})()"""
    for name, r in _val(js).items():
        assert r["n"] > 1000, f"{name} orbit collapsed to {r['n']} points"
        assert 0.05 < r["span"] < 1e6, f"{name} span {r['span']} is degenerate"


def _halvorsen_lyapunov(a: float) -> float:
    """Largest Lyapunov exponent by two-orbit renormalisation.

    Two trajectories start 1e-9 apart; each step measures how far they have
    separated, accumulates the log of that stretch, and pulls the second back to
    the original distance so the estimate stays in the linear regime. Positive
    means chaos, ~0 means a closed orbit.
    """
    js = f"""(()=>{{
      const sys=_FRAC_ATTRACTORS.halvorsen, q={{a:{a}}}, dt=0.005, D0=1e-9;
      let p=[-1.48,-1.51,2.04];
      for(let i=0;i<20000;i++)p=_fracRK4(sys.f,p[0],p[1],p[2],dt,q);   // settle
      let r=[p[0]+D0,p[1],p[2]], sum=0; const N=60000;
      for(let i=0;i<N;i++){{
        p=_fracRK4(sys.f,p[0],p[1],p[2],dt,q);
        r=_fracRK4(sys.f,r[0],r[1],r[2],dt,q);
        const d=Math.hypot(r[0]-p[0],r[1]-p[1],r[2]-p[2]);
        if(!(d>0)||!isFinite(d))break;
        sum+=Math.log(d/D0);
        const k=D0/d;
        r=[p[0]+(r[0]-p[0])*k, p[1]+(r[1]-p[1])*k, p[2]+(r[2]-p[2])*k];
      }}
      return sum/(N*dt);}})()"""
    return _val(js)


def test_halvorsen_sits_in_a_chaotic_window_not_a_limit_cycle():
    """a=1.89 is the value most write-ups quote for this system, and under these
    equations it is a CLOSED orbit: the card would paint one plain loop while
    calling itself a strange attractor. a=1.4 is inside the chaotic window.

    Asserted on the Lyapunov exponent rather than on drawn-pixel growth, because
    the exponent is falsifiable in both directions — it fails if the shipped
    coefficient drifts back into a periodic window AND if it lands somewhere
    chaotic but different, where a pixel count would simply keep passing.
    """
    shipped = _val("_fracSpec('{\"type\":\"halvorsen\"}')")["params"]["a"]
    assert shipped == 1.4, f"the shipped coefficient moved to {shipped}"
    chaotic = _halvorsen_lyapunov(shipped)
    periodic = _halvorsen_lyapunov(1.89)
    assert chaotic > 0.3, f"a={shipped} is not chaotic (lambda={chaotic:.4f})"
    assert periodic < 0.05, f"a=1.89 is not periodic after all (lambda={periodic:.4f})"


# ── the drift that caused all of this ────────────────────────────────────────
def _preset_clause() -> str:
    """Just the 'PREFER A PRESET … :' list, not the whole bullet.

    16 of the 21 names appear twice in the bullet — once in the preset list and
    again in the order-limits or coefficients clause — so a name deleted from the
    list still matched somewhere and the drift this guards against went through.
    """
    from visualweaver import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    bullet = t[t.index("```fractal —"):t.index("\n", t.index("```fractal —"))]
    after = bullet[bullet.index("the name is all you need to get right:"):]
    return after[:after.index(".")]


def test_the_prompt_names_every_preset_the_renderer_can_draw():
    """The root cause of the report. The lane could draw far more than the seven
    names the prompt listed, so the model — told ```fractal is REQUIRED for every
    L-system and attractor — invented the vocabulary and hit an error box. A
    preset the model is never told about is a preset it can only reach by luck."""
    import re

    clause = _preset_clause()
    missing = [n for n in _preset_names()
               if not re.search(rf"\b{re.escape(n)}\b", clause, re.I)]
    assert not missing, f"presets the model is never told about: {missing}"


def test_every_name_the_prompt_offers_actually_renders():
    """The other direction, and the one that catches a promise the lane cannot
    keep: julia's `c` was dropped from the bullet in a rewrite while the parser
    still accepted it, which no presence check would ever have noticed."""
    import re

    known = set(_preset_names())
    offered = [w for w in re.findall(r"[a-z][a-z0-9-]{2,}", _preset_clause())
               if w in known or w in {"flowsnake", "snowflake"}]
    assert len(offered) >= len(known), f"only {len(offered)} names parsed out"
    for name in offered:
        s = _val(f"_fracSpec('{{\"type\":\"{name}\"}}')")
        assert "__err" not in s, f"the prompt offers {name!r} but: {s.get('__err')}"


def test_the_prompt_still_shows_julia_how_to_set_c():
    """A regression, not a hypothetical: the rewrite that added the preset list
    dropped `c` from the escape-time options while _fracSpec kept parsing it, so
    the one parameter that picks WHICH Julia set to draw became unreachable."""
    from visualweaver import constants
    t = constants.DEFAULT_BASE_INSTRUCTION
    bullet = t[t.index("```fractal —"):t.index("\n", t.index("```fractal —"))]
    assert '"c"' in bullet, "the fractal bullet never mentions julia's c"
    assert _val('_fracSpec(\'{"type":"julia","c":[-0.4,0.6]}\')')["c"] == [-0.4, 0.6]


def test_the_prompt_does_not_offer_coefficients_a_system_ignores():
    """rossler was documented as taking a,b,c,d alongside clifford and dejong. It
    has no d: the loop only reads coefficients the system declares, so the extra
    one was accepted and silently dropped."""
    declared = _val("(()=>{const o={};"
                    "for(const[k,v]of Object.entries(_FRAC_ATTRACTORS))o[k]=Object.keys(v.params);"
                    "return o;})()")
    assert declared["rossler"] == ["a", "b", "c"], declared["rossler"]
    assert set(declared["clifford"]) == {"a", "b", "c", "d"}
    s = _val('_fracSpec(\'{"type":"rossler","d":3}\')')
    assert "d" not in s["params"]


# The largest `order` each curve preset actually renders. Measured, not read off
# the depth clamp: the clamp allows 14, but most of these expansions blow through
# the 120000-symbol budget well before that and throw "reduce depth". The numbers
# are pinned here because DOCS.md and the model-facing prompt both quote them.
_MAX_ORDER = {"peano": 4, "gosper": 5, "moore": 6, "hilbert": 7, "koch": 7,
              "plant": 7, "frond": 7, "arrowhead": 10, "tree": 13, "dragon": 14, "levy": 14}


def test_documented_max_order_per_curve_is_what_the_lane_actually_renders():
    js = """(()=>{const out={};
      for(const [k,p] of Object.entries(_FRAC_PRESETS)){
        if(p.type!=='lsystem')continue;
        let last=0;
        for(let o=1;o<=14;o++){
          try{ const s=_fracSpec(JSON.stringify({type:k,order:o}));
               _fracLExpand(s.axiom,s.rules,s.depth,_FRAC_MAX_LSYS); last=o; }
          catch(e){ break; }}
        out[k]=last;}
      return out;})()"""
    measured = _val(js)
    assert measured == _MAX_ORDER, f"measured {measured}, pinned {_MAX_ORDER}"


def test_the_prompt_and_docs_quote_the_real_order_limits():
    """The limits are stated in two places the code cannot check for itself."""
    import pathlib
    import re

    from visualweaver import constants
    bullet = constants.DEFAULT_BASE_INSTRUCTION
    bullet = bullet[bullet.index("```fractal —"):bullet.index("\n", bullet.index("```fractal —"))]
    docs = (pathlib.Path(__file__).resolve().parents[1] / "DOCS.md").read_text(encoding="utf-8")
    section = docs[docs.index("### Fractals and attractors"):]
    section = section[:section.index("### Working with tables")]

    for name, limit in _MAX_ORDER.items():
        # the name and its number must appear together in one clause
        for where, text in (("prompt", bullet), ("DOCS.md", section)):
            near = re.search(rf"{name}[^.]*?{limit}\b|{limit}\b[^.]*?{name}", text, re.I)
            assert near, f"{where} does not state order ≤{limit} for {name}"



# ── deterministic depth-mode IFS (MRCM), offline ─────────────────────────────
# The box-zoom maths lives in pure helpers tested by test_fractal_zoom_js; the
# MRCM affine composition is likewise pure (_fracIFSLeaves), so it is checked
# here without a browser — the in-browser probe would skip where playwright is
# absent, leaving the composition unguarded.
def test_mrcm_depth1_places_the_three_corner_copies():
    leaves = _val("_fracIFSLeaves(_FRAC_PRESETS.sierpinski.maps,1,_FRAC_MAX_POINTS)")
    assert len(leaves) == 3
    assert sorted(round(l[0], 4) for l in leaves) == [0.5, 0.5, 0.5]   # each a half-scale copy
    trans = sorted((round(l[4], 4), round(l[5], 4)) for l in leaves)
    assert trans == [(0.0, 0.0), (0.25, 0.433), (0.5, 0.0)], trans      # 3 corners, apex at √3/4


def test_mrcm_nests_translations_with_depth():
    r = _val("(()=>{const L=_fracIFSLeaves(_FRAC_PRESETS.sierpinski.maps,2,_FRAC_MAX_POINTS);"
             "return {n:L.length, pos:[...new Set(L.map(t=>t[4].toFixed(5)+','+t[5].toFixed(5)))].length,"
             "scale:[...new Set(L.map(t=>t[0].toFixed(5)))]};})()")
    assert r["n"] == 9, r
    # Each depth-2 copy must sit at its OWN nested position; a compose that dropped
    # the accumulated translation would collapse all nine onto three (M424).
    assert r["pos"] == 9, f"depth-2 copies collapsed to {r['pos']} positions"
    assert r["scale"] == ["0.25000"], r["scale"]                        # 0.5**2


def test_mrcm_leaf_count_is_capped():
    # sierpinski (3 maps) tops out at depth 11 (3**11=177147 <= 200000)
    n = _val("_fracIFSLeaves(_FRAC_PRESETS.sierpinski.maps,11,_FRAC_MAX_POINTS).length")
    assert n == 3 ** 11
    capped = _val("_fracIFSLeaves(_FRAC_PRESETS.sierpinski.maps,20,_FRAC_MAX_POINTS).length")
    assert capped <= _val("_FRAC_MAX_POINTS") * 3   # the guard stops one pass past the cap


def test_ifs_render_mode_and_seed_per_preset():
    val = lambda s: _val(f"_fracSpec('{s}')")
    sp = val('{"type":"sierpinski"}'); assert sp["ifsMode"] == "depth" and sp["ifsSeed"] == "triangle", sp
    cp = val('{"type":"carpet"}');     assert cp["ifsMode"] == "depth" and cp["ifsSeed"] == "square", cp
    assert val('{"type":"fern"}')["ifsMode"] == "chaos"                 # Barnsley stays the IFS point cloud
    assert val('{"type":"ifs","maps":[[0.5,0,0,0.5,0,0]]}')["ifsMode"] == "chaos"
    assert val('{"type":"ifs","ctl":"depth","maps":[[0.5,0,0,0.5,0,0]]}')["ifsMode"] == "depth"


def test_frond_is_an_lsystem_distinct_from_the_barnsley_fern():
    assert _val('_fracSpec(\'{"type":"frond"}\').type') == "lsystem"
    assert _val('_fracSpec(\'{"type":"fern"}\').type') == "ifs"
    for alias in ("fern-lsystem", "lsystem-fern", "fern-curve"):
        assert _val(f'_fracSpec(JSON.stringify({{type:{alias!r}}})).type') == "lsystem", alias


# ── the remaining curve rule strings ─────────────────────────────────────────
# Only hilbert and peano had a mathematical test. The ones the code comment
# itself calls out as the risky rewrite — gosper and arrowhead, whose textbook
# form uses A and B as DRAWING symbols and had to be transcribed to F/G because
# this turtle only draws on F and G — were the untested ones.

_TURTLE = """
  const walk=(s,degrees)=>{
    const step=degrees*Math.PI/180;
    let x=0,y=0,a=0,n=0; const pts=new Set(['0.000,0.000']);
    const key=()=>x.toFixed(3)+','+y.toFixed(3);
    for(const c of s){
      if(c==='F'||c==='G'){x+=Math.cos(a);y+=Math.sin(a);n++;pts.add(key());}
      else if(c==='f'){x+=Math.cos(a);y+=Math.sin(a);}
      else if(c==='+')a+=step; else if(c==='-')a-=step;
    }
    return {n, u:pts.size, end:Math.hypot(x,y)};
  };
  const run=(name,order)=>{
    const p=_FRAC_PRESETS[name];
    return walk(_fracLExpand(p.axiom,p.rules,order,_FRAC_MAX_LSYS), p.angle);
  };
"""


def _curve(name: str, order: int):
    return _val(f"(()=>{{{_TURTLE} return run({name!r},{order});}})()")


@pytest.mark.parametrize("order", [1, 2, 3])
def test_gosper_closes_on_the_flowsnake_displacement(order):
    """The Gosper curve's end-to-end displacement is exactly sqrt(7)^n — that is
    what makes seven copies tile one scaled copy of themselves."""
    r = _curve("gosper", order)
    assert r["n"] == 7 ** order, f"segments {r['n']}"
    assert abs(r["end"] - 7 ** (order / 2)) < 1e-9, r["end"]


@pytest.mark.parametrize("order", [1, 2, 4, 8])
def test_levy_c_curve_spans_root_two_to_the_order(order):
    r = _curve("levy", order)
    assert r["n"] == 2 ** order
    assert abs(r["end"] - 2 ** (order / 2)) < 1e-9, r["end"]


@pytest.mark.parametrize("order", [1, 2, 3, 5])
def test_sierpinski_arrowhead_spans_two_to_the_order_without_retracing(order):
    r = _curve("arrowhead", order)
    assert r["n"] == 3 ** order
    assert abs(r["end"] - 2 ** order) < 1e-9, r["end"]
    # every step lands somewhere new — the arrowhead never doubles back
    assert r["u"] == r["n"] + 1, f"{r['n'] + 1 - r['u']} points revisited"


@pytest.mark.parametrize("order", [1, 2, 3])
def test_moore_curve_closes_on_itself(order):
    """A Moore curve is the closed variant of Hilbert: it returns to within one
    unit of where it started.

    It covers 4^(n+1) points, not 4^n — its axiom is already four Hilbert
    quadrants joined up, so a Moore curve of order n fills the grid a Hilbert
    curve reaches at order n+1.
    """
    r = _curve("moore", order)
    assert r["u"] == 4 ** (order + 1), f"{r['u']} distinct points"
    assert r["n"] == 4 ** (order + 1) - 1, f"{r['n']} segments"
    assert abs(r["end"] - 1) < 1e-9, f"loop does not close (end {r['end']})"


@pytest.mark.parametrize("order", [1, 2, 3, 6])
def test_binary_tree_doubles_its_branches_each_order(order):
    branches = _val(f"(()=>{{const p=_FRAC_PRESETS.tree;"
                    f"const s=_fracLExpand(p.axiom,p.rules,{order},_FRAC_MAX_LSYS);"
                    f"return (s.match(/\\[/g)||[]).length;}})()")
    assert branches == 2 ** order - 1, branches


def test_koch_and_dragon_still_expand_as_they_always_did():
    # a guard on the two presets that predate all of this
    assert _curve("koch", 2)["n"] == 3 * 4 ** 2
    assert _curve("dragon", 6)["n"] == 2 ** 6


# ── the integrator ───────────────────────────────────────────────────────────
def test_fracRK4_is_fourth_order_on_a_system_with_a_closed_form():
    """dx/dt = x on [0,1] has the exact solution e. Halving the step must cut the
    error by ~16x for a fourth-order method; a mis-weighted stage would show up
    immediately as a lower order."""
    js = """(()=>{
      const f=(x,y,z)=>[x,0,0];
      const solve=n=>{let x=1;const dt=1/n;
        for(let i=0;i<n;i++)x=_fracRK4(f,x,0,0,dt,{})[0];
        return Math.abs(x-Math.E);};
      const a=solve(10), b=solve(20);
      return {a,b,ratio:a/b};})()"""
    r = _val(js)
    assert r["a"] < 1e-4, f"RK4 error too large at n=10: {r['a']:g}"
    assert 12 < r["ratio"] < 20, f"order looks wrong (error ratio {r['ratio']:.1f}, expected ~16)"


# ── clamps on the attractor branch ───────────────────────────────────────────
def test_attractor_steps_are_clamped():
    lo = _val('_fracSpec(\'{"type":"lorenz","iter":1}\')')
    hi = _val('_fracSpec(\'{"type":"lorenz","iter":99999999}\')')
    assert lo["steps"] == 500 and hi["steps"] == 200000
    # a map has no integration step at all
    assert _val('_fracSpec(\'{"type":"clifford","dt":0.05}\')')["dt"] == 0


@pytest.mark.parametrize("system", ["lorenz", "rossler", "thomas", "halvorsen"])
def test_no_dt_the_clamp_allows_can_fail_to_integrate(system):
    """The ceiling used to be a flat 0.2 for every system, and 0.2 is a step
    NONE of them survives: asking for the largest value the clamp permitted
    returned an empty orbit, and the card then reported that the coefficients had
    diverged — blaming the spec for the step size.

    These differ by two orders of magnitude in stiffness, so the bound is now
    relative to each system's own default. Every value the clamp accepts has to
    produce a full orbit.
    """
    js = """(({sys,dt})=>{
      const s=_fracSpec(JSON.stringify(dt===null?{type:sys}:{type:sys,dt}));
      const o=_fracOrbit(s);
      let a=1e308,b=-1e308;
      for(let i=0;i<o.n;i++){if(o.xs[i]<a)a=o.xs[i];if(o.xs[i]>b)b=o.xs[i];}
      return {dt:s.dt, n:o.n, asked:s.steps, span:o.n?+(b-a).toPrecision(4):0};
    })"""
    for asked in ("null", "0.2", "99", "1e-9"):
        r = _val(f'{js}({{sys:{system!r},dt:{asked}}})')
        assert r["n"] == r["asked"], (
            f"dt={asked} clamped to {r['dt']} produced {r['n']} of {r['asked']} points")
        assert 0.5 < r["span"] < 1e5, f"dt={asked} gave a degenerate span {r['span']}"


# ── the tables are looked up as own properties ───────────────────────────────
@pytest.mark.parametrize("hostile", ["constructor", "__proto__", "toString",
                                     "valueOf", "hasOwnProperty"])
def test_prototype_keys_are_rejected_not_resolved(hostile):
    """`_FRAC_ATTRACTORS['constructor']` is Object off the prototype chain: it
    passed the truthiness guard and then threw out of Object.entries(sys.params).
    Same class the RICH_LANES lookup already guards."""
    r = _val(f'_fracSpec(\'{{"type":"{hostile}"}}\')')
    assert "unknown fractal type" in r.get("__err", ""), r
    r2 = _val(f'_fracSpec(\'{{"type":"attractor","system":"{hostile}"}}\')')
    assert "unknown attractor" in r2.get("__err", ""), r2


# ── aliases resolve as SPECS, not just as table entries ──────────────────────
def test_every_alias_key_renders_through_fracSpec():
    """The earlier test only checked that alias targets name real presets, so a
    key that could never be reached — `mandelbrot_set`, unreachable because the
    separator fold rewrites `_` to `-` before any lookup — stayed invisible."""
    keys = _val("Object.keys(_FRAC_ALIASES)")
    for alias in keys:
        s = _val(f"_fracSpec(JSON.stringify({{type:{alias!r}}}))")
        assert "__err" not in s, f"alias {alias!r}: {s.get('__err')}"
