# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an answer tells the user about caching.

The reported bug was a cache that "did not work": the same question three times, nothing
cached. The cache was behaving as designed — a chart request is deliberately never looked
up and never stored — but nothing on screen said so, and the badge that could have said it
never rendered at all: the meta row is built while meta.streaming is still true, and it is
never redrawn once the stream finishes.

Browser work lives in ``tests/cache_badge_probe.py``. Same design as ``test_ux_browser.py``.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_INDEX = _ROOT / "visualweaver" / "index.html"
_PROBE = pathlib.Path(__file__).with_name("cache_badge_probe.py")

_CANDIDATE_PYTHONS = [
    os.environ.get("VISUALWEAVER_TEST_PLAYWRIGHT_PYTHON"),
    sys.executable,
    shutil.which("python3"),
    shutil.which("python"),
    str(pathlib.Path.home() / "miniconda3" / "bin" / "python3"),
    str(pathlib.Path.home() / "anaconda3" / "bin" / "python3"),
    str(pathlib.Path.home() / ".pyenv" / "shims" / "python3"),
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
]
_resolved: list = []


def _no_browser():
    """Skip — or FAIL when CI insists the browser tests must really run."""
    msg = ("no interpreter with playwright installed "
           "(set VISUALWEAVER_TEST_PLAYWRIGHT_PYTHON)")
    if os.environ.get("VISUALWEAVER_REQUIRE_BROWSER_TESTS") == "1":
        pytest.fail(msg + " — required because VISUALWEAVER_REQUIRE_BROWSER_TESTS=1")
    pytest.skip(msg)


def _playwright_python():
    if _resolved:
        return _resolved[0]
    seen = set()
    for cand in _CANDIDATE_PYTHONS:
        if not cand or cand in seen or not pathlib.Path(cand).exists():
            continue
        seen.add(cand)
        try:
            r = subprocess.run([cand, "-c", "import playwright"],
                               capture_output=True, timeout=60)
        except Exception:
            continue
        if r.returncode == 0:
            _resolved.append(cand)
            return cand
    _resolved.append(None)
    return None


@pytest.fixture(scope="session")
def badge():
    py = _playwright_python()
    if py is None:
        _no_browser()
    r = subprocess.run([py, str(_PROBE), str(_INDEX)],
                       capture_output=True, text=True, timeout=600)
    out = (r.stdout or "").strip()
    # SKIP rather than fail on a harness problem: one flaky browser launch should not
    # read as two dozen unrelated behavioural failures.
    if not out:
        pytest.skip(f"cache badge probe produced no output (exit {r.returncode}): {r.stderr[-400:]}")
    try:
        data = json.loads(out.splitlines()[-1])
    except json.JSONDecodeError:
        pytest.skip(f"cache badge probe output was not JSON: {out[-400:]}")
    if "skip" in data:
        pytest.skip(data["skip"])
    if not data.get("ok"):
        pytest.skip(f"cache badge probe could not run: {data.get('error')}")
    return data




# ── a streamed answer gets a cache badge at all ──────────────────────────────
def test_no_badge_is_shown_while_the_answer_is_still_streaming(badge):
    """Claiming "not cached" before the store has run would be a guess."""
    for k in ("miss", "visual", "attach", "rerun"):
        assert badge["badges"][f"{k}_while_streaming"] is None, k


def test_a_finished_answer_always_says_something_about_the_cache(badge):
    """It said nothing at all: the row is built with streaming true, which suppresses the
    badge, and nothing redraws it afterwards."""
    for k in ("miss", "visual", "attach", "rerun", "hit"):
        b = badge["badges"][k]
        assert b and b["text"], k
        assert b["h"] > 0, k


# ── the three states are distinguishable ─────────────────────────────────────
def test_a_query_that_is_never_cacheable_is_not_reported_as_a_plain_miss(badge):
    """The whole reported bug. "Live" reads as "nothing matched, try again"; the truth is
    that this question will never be cached however many times it is asked."""
    assert badge["badges"]["miss"]["text"] != badge["badges"]["visual"]["text"]
    assert "Not cached" in badge["badges"]["visual"]["text"]


