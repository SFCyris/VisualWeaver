# SPDX-License-Identifier: AGPL-3.0-or-later
"""The science lanes' pure helpers, run offline under node.

```plotly, ```ode, ```sequence, ```phylo, ```circuit and the ```plot extensions
(parametric, polar, field, contour, implicit) are each a pure function from source
text to SVG or a cleaned spec, with no DOM. Those functions are extracted from
index.html by brace balance and driven here with a small math.js stand-in, so the
geometry, parsing and error paths are pinned without a browser. What the browser
does with the output (Plotly, three.js, SmilesDrawer, sliders, Apply/Reset) is
covered by tests/test_science_lanes_browser.py.
"""
import json
import math
import pathlib
import re
import shutil

import pytest

try:
    from _jsrun import run_node, extract_js_function
except ImportError:  # pragma: no cover — depends on how pytest sets sys.path
    from tests._jsrun import run_node, extract_js_function

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "visualweaver" / "index.html"

_FNS = ["_plotNum", "_plotTicks", "_plotFmt", "_plotEsc", "_plotDefLabel",
        "_marchingSquares", "_fracRK4", "_plotUndefinedErr", "_plotPair", "_odeParse", "_odeSvg",
        "_buildFunctionPlot", "_newickParse", "_phyloSvg", "_seqParse", "_seqSvg",
        "_circuitSvg", "_plotlyClean"]

# math.js stand-in: `compile(expr).evaluate(scope)` over plain JS arithmetic. `^`
# is exponentiation as in math.js, and a symbol missing from the scope throws —
# the one behaviour the undefined-symbol path depends on.
_MATH_STUB = r"""
const _MF={sin:Math.sin,cos:Math.cos,tan:Math.tan,exp:Math.exp,sqrt:Math.sqrt,abs:Math.abs,log:Math.log,max:Math.max,min:Math.min,pi:Math.PI,e:Math.E};
const _src=ex=>String(ex).replace(/\^/g,'**').replace(/θ/g,'theta');
const math={
  evaluate:(s)=>{ try{ return Function(...Object.keys(_MF),'return ('+_src(s)+')')(...Object.values(_MF)); }catch(e){ return NaN; } },
  compile:(ex)=>{ const f=Function(...Object.keys(_MF),'sc','with(sc){ return ('+_src(ex)+'); }');
    return {evaluate:(sc)=>f(...Object.values(_MF),sc||{})}; },
};
"""

_harness_cache: list = []


def _harness() -> str:
    if _harness_cache:
        return _harness_cache[0]
    html = _INDEX.read_text(encoding="utf-8")
    m = re.search(r"^const _PLOT_DEF=(/.+/);$", html, re.M)
    assert m, "the shared definition-line regex is gone"
    nt = re.search(r"^const _SEQ_NT=\{.*?\};$", html, re.M)
    aa = re.search(r"^const _SEQ_AA=\{.*?\};$", html, re.M | re.S)
    assert nt and aa, "the sequence palettes are gone"
    js = _MATH_STUB + f"const _PLOT_DEF={m.group(1)};\n" + nt.group(0) + "\n" + aa.group(0) + "\n"
    js += "\n".join(extract_js_function(html, f) for f in _FNS) + "\n"
    _harness_cache.append(js)
    return js


def _call(body: str):
    """Run `body` — a JS function body returning a JSON-able value — with the helpers loaded."""
    if not shutil.which("node"):
        pytest.skip("node not available")
    js = _harness() + "process.stdout.write(JSON.stringify((()=>{\n" + body + "\n})()));\n"
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1500]}"
    return json.loads(r.stdout)


def _catch(expr: str):
    """{ok: value} or {err: message}."""
    return _call(f"try{{ return {{ok: {expr} }}; }}catch(e){{ return {{err:String(e.message)}}; }}")


def _err(expr: str) -> str:
    d = _catch(expr)
    assert "err" in d, f"expected an error, got {d}"
    return d["err"]


def _ok(expr: str):
    d = _catch(expr)
    assert "ok" in d, f"unexpected error: {d.get('err')}"
    return d["ok"]


