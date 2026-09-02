# SPDX-License-Identifier: AGPL-3.0-or-later
"""What loadConfig does when GET /api/config does not return a config.

An error body is valid JSON — FastAPI answers 500 with {"detail": …} — so the
original `S.config = await r.json()` accepted it. That mattered because
`_collectSettings` reads `S.config` with `||` fallbacks: from an unusable config
a Save posts `web_sources: []`, `watch_folders: []` and empty `api_key` strings,
and an empty string is not the "unchanged" sentinel, so the server does not
restore them. One failed GET followed by one Save deleted every web source,
watch folder and stored API key.

Rejecting the body is not enough on its own — `{}` and `{detail: …}` behave
identically downstream — so `S.configLoaded` records whether the config is known
at all, and saveSettings refuses while it is false. Both halves are asserted
here.

Runs under node with fetch and DOM stubs.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"

_STUBS = """
const S = {config:null, configLoaded:false, activeRag:null, appInfo:null};
const document = {getElementById: () => null};
const console = {warn(){}, log:globalThis.console.log};
function _fillWelcomeVersion(){}
let RESPONSE = null;
async function fetch(url){
  if (String(url).indexOf('/api/health') === 0 || String(url).indexOf('/api/health') > 0)
    return {ok:true, status:200, json: async () => ({})};
  return RESPONSE;
}
"""


def _load(response_js):
    if not shutil.which("node"):
        pytest.skip("node not available")
    src = _INDEX.read_text(encoding="utf-8")
    js = (_STUBS
          + extract_js_function(src, "loadConfig") + "\n"
          + f"RESPONSE = {response_js};\n"
          + "loadConfig().then(() => globalThis.console.log(JSON.stringify(\n"
            "  {loaded:S.configLoaded, keys:Object.keys(S.config||{}).sort()})));\n")
    r = run_node(js, timeout=60)
    assert r.returncode == 0, f"node exited {r.returncode}:\n{r.stderr[:1200]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_a_good_config_is_accepted_and_marked_loaded():
    r = _load("{ok:true, status:200, json: async () => ({active_rag:'default', web_sources:[1]}) }")
    assert r["loaded"] is True
    assert "web_sources" in r["keys"]


def test_a_500_body_is_not_accepted_as_the_config():
    """{"detail": "Internal Server Error"} parses perfectly well."""
    r = _load("{ok:false, status:500, json: async () => ({detail:'Internal Server Error'}) }")
    assert r["loaded"] is False, "a 500 must not leave the config marked loaded"
    assert r["keys"] == [], f"the error body became the config: {r['keys']}"


def test_a_non_object_body_is_rejected():
    for body in ("[]", "'a string'", "null", "42"):
        r = _load(f"{{ok:true, status:200, json: async () => ({body}) }}")
        assert r["loaded"] is False, f"{body} was accepted as a config"


def test_a_network_failure_leaves_the_config_unknown_not_empty():
    """An empty config is indistinguishable from a real one with nothing
    configured, which is exactly what made the following Save destructive."""
    r = _load("{ok:true, status:200, json: async () => { throw new Error('boom'); } }")
    assert r["loaded"] is False and r["keys"] == []


def test_save_settings_refuses_while_the_config_is_unknown():
    """The half that actually prevents the loss. Rejecting the body changes
    nothing on its own, because {} behaves like {detail:…} downstream."""
    src = _INDEX.read_text(encoding="utf-8")
    fn = extract_js_function(src, "saveSettings")
    # Behaviour, not spelling: the earlier version pinned the exact string
    # "S.configLoaded===false", so tightening the guard to the safer
    # "!S.configLoaded" would have failed it.
    body = fn[:fn.index("_postConfig")]
    code = "\n".join(l for l in body.split("\n") if not l.strip().startswith("//"))
    assert "configLoaded" in code, \
        "saveSettings does not check whether the config was ever read"
    assert "return" in code, "the guard does not return before posting"