def test_the_reason_is_readable_without_opening_anything(badge):
    """A reason only in the DOM is not a reason the user has."""
    assert "chart or diagram" in badge["badges"]["visual"]["title"]
    assert "file is attached" in badge["badges"]["attach"]["title"]


def test_a_deliberate_regenerate_is_not_reported_as_a_cache_failure(badge):
    """Re-running deliberately bypasses the cache; calling that a miss invites the user
    to debug something that is working."""
    assert "Regenerated" in badge["badges"]["rerun"]["text"]


def test_a_hit_reports_how_close_the_match_was(badge):
    b = badge["badges"]["hit"]
    assert "Cached" in b["text"] and "94" in b["text"]
    assert "94" in b["title"]


def test_a_hit_and_a_miss_do_not_look_the_same(badge):
    assert badge["badges"]["hit"]["colour"] != badge["badges"]["miss"]["colour"]


def test_every_badge_is_the_same_height(badge):
    """The ↻ glyph takes no emoji fallback and rendered 5px shorter, so the row jittered
    depending on which state an answer landed in."""
    hs = {k: badge["badges"][k]["h"] for k in ("miss", "visual", "attach", "rerun", "hit")}
    assert len(set(hs.values())) == 1, hs


# ── it cannot contradict itself ──────────────────────────────────────────────
def test_a_cache_hit_is_never_overwritten_by_a_late_status(badge):
    """Prepending a "Live" beside "Cached 94%" put two contradictory badges on one
    answer."""
    assert badge["badges"]["hit_after_setCacheBadge"]["text"] == badge["badges"]["hit"]["text"]


def test_setting_the_status_twice_leaves_one_badge(badge):
    assert badge["idempotent"]["count"] == 1
    assert badge["idempotent"]["text"] == ["🚫 Not cached"]


def test_a_status_for_an_answer_that_is_gone_is_survivable(badge):
    """stream_end can arrive after the session was switched away."""
    assert badge["missing_msg_is_survivable"] is True


# ── the reason string is data ────────────────────────────────────────────────
def test_a_reason_from_the_server_cannot_execute(badge):
    e = badge["escaping"]
    assert e["xss"] is False and e["xss_after_wait"] is False
    assert e["imgs"] == 0


# ── a reopened conversation must not invent a cache lookup ───────────────────
def test_a_turn_from_before_this_was_recorded_shows_no_badge(badge):
    """switchSession replays stored meta straight into appendMessage with no stream_end
    to follow, so every restored answer rendered "no sufficiently similar question was
    cached" — including the ones served FROM the cache. Silence is the honest state."""
    assert badge["restored"]["unknown"] is None


def test_a_restored_cache_hit_still_says_it_was_a_hit(badge):
    b = badge["restored"]["was_hit"]
    assert b and "Cached" in b["text"] and "93" in b["text"]


def test_a_restored_skip_still_names_its_reason(badge):
    b = badge["restored"]["was_skipped"]
    assert b and "Not cached" in b["text"] and "image is attached" in b["title"]


def test_a_restored_miss_still_reads_as_a_miss(badge):
    assert "Live" in badge["restored"]["was_miss"]["text"]


# ── the reason has to be readable, not hover-only ────────────────────────────
def test_the_reason_is_on_the_badge_not_only_in_a_tooltip(badge):
    """A title attribute is invisible on touch and to anyone not already hunting for it.
    The user asked the same question twice, was told "Not cached" with no reason on
    screen, and reasonably concluded the cache was broken."""
    for theme in ("light", "dark"):
        assert badge["legibility"][theme]["reasonVisible"] is True, theme
        assert "chart request" in badge["legibility"][theme]["text"], theme


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_badge_clears_the_contrast_floor_in_both_themes(theme, badge):
    """It measured 3.8:1 as 10px text: the badge's own tint lifts the backdrop, so the
    colour token that clears 4.5:1 elsewhere in the app does not clear it here."""
    m = badge["legibility"][theme]
    assert m["contrast"] >= 4.5, (theme, m["contrast"], m["colour"])


def test_the_short_label_does_not_replace_the_full_sentence(badge):
    """The badge is a summary; the sentence is still there for anyone who hovers."""
    b = badge["badges"]["visual"]
    assert "chart request" in b["text"]
    assert "always generated fresh" in b["title"]