def _path_points(d: str):
    return [(float(x), float(y)) for x, y in re.findall(r"[ML]\s*(-?[\d.]+)\s+(-?[\d.]+)", d)]


def _paths(svg: str, stroke: str):
    return re.findall(r'<path d="([^"]*)" fill="none" stroke="' + re.escape(stroke), svg)


# ── marching squares ─────────────────────────────────────────────────────────
def test_marching_squares_traces_the_unit_circle():
    segs = _call("""
      const xs=[],ys=[]; for(let i=0;i<=40;i++){ xs.push(-2+4*i/40); ys.push(-2+4*i/40); }
      const g=ys.map(y=>xs.map(x=>x*x+y*y-1));
      return _marchingSquares(g,xs,ys,0);""")
    assert len(segs) > 40
    for x1, y1, x2, y2 in segs:
        for x, y in ((x1, y1), (x2, y2)):
            assert abs(math.hypot(x, y) - 1) < 0.03, (x, y)


def test_marching_squares_saddle_cell_gives_two_segments_and_flat_cells_none():
    d = _call("""return {
      saddle: _marchingSquares([[1,0],[0,1]],[0,1],[0,1],0.5).length,
      above:  _marchingSquares([[5,5],[5,5]],[0,1],[0,1],0).length,
      below:  _marchingSquares([[0,0],[0,0]],[0,1],[0,1],1).length,
      nan:    _marchingSquares([[NaN,1],[1,1]],[0,1],[0,1],0.5).length };""")
    assert d == {"saddle": 2, "above": 0, "below": 0, "nan": 0}


def test_marching_squares_interpolates_the_crossing_not_the_cell_edge():
    # f = x on [0,1]: the level 0.25 crossing is at x=0.25, not at a grid line.
    segs = _call("return _marchingSquares([[0,1],[0,1]],[0,1],[0,1],0.25);")
    assert len(segs) == 1
    x1, _, x2, _ = segs[0]
    assert abs(x1 - 0.25) < 1e-9 and abs(x2 - 0.25) < 1e-9


# ── Newick ───────────────────────────────────────────────────────────────────
def test_newick_parses_nesting_names_and_branch_lengths():
    t = _ok("_newickParse('((Human:0.1,Chimp:0.12)hominini:0.3,(Mouse:0.4,Rat:0.38):0.2);')")
    assert [c["name"] for c in t["children"]] == ["hominini", ""]
    assert t["children"][0]["length"] == 0.3
    assert [l["name"] for l in t["children"][0]["children"]] == ["Human", "Chimp"]
    assert t["children"][1]["children"][1]["length"] == 0.38


def test_newick_accepts_quoted_names_comments_and_missing_lengths():
    t = _ok("_newickParse(\"('Homo sapiens'[&support=99]:0.5,B);\")")
    assert t["children"][0]["name"] == "Homo sapiens"
    assert t["children"][0]["length"] == 0.5
    assert t["children"][1]["length"] is None
    # the standard '' escape for an apostrophe inside a quoted label
    assert _ok("_newickParse(\"('O''Brien':1,B);\")")["children"][0]["name"] == "O'Brien"


def test_long_taxon_names_widen_the_tree_instead_of_clipping():
    long = "Pseudomonas_aeruginosa_strain_PAO1_isolate_2019_x"   # 50 chars
    svg = _ok(f"_phyloSvg(_newickParse('(({long}:0.1,B:0.2),C:0.3);'))")
    w = int(re.search(r'<svg width="(\d+)"', svg).group(1))
    assert w >= 420 + 7 * len(long) + 20, w
    lx = float(re.search(r'<text x="([\d.]+)" y="[\d.]+" fill="#1e293b">' + long, svg).group(1))
    assert lx + 7 * len(long) <= w, "the long name still runs off the right edge"


@pytest.mark.parametrize("bad", ["", ";", "(A,B))", "(A,B"])
def test_newick_rejects_malformed_input(bad):
    assert _err(f"_newickParse({json.dumps(bad)})")


