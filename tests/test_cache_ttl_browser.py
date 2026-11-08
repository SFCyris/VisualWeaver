# SPDX-License-Identifier: AGPL-3.0-or-later
"""The answer-cache table reads TTL -1 as "never" (cache TTL 0), not "expired"."""
import json
import pathlib
import subprocess

import pytest

from test_science_lanes_browser import _no_browser, _playwright_python

_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def probe():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(pathlib.Path(__file__).with_name("cache_ttl_probe.py")),
                        str(_ROOT / "visualweaver" / "index.html")], capture_output=True, text=True, timeout=240)
    if "No module named 'playwright'" in r.stderr:
        pytest.skip("playwright not installed")
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_cache_ttl_codes_and_durations_read_as_words(probe):
    assert dict(probe["rows"]) == {"never q": "never", "gone q": "expired", "short": "45s",
                                   "mins": "59 min", "hours": "24 h", "days": "7 d"}
    assert probe["expired_color"] == "rgb(220, 38, 38)"
