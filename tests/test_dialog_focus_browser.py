# SPDX-License-Identifier: AGPL-3.0-or-later
"""A dialog's deferred initial focus never takes focus away from a field the person moved
into (dialog_focus_probe.py, Chromium / Firefox / WebKit, at 1400 px and 375 px)."""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_PROBE = pathlib.Path(__file__).with_name("dialog_focus_probe.py")
_RUNS: dict = {}


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def focus(request):
    engine = request.param
    if engine not in _RUNS:
        py = _playwright_python()
        if not py:
            _no_browser()
        r = subprocess.run([py, str(_PROBE), str(_ROOT / "visualweaver" / "index.html"), engine],
                           capture_output=True, text=True, timeout=600, cwd=str(_PROBE.parent))
        lines = (r.stdout or "").strip().splitlines()
        _RUNS[engine] = json.loads(lines[-1]) if lines else {"error": r.stderr[-600:]}
    out = _RUNS[engine]
    if out.get("skip"):
        if engine == "chromium":
            _no_browser()
        pytest.skip(out["skip"])
    assert out.get("ok"), out.get("error")
    assert out["errors"] == []
    return out


@pytest.mark.parametrize("width", ["1400", "375"])
def test_typing_into_a_field_clicked_right_after_opening_settings_reaches_the_field(focus, width):
    """Old behaviour: Settings moved focus to its close button 40 ms after opening, even
    when focus was already in a field — so the typing that followed went to the button,
    the field kept 1024, nothing counted as unsaved and a read-only tab hid Save.

    The `focused` case (focus inside the 40 ms window, typing after it) is the one that
    discriminates: it is what Playwright's fill does, and it failed in all three engines
    on the old code. The `clicked` case drives real pointer input; Playwright's click
    usually lands after the timer, so it guards that the fix keeps clicking working."""
    c = focus[width]
    assert c["clicked"]["active"] == "s-max-img-dim", c
    assert c["clicked"]["value"] == "10247", c
    assert c["clicked"]["dirty"] is True and c["clicked"]["status_footer"] == "flex", c
    assert c["focused"]["active"] == "s-max-img-dim", c
    assert c["focused"]["value"] == "10245" and c["focused"]["dirty"] is True, c


@pytest.mark.parametrize("width", ["1400", "375"])
def test_settings_still_focuses_its_close_button_when_nothing_else_has_focus(focus, width):
    """The guard must not drop the initial focus itself (WCAG 2.4.3): with no click, focus
    still lands on the close control, the reversible action."""
    assert focus[width]["untouched_active"] == "settings-close", focus[width]