# ── phylogram / cladogram ────────────────────────────────────────────────────
def test_phylogram_has_a_leaf_per_taxon_evenly_spaced_and_a_scale_bar():
    svg = _ok("_phyloSvg(_newickParse('((Human:0.1,Chimp:0.12)hominini:0.3,(Mouse:0.4,Rat:0.38):0.2);'))")
    cys = [float(v) for v in re.findall(r'<circle cx="[\d.]+" cy="([\d.]+)"', svg)]
    assert len(cys) == 4
    gaps = {round(b - a, 3) for a, b in zip(cys, cys[1:])}
    assert len(gaps) == 1, f"leaves are not evenly spaced: {cys}"
    assert ">Human<" in svg and ">Rat<" in svg and ">hominini<" in svg
    assert ">0.1<" in svg, "no scale bar for a tree with branch lengths"
    assert svg.count("<line") == 10   # 3 internal nodes × (1 vertical + 2 horizontal) + the scale bar


def test_cladogram_has_no_scale_bar_and_uniform_depth():
    svg = _ok("_phyloSvg(_newickParse('((A,B),(C,D));'))")
    assert svg.count("<circle") == 4 and svg.count("<line") == 9
    assert svg.count("<text") == 4, "a cladogram must not draw a scale bar"
    cxs = {v for v in re.findall(r'<circle cx="([\d.]+)"', svg)}
    assert len(cxs) == 1, f"leaves at uniform depth should share one x: {cxs}"


def test_a_longer_branch_puts_its_leaf_further_right():
    svg = _ok("_phyloSvg(_newickParse('((A:0.1,B:0.5),C:0.2);'))")
    cx = [float(v) for v in re.findall(r'<circle cx="([\d.]+)"', svg)]   # draw order A, B, C
    assert cx[1] > cx[2] > cx[0], cx


def test_phylo_escapes_names_and_caps_the_leaf_count():
    svg = _ok("_phyloSvg(_newickParse(\"('<b>&x':1,B:1);\"))")
    assert "<b>" not in svg and "&lt;b>&amp;x" in svg
    big = "(" + ",".join(f"L{i}:1" for i in range(201)) + ");"
    assert "200" in _err(f"_phyloSvg(_newickParse({json.dumps(big)}))")


# ── sequences ────────────────────────────────────────────────────────────────
def test_seq_parse_reads_fasta_bare_text_and_strips_numbering():
    d = _call("""return {
      fasta: _seqParse(">s1\\nAC GT 10 ac\\n>s2\\nACTT\\n"),
      bare: _seqParse("acgt"),
      empty: _seqParse(">only a header\\n"),
      capped: _seqParse('A'.repeat(6000))[0].seq.length };""")
    assert d["fasta"] == [{"name": "s1", "seq": "ACGTAC"}, {"name": "s2", "seq": "ACTT"}]
    assert d["bare"] == [{"name": "", "seq": "ACGT"}]
    assert d["empty"] == [] and d["capped"] == 5000


def test_seq_parse_separates_bare_sequences_on_blank_lines():
    d = _call("""return { two: _seqParse("ACGTACGT\\n\\nTTTTGGGG"), wrapped: _seqParse("ACGT\\nACGT") };""")
    assert [r["seq"] for r in d["two"]] == ["ACGTACGT", "TTTTGGGG"]
    assert [r["name"] for r in d["two"]] == ["seq1", "seq2"], "headerless records need names for the alignment"
    assert d["wrapped"] == [{"name": "", "seq": "ACGTACGT"}]


def test_seq_parse_rejects_text_that_is_not_a_sequence():
    e = _err("_seqParse('A->>B: hi')")
    assert "not a residue" in e and "mermaid" in e
    assert _ok("_seqParse('ACGT-N*')")[0]["seq"] == "ACGT-N*"


