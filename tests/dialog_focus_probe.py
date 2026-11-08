# SPDX-License-Identifier: AGPL-3.0-or-later
"""A dialog's deferred initial focus must not take focus away from a field the person
already moved into. Real browser, the canned API answers of ux_fixes_probe.py, no server.
Prints one JSON object.

    python dialog_focus_probe.py <index.html> [chromium|firefox|webkit]

Settings moves focus to its close button on a 40 ms timer after it opens. A click into a
field inside that window, followed by typing, used to send the keystrokes to the close
button: the field kept its old value and the edit never counted as unsaved. That is the
race behind the intermittent Firefox failure of
test_read_only_tabs_hide_save_unless_something_is_unsaved, where the probe typed within
~10-30 ms of opening Settings and Firefox's slower fill straddled the timer.
"""
import json
import pathlib
import sys

import ux_fixes_probe as P

OUT: dict = {}

STATE = """() => ({value: document.getElementById('s-max-img-dim').value,
  active: document.activeElement && (document.activeElement.id || document.activeElement.className),
  dirty: _settingsDirty(),
  footer: getComputedStyle(document.querySelector('.settings-footer')).display})"""


def _case(r, width: int) -> dict:
    out = {}
    pg = r.page(P._variant(speech="remote"), width=width, height=900)
    # 1. Nobody moves focus: the timer still lands it on the close control.
    pg.evaluate("openSettings('general')")
    pg.wait_for_timeout(150)
    out["untouched_active"] = pg.evaluate("document.activeElement.id || document.activeElement.className")
    pg.evaluate("closeSettings(true)")
    # 2. A real click into the field right after opening, typing only after the timer
    #    has run — the order in which the race lost the edit.
    pg.evaluate("openSettings('general')")
    pg.locator("#s-max-img-dim").click()
    pg.wait_for_timeout(150)
    pg.keyboard.press("End")
    pg.keyboard.type("7")
    out["clicked"] = pg.evaluate(STATE)
    pg.evaluate("switchTab('status')")
    out["clicked"]["status_footer"] = pg.evaluate(STATE)["footer"]
    pg.evaluate("closeSettings(true)")
    # 3. Same with programmatic focus (what Playwright's fill does first).
    pg.evaluate("openSettings('analytics')")
    pg.evaluate("switchTab('general')")
    pg.focus("#s-max-img-dim")
    pg.wait_for_timeout(150)
    pg.keyboard.press("End")
    pg.keyboard.type("5")
    out["focused"] = pg.evaluate(STATE)
    pg.evaluate("closeSettings(true)")
    pg.context.close()
    return out


def main(index: pathlib.Path, engine: str) -> None:
    P.INDEX = index
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        OUT["skip"] = "playwright not installed"
        return
    with sync_playwright() as p:
        try:
            r = P.Run(p, engine, None)
        except Exception as exc:                  # noqa: BLE001 — engine not installed
            OUT["skip"] = f"{engine} not available: {exc}"[:300]
            return
        for width in (1400, 375):
            OUT[str(width)] = _case(r, width)
        OUT["errors"] = r.errors[:8]
        OUT["ok"] = True
        r.browser.close()


if __name__ == "__main__":
    try:
        main(pathlib.Path(sys.argv[1]).resolve(), sys.argv[2] if len(sys.argv) > 2 else "chromium")
    except Exception as exc:                      # noqa: BLE001 — reported as JSON
        OUT["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(OUT, ensure_ascii=False, default=str))
