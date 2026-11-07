# SPDX-License-Identifier: AGPL-3.0-or-later
"""The common-symbol fallback for music notation, run as real JavaScript."""
import json
import pathlib
import re
import shutil

import pytest

from _jsrun import run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _fallback(text: str) -> str:
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    blk = html[html.index("const _MUSIC_CH="):html.index("let _musicFontLoad")]
    r = run_node(blk + "\nconsole.log(JSON.stringify(_musicCommon(" + json.dumps(text) + ")));")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("src,want", [
    ("\U0001D157\U0001D165 half", "○ half"),              # notehead + stem
    ("\U0001D15E half", "○ half"),                        # precomposed half note
    ("\U0001D15D whole", "○ whole"),
    ("\U0001D158\U0001D165 quarter", "♩ quarter"),
    ("\U0001D15F quarter", "♩ quarter"),
    ("\U0001D158\U0001D165\U0001D16E eighth", "♪ eighth"),  # must not stop at the quarter
    ("\U0001D160 eighth", "♪ eighth"),
    ("\U0001D161 sixteenth", "♬ sixteenth"),
    ("\U0001D12A x \U0001D12B", "♯♯ x ♭♭"),
    ("\U0001D158\U0001D165\U0001D170 32nd", "♬ 32nd"),     # notehead+stem+3 flags: not ♩ + a box
    ("\U0001D158\U0001D165\U0001D171 64th", "♬ 64th"),
    ("\U0001D162 \U0001D163 \U0001D164", "♬ ♬ ♬"),       # precomposed 32nd/64th/128th
])
def test_notes_map_to_the_common_symbols(src, want):
    assert _fallback(src) == want


def test_text_without_music_notation_is_untouched():
    s = "♩♪♫♬ E♭ F♯ G♮ — plain text (x)"
    assert _fallback(s) == s


def _detects(text: str) -> bool:
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    blk = html[html.index("const _MUSIC_CH="):html.index("let _musicFontLoad")]
    r = run_node(blk + "\nconsole.log(JSON.stringify(_MUSIC_CH.test(" + json.dumps(text) + ")));")
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("ch,want", [
    ("\U0001D000", True),    # Byzantine musical symbols
    ("\U0001D11E", True),    # G clef
    ("\U0001D15E", True),    # half note
    ("\U0001D245", True),    # end of Ancient Greek musical notation — the font covers it too
    ("\u2669", False),       # ♩ — every font has it; no reason to wait for a webfont
    ("\u266D", False),       # ♭
])
def test_the_detector_matches_the_font_range(ch, want):
    """The diagram waits for the font exactly when the font would draw something."""
    assert _detects("x " + ch) is want


def test_the_detector_range_matches_the_font_face():
    html = _INDEX.read_text(encoding="utf-8")
    face = re.search(r"font-family:'VW Music'.*?unicode-range:([^}]+)\}", html, re.S).group(1)
    assert face.replace(" ", "") == "U+1D000-1D0F5,U+1D100-1D245"