def test_seq_svg_picks_the_nucleotide_or_protein_palette():
    d = _call("""return { nt: _seqSvg([{name:'',seq:'ACGT'}]), aa: _seqSvg([{name:'',seq:'MKV'}]) };""")
    assert d["nt"].count("<rect") == 5           # background + one cell per base
    fills = re.findall(r'<rect [^>]*fill="(#[0-9a-f]{6})"', d["nt"])[1:]   # skip the background
    assert fills == ["#86efac", "#93c5fd", "#fde68a", "#fca5a5"], fills   # A C G T, nucleotide palette
    assert 'fill="#93c5fd"' in d["aa"] and 'fill="#fde68a"' in d["aa"]   # K basic, M/V hydrophobic
    assert 'fill="#86efac"' not in d["aa"]


def test_an_alignment_marks_the_differing_columns():
    svg = _ok("_seqSvg([{name:'a',seq:'ACGT'},{name:'b',seq:'ACTT'}])")
    assert svg.count(">*<") == 3 and svg.count(">·<") == 1
    assert ">a<" in svg and ">b<" in svg


def test_an_alignment_wraps_in_blocks_of_sixty_columns():
    """Two 150-column sequences: three blocks of 60, each with its own name column
    and marker row, in an SVG no wider than 60 cells — not one 2,100-px-wide strip."""
    svg = _ok("_seqSvg([{name:'a',seq:'A'.repeat(150)},{name:'b',seq:'A'.repeat(149)+'C'}])")
    w = int(re.search(r'<svg width="(\d+)"', svg).group(1))
    assert w == 19 + 8 + 60 * 14 + 8, w          # nameW(19) + 8 + 60 cells + 8
    assert svg.count(">a<") == 3 and svg.count(">b<") == 3
    assert svg.count(">*<") == 149 and svg.count(">·<") == 1
    assert 'role="img"' in svg


def test_a_long_single_sequence_wraps_and_escapes_its_name():
    d = _call("""return { long: _seqSvg([{name:'',seq:'A'.repeat(80)}]), esc: _seqSvg([{name:'a<b',seq:'AC'},{name:'c',seq:'AC'}]) };""")
    assert d["long"].count("<rect") == 81 and 'height="120"' in d["long"]   # 3 rows of 36
    assert "a&lt;b" in d["esc"] and "<b" not in d["esc"].replace("a&lt;b", "")


# ── circuit ──────────────────────────────────────────────────────────────────
def test_circuit_rejects_an_empty_or_oversized_netlist():
    assert "components" in _err("_circuitSvg({})")
    assert "200" in _err("_circuitSvg({components:Array.from({length:201},()=>({type:'wire',from:[0,0],to:[1,0]}))})")


def test_circuit_draws_symbols_at_midpoints_rotated_along_the_segment():
    svg = _ok("""_circuitSvg({components:[
      {type:'resistor',from:[0,0],to:[3,0],label:'R1'},
      {type:'capacitor',from:[0,0],to:[1,1]},
      {type:'wire',from:[3,0],to:[3,2]},
      {type:'foo',from:[0,2],to:[3,2],label:'<x>'}]})""")
    assert svg.count("<g transform=") == 2, "one symbol group per non-wire component"
    assert "rotate(0.0)" in svg and "rotate(-45.0)" in svg   # y grows upward: (0,0)→(1,1) rises on screen
    assert ">R1<" in svg and "&lt;x>" in svg and "<x>" not in svg


def test_circuit_y_grows_upward():
    """Graph-paper convention, as the ```scene lane already states: a wire from
    y=0 to y=2 goes UP the page, and a ground at the lowest y sits at the bottom."""
    svg = _ok("_circuitSvg({components:[{type:'wire',from:[0,0],to:[0,2]},{type:'ground',at:[1,0]}]})")
    y1, y2 = [float(v) for v in re.search(r'<line x1="[\d.]+" y1="([\d.]+)" x2="[\d.]+" y2="([\d.]+)"', svg).groups()]
    assert y1 > y2, f"the wire from y=0 to y=2 runs downward on screen ({y1} -> {y2})"
    # the ground glyph is drawn below its point (bars fan out downward on screen)
    gys = [float(v) for v in re.findall(r'<line x1="[\d.]+" y1="([\d.]+)"', svg)[1:]]   # the four ground lines
    assert min(gys) >= y1 - 0.1, f"ground bars {gys} should hang below the ground point at {y1}"
    assert 'r="3.5"' not in svg


