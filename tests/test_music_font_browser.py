# SPDX-License-Identifier: AGPL-3.0-or-later
"""Music notation in diagrams, in a real browser under the app's CSP.

No font macOS ships has the Musical Symbols block (U+1D100…), so a half note drew as two
empty boxes. The page now declares Noto Music for that range; the mermaid lane waits for
it before laying a diagram out, and falls back to the common symbols when it cannot load.
"""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_PROBE = pathlib.Path(__file__).with_name("music_font_probe.py")
HALF = "\U0001D157\U0001D165"


@pytest.fixture(scope="module")
def probe():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE)], capture_output=True, text=True, timeout=240)
    out = json.loads(r.stdout.strip().splitlines()[-1])
    if out.get("skip"):
        pytest.skip(out["skip"])
    assert out.get("ok"), out
    # mermaid itself comes from cdnjs: without the network there is nothing to measure
    if not out["online"].get("mermaid"):
        pytest.skip("mermaid could not be loaded (offline?)")
    return out


def test_with_the_font_the_real_note_symbols_are_drawn(probe):
    c = probe["online"]
    assert c["svg"] and not c["err"] and not c["csp_errors"], c
    assert c["faces"] == ["loaded"], c
    assert "VW Music" in c["family"], c
    assert HALF + " One long E♭" in c["tspans"], c["tspans"]


def test_a_png_export_carries_the_common_symbols(probe):
    """An SVG rasterised through <img> cannot use the page's web fonts, so the export
    swaps notation for the common symbols instead of drawing boxes."""
    c = probe["online"]
    assert c["exportedHasCommon"] is True and c["exportedHasHalf"] is False, c


def test_a_slow_font_falls_back_once_then_is_used(probe):
    """A first fetch that outlasts the 4 s wait falls back for that diagram only; the next
    diagram, drawn after the font arrived, uses it."""
    first, second = probe["font_slow"], probe["font_slow_second"]
    assert "○ One long E♭" in first["tspans"], first
    assert HALF + " One long E♭" in second["tspans"], second


def test_without_the_font_the_common_symbols_stand_in(probe):
    c = probe["font_blocked"]
    if c["faces"] == ["loaded"]:
        pytest.skip("Noto Music is installed on this machine, so blocking the CDN changes nothing")
    assert c["svg"] and not c["err"], c
    assert c["faces"] == ["error"], c
    assert "○ One long E♭" in c["tspans"], c["tspans"]
    assert "♪ eighth" in c["tspans"], c["tspans"]
    assert not any(HALF in t for t in c["tspans"]), c["tspans"]
    # the everyday symbols are left exactly as written
    assert "♩♩♩ Three short Gs" in c["tspans"], c["tspans"]