def test_circuit_shrinks_a_symbol_to_fit_a_short_segment():
    svg = _ok("_circuitSvg({components:[{type:'resistor',from:[0,0],to:[0.3,0]},{type:'resistor',from:[0,1],to:[3,1]}]})")
    scales = re.findall(r"scale\(([\d.]+)\)", svg)
    assert len(scales) == 1 and 0.15 <= float(scales[0]) < 0.5, scales   # only the short one is scaled
    assert "constructor" not in _ok("_circuitSvg({components:[{type:'constructor',from:[0,0],to:[1,0]}]})")


def test_circuit_labels_clear_their_symbol():
    """A label sits beyond the symbol's half-extent across the wire: on a vertical
    battery (plates ±14 px) the text is centred ≥ 26 px from the axis, not on it."""
    svg = _ok("_circuitSvg({components:[{type:'battery',from:[0,2],to:[0,0],label:'9 V'},{type:'resistor',from:[0,0],to:[3,0],label:'R1'}]})")
    groups = re.findall(r'<g transform="translate\(([\d.]+) ([\d.]+)\) rotate\((-?[\d.]+)\)', svg)
    (bx, by, _), (rx, ry0, _) = [g for g in groups if abs(float(g[2])) == 90][0], [g for g in groups if float(g[2]) == 0][0]
    lx = float(re.search(r'<text x="([\d.]+)" y="[\d.]+" text-anchor="middle" fill="#1f2937">9 V</text>', svg).group(1))
    assert abs(lx - float(bx)) >= 26, f"battery label {lx} sits {abs(lx-float(bx)):.0f} px from the axis at {bx}"
    ry = float(re.search(r'<text x="[\d.]+" y="([\d.]+)" text-anchor="middle" fill="#1f2937">R1</text>', svg).group(1))
    # the zigzag reaches ±8 across the axis; the label baseline sits 4 below its anchor
    assert abs(ry - 4 - float(ry0)) >= 20, f"resistor label at y={ry} overlaps the zigzag on the axis at {ry0}"


def test_circuit_marks_a_tee_onto_the_middle_of_a_segment():
    """A capacitor tapped onto the middle of a resistor's run: only two endpoints
    meet there, but it is a junction and gets the dot; its free end does not."""
    svg = _ok("_circuitSvg({components:[{type:'resistor',from:[0,0],to:[3,0]},{type:'capacitor',from:[1.5,0],to:[1.5,2]}]})")
    assert svg.count('r="3.5"') == 1


def test_circuit_marks_junctions_of_three_or_more_and_draws_ground():
    d = _call("""return {
      three: _circuitSvg({components:[{type:'wire',from:[0,0],to:[1,0]},{type:'wire',from:[0,0],to:[0,1]},{type:'wire',from:[0,0],to:[-1,0]}]}),
      two:   _circuitSvg({components:[{type:'wire',from:[0,0],to:[1,0]},{type:'wire',from:[0,0],to:[0,1]}]}),
      gnd:   _circuitSvg({components:[{type:'ground',at:[1,1]}]}) };""")
    assert d["three"].count('r="3.5"') == 1
    assert 'r="3.5"' not in d["two"]
    assert d["gnd"].count("<line") == 4 and 'width="220"' in d["gnd"]


# ── ode: parsing ─────────────────────────────────────────────────────────────
def test_ode_parse_reads_every_line_form():
    o = _ok("""_odeParse(`# pendulum
      dx/dt = y
      y' = -sin(x) - c*y
      x from -3 to 3
      y = -2 .. 2
      start: 1, 0
      initial: (2, 1)
      start: pi, 0
      title: damped pendulum
      // a note
      steps: 3
      dt: 0.5
      arrows: 100
      param: c = 0 .. 1 (0.2)`)""")
    assert o["fx"] == "y" and o["fy"] == "-sin(x) - c*y"
    assert o["xr"] == [-3, 3] and o["yr"] == [-2, 2]
    assert o["starts"][:2] == [[1, 0], [2, 1]] and abs(o["starts"][2][0] - math.pi) < 1e-9
    assert o["steps"] == 10 and o["dt"] == 0.5 and o["arrows"] == 40


def test_ode_parse_clamps_and_rejects():
    d = _call("""return {
      lo: _odeParse("dx = 1\\ndy = 1\\narrows: 2\\nsteps: 99999\\ndt: 2"),
      starts: _odeParse("dx = 1\\ndy = 1\\n"+Array.from({length:15},(_,i)=>'start: '+i+', 0').join('\\n')).starts.length };""")
    assert d["lo"]["arrows"] == 6 and d["lo"]["steps"] == 5000 and d["lo"]["dt"] == 0.02
    assert d["starts"] == 12
    assert "dy" in _err("_odeParse('dx = y')")
    assert "cannot read" in _err("_odeParse('dx = y\\ndy = -x\\ndz = 1')")          # an unknown EQUATION is a mistake
    assert "start point" in _err("_odeParse('dx = y\\ndy = -x\\nstart: a, 0')")
    assert _ok("_odeParse('dx = y\\ndy = -x\\nfoo: 3')")["fx"] == "y"                 # a label line is not
    assert _ok("_odeParse('dx = y\\ndy = -x\\nx = 3 .. 1')")["xr"] == [1, 3]        # a descending range is a range


# ── ode: the drawing ─────────────────────────────────────────────────────────
def test_ode_svg_draws_the_field_and_a_two_way_trajectory_inside_the_window():
    svg = _ok("_odeSvg('dx = y\\ndy = -x\\nstart: 2, 0', {})")
    field = re.search(r'<g stroke="#94a3b8"[^>]*>(.*?)</g>', svg, re.S).group(1)
    assert field.count("<line") == 18 * 18
    traj = _paths(svg, "#2563eb")
    assert len(traj) == 2, "forward and backward integration from the start point"
    for d in traj:
        pts = _path_points(d)
        assert len(pts) > 50
        for x, y in pts:
            assert 47.9 <= x <= 624.1 and 15.9 <= y <= 326.1, (x, y)
    assert svg.count("<circle") == 1


def test_ode_field_arrows_follow_the_flow_in_screen_space():
    """dx=1, dy=0 is a horizontal flow: every arrow must be a horizontal line."""
    svg = _ok("_odeSvg('dx = 1\\ndy = 0', {})")
    field = re.search(r'<g stroke="#94a3b8"[^>]*>(.*?)</g>', svg, re.S).group(1)
    lines = re.findall(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', field)
    assert lines and all(y1 == y2 and float(x2) > float(x1) for x1, y1, x2, y2 in lines)


def test_ode_each_start_point_gets_its_own_colour():
    svg = _ok("_odeSvg('dx = y\\ndy = -x\\nstart: 1, 0\\nstart: 2, 0', {})")
    assert len(_paths(svg, "#2563eb")) == 2 and len(_paths(svg, "#dc2626")) == 2


def test_ode_names_an_undeclared_symbol_instead_of_drawing_an_empty_window():
    e = _err("_odeSvg('dx = k*y\\ndy = -x', {})")
    assert "k" in e and "not defined" in e
    assert _ok("_odeSvg('dx = k*y\\ndy = -x', {k:1})").count("<path") > 2


# ── plot: parametric / polar / field / contour / implicit ────────────────────
def test_parametric_curve_is_framed_to_fit_and_labelled():
    svg = _ok("_buildFunctionPlot('x = cos(t)\\ny = sin(t)', {})")
    assert "(x(t), y(t))" in svg
    pts = _path_points(_paths(svg, "#2563eb")[0])
    xs = [p[0] for p in pts]
    # auto-frame: [-1, 1] padded by 8% of the span → x=-1 lands at sx = 52 + 0.16/2.32·572 = 91.4
    assert min(xs) < 92 and max(xs) > 584, "the circle should span the auto-framed window"


def test_a_descending_domain_is_accepted():
    """`x = 5 .. -5` used to fall through to the parametric rule and die on
    `cannot parse "5 .. -5"`; it is the same window as -5 .. 5."""
    a = _ok("_buildFunctionPlot('y = x^2\\nx = 5 .. -5', {})")
    b = _ok("_buildFunctionPlot('y = x^2\\nx = -5 .. 5', {})")
    assert _path_points(_paths(a, "#2563eb")[0]) == _path_points(_paths(b, "#2563eb")[0])


def test_r_is_polar_only_with_theta():
    plain = _ok("_buildFunctionPlot('r = 0.05\\nx = 0 .. 10', {})")
    assert "r(θ)" not in plain and len(_paths(plain, "#2563eb")) == 1
    ys = {round(y, 1) for _, y in _path_points(_paths(plain, "#2563eb")[0])}
    assert len(ys) == 1, "a constant function named r must draw a flat line, not a polar circle"
    assert "r(θ)" in _ok("_buildFunctionPlot('r = 1 + cos(θ)', {})")


def test_parametric_x_without_y_is_an_error():
    assert "parametric" in _err("_buildFunctionPlot('x = cos(t)', {})")


def test_polar_curve_is_drawn_and_labelled():
    svg = _ok("_buildFunctionPlot('r = 1 + cos(theta)', {})")
    assert "r(θ)" in svg and len(_paths(svg, "#2563eb")) == 1
    # a cardioid crosses x=0 at y=1, 0 and -1: the same column holds points far
    # apart vertically, which no collinear (r·cos, r·cos) mistake can produce
    cols = {}
    for X, Y in _path_points(_paths(svg, "#2563eb")[0]):
        cols.setdefault(round(X / 4), []).append(Y)
    assert max(max(v) - min(v) for v in cols.values()) > 80, "the polar curve is collinear"


def test_field_components_may_contain_calls_and_commas():
    d = _call("""return { call: _buildFunctionPlot('field: 1, cos(x)\\nx = -6 .. 6', {}),
                         comma: _buildFunctionPlot('field: max(x, y), 1\\nx = -2 .. 2', {}),
                         parens: _buildFunctionPlot('field: (x, y)\\nx = -2 .. 2', {}) };""")
    for k, svg in d.items():
        g = re.search(r'<g stroke="#64748b"[^>]*>(.*?)</g>', svg, re.S)
        assert g and g.group(1).count("<line") == 256, k
    assert _err("_buildFunctionPlot('field: x', {})")


def test_ode_field_arrows_point_upward_for_positive_dy():
    """dx=0, dy=1 flows up the page: every arrow is vertical with its head ABOVE its tail."""
    svg = _ok("_odeSvg('dx = 0\\ndy = 1', {})")
    field = re.search(r'<g stroke="#94a3b8"[^>]*>(.*?)</g>', svg, re.S).group(1)
    lines = re.findall(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', field)
    assert lines and all(x1 == x2 and float(y2) < float(y1) for x1, y1, x2, y2 in lines)


def test_vector_field_draws_a_16_by_16_grid_oriented_in_screen_space():
    d = _call("""return { rot: _buildFunctionPlot('field: -y, x\\nx = -2 .. 2\\ny = -2 .. 2', {}),
                         flat: _buildFunctionPlot('field: 1, 0\\nx = -2 .. 2', {}) };""")
    g = re.search(r'<g stroke="#64748b"[^>]*>(.*?)</g>', d["rot"], re.S).group(1)
    assert g.count("<line") == 256
    flat = re.search(r'<g stroke="#64748b"[^>]*>(.*?)</g>', d["flat"], re.S).group(1)
    lines = re.findall(r'<line x1="([\d.]+)" y1="([\d.]+)" x2="([\d.]+)" y2="([\d.]+)"', flat)
    assert lines and all(l[1] == l[3] and float(l[2]) > float(l[0]) for l in lines)
    # an overlay with no y-window mirrors the x window
    assert ">-2<" in d["flat"] and ">2<" in d["flat"]


def test_contour_draws_eight_levels_and_implicit_one_red_curve():
    d = _call("""return { c: _buildFunctionPlot('contour: x^2 + y^2\\nx = -2 .. 2\\ny = -2 .. 2', {}),
                         i: _buildFunctionPlot('implicit: x^2 + y^2 = 4\\nx = -3 .. 3\\ny = -3 .. 3', {}) };""")
    assert d["c"].count('stroke="hsl(') == 8
    red = _paths(d["i"], "#dc2626")
    assert len(red) == 1
    for X, Y in _path_points(red[0]):
        x = (X - 52) / 572 * 6 - 3
        y = 3 - (Y - 22) / 294 * 6
        assert abs(math.hypot(x, y) - 2) < 0.15, (x, y)


def test_a_stated_y_window_clips_the_series():
    svg = _ok("_buildFunctionPlot('y = 100*x\\nx = 0 .. 1\\ny = -5 .. 5', {})")
    pts = _path_points(_paths(svg, "#2563eb")[0])
    assert pts and all(21.9 <= y <= 316.1 for _, y in pts)
    # only x ≤ 0.05 lies inside y ≤ 5: ~16 of the 301 samples survive the clip
    assert len(pts) < 30, f"{len(pts)} points drawn — the y window did not clip"
    assert abs(pts[0][1] - 169) < 1.5, "y=0 must sit at sy(0)=169 in a [-5, 5] window"


def test_field_and_contour_name_an_undeclared_symbol():
    assert "not defined" in _err("_buildFunctionPlot('field: k*x, y', {})")
    assert "not defined" in _err("_buildFunctionPlot('contour: k*x*y', {})")


# ── plotly spec cleaning ─────────────────────────────────────────────────────
def test_plotly_clean_strips_markup_everywhere_and_normalises_titles():
    d = _ok("""_plotlyClean({data:[{type:'bar',x:['<a href="x">a</a>'],name:'<img src=x onerror=alert(1)>n',colorbar:{title:'cb'},marker:{colorbar:{title:'mc'}}}],
                            layout:{title:'<script>x</script>T',xaxis:{title:'X'},scene:{zaxis:{title:'Z'}},legend:{title:'L'}}})""")
    assert d["data"][0]["x"] == ["a"] and d["data"][0]["name"] == "n"
    assert d["data"][0]["colorbar"]["title"] == {"text": "cb"}
    assert d["data"][0]["marker"]["colorbar"]["title"] == {"text": "mc"}
    assert d["layout"]["title"] == {"text": "xT"}
    assert d["layout"]["xaxis"]["title"] == {"text": "X"}
    assert d["layout"]["scene"]["zaxis"]["title"] == {"text": "Z"}
    assert d["layout"]["legend"]["title"] == {"text": "L"}


def test_plotly_clean_keeps_plotly_markup_and_blocks_outbound_images():
    """Plotly's own <br>/<b>/<extra> subset is legitimate hover formatting; a
    layout image or an image trace with a URL is an outbound request from the
    reader's browser to a model-chosen host."""
    d = _ok("""_plotlyClean({data:[{type:'bar',x:[1],y:[1],hovertemplate:'%{x}<br><b>%{y}</b><extra></extra>'},
                                  {type:'image',source:'https://evil.example/p.png'},
                                  {type:'image',source:'data:image/png;base64,AAAA'}],
                            layout:{images:[{source:'https://evil.example/pixel.png'}],title:'T'}})""")
    assert d["data"][0]["hovertemplate"] == "%{x}<br><b>%{y}</b><extra></extra>"
    assert "source" not in d["data"][1] and d["data"][2]["source"].startswith("data:image/png")
    assert "images" not in d["layout"]


def test_plotly_clean_tolerates_a_missing_or_non_array_data():
    d = _ok("_plotlyClean({data:'nope'})")
    assert d["data"] == [] and d["layout"] == {}
