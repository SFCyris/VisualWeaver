# SPDX-License-Identifier: AGPL-3.0-or-later
"""The verdicts behind the first-run checklist and the chat status strip (_healthModel),
run as real JavaScript under node.

Before 1.16.0 nothing in the chat view said whether the storage, the model provider or the
server connection was working, and the only "is there a model?" test was
``!!S.currentModel`` — true for a hosted provider with no API key at all, because Claude
and Qwen list their models without one.
"""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"

_PRELUDE = """
let _wsQueued=null;
const PROVIDER_META={ollama:{label:'Ollama'},claude:{label:'Claude'},openai:{label:'OpenAI'}};
function _appConn(){return S.storage?.app||'default';}
function _connLabel(n){return n==='default'?'Built-in Redis':n;}
function _isActiveRag(r){return S.activeRag&&r.name===S.activeRag.name&&(r.redis_endpoint||'default')===(S.activeRag.ep||'default');}
let S;
function base(o){return Object.assign({provider:'ollama',modelsLoaded:true,modelsLoading:false,currentModel:'llama3:latest',
  ollamaModels:[{name:'llama3:latest'}],providerStatus:{ollama:{ok:true}},defaultStatus:{ok:true},epOk:{},
  ragInstances:[{name:'default',chunks:0,redis_endpoint:'default',reachable:true}],wsState:'open'},o||{});}
function H(o){S=base(o);return _healthModel();}
"""


def _run(body: str):
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in ("_frTarget", "_healthModel"))
    r = run_node(f"{_PRELUDE}\n{src}\n{body}")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_a_hosted_provider_without_a_key_is_not_a_usable_model():
    """Old: Claude with no key counted as ready — its static model list selects a model."""
    out = _run("""console.log(JSON.stringify([
      H({provider:'claude',currentModel:'claude-sonnet-4-6',providerStatus:{claude:{ok:false,configured:false}}}).model,
      H({provider:'claude',currentModel:'claude-sonnet-4-6',providerStatus:{claude:{ok:false,error:'invalid x-api-key'}}}).model,
      H({provider:'claude',currentModel:'claude-sonnet-4-6',providerStatus:{claude:{ok:true}}}).model,
      H({provider:'claude',currentModel:'claude-sonnet-4-6',providerStatus:{}}).model]));""")
    nokey, rejected, good, unknown = out
    assert nokey["ok"] is False and nokey["text"] == "No Claude API key"
    assert rejected["ok"] is False and rejected["fix"] == "invalid x-api-key"
    assert good["ok"] is True and good["text"] == "Claude · claude-sonnet-4-6"
    assert unknown["ok"] is None                       # not probed yet: not claimed either way


def test_an_ollama_that_stopped_answering_is_not_ready_even_with_a_model_selected():
    out = _run("""console.log(JSON.stringify([
      H({providerStatus:{ollama:{ok:false,error:'refused'}}}).model,
      H({}).model,
      H({currentModel:'',ollamaModels:[{name:'nomic-embed-text'}]}).model,
      H({currentModel:'',ollamaModels:[]}).model,
      H({modelsLoaded:false}).model]));""")
    down, ok, embed_only, none, loading = out
    assert down["ok"] is False and down["text"] == "Ollama is not reachable"
    assert ok["ok"] is True and ok["text"] == "Ollama · llama3:latest"
    assert embed_only["ok"] is False and "only embedding" in embed_only["text"]
    assert none["ok"] is False and "ollama pull" in none["fix"]
    assert loading["ok"] is None


def test_storage_is_the_connection_that_holds_the_app_data():
    out = _run("""console.log(JSON.stringify([
      H({}).storage, H({defaultStatus:{ok:false}}).storage, H({defaultStatus:undefined}).storage,
      H({storage:{app:'team'},epOk:{team:false}}).storage,
      H({storage:{app:'team'},epOk:{team:true},defaultStatus:{ok:false}}).storage]));""")
    assert out[0] == {"ok": True, "label": "Built-in Redis"}
    assert out[1]["ok"] is False and out[2]["ok"] is None
    assert out[3] == {"ok": False, "label": "team"}
    assert out[4]["ok"] is True                        # the built-in being down is not the app's store


def test_knowledge_is_done_when_an_instance_has_chunks_and_targets_the_searched_one():
    out = _run("""console.log(JSON.stringify([
      H({ragInstances:[{name:'default',chunks:0},{name:'handbook',chunks:12}]}).knowledge,
      H({ragInstances:[{name:'default',chunks:0,redis_endpoint:'default'},{name:'handbook',chunks:0,redis_endpoint:'default'}],
         activeRag:{name:'handbook',ep:'default'}}).knowledge,
      H({ragInstances:[{name:'default',chunks:0,redis_endpoint:'default'},{name:'handbook',chunks:0,redis_endpoint:'default',reachable:false}],
         activeRag:{name:'handbook',ep:'default'}}).knowledge,
      H({ragInstances:[{name:'default',chunks:0,redis_endpoint:'default'},{name:'handbook',chunks:0,redis_endpoint:'default'}],
         activeRag:{name:'handbook',ep:'default'},ragAllMode:true}).knowledge,
      H({ragInstances:[{name:'gone',chunks:9,reachable:false}]}).knowledge,
      H({defaultStatus:{ok:false}}).knowledge,
      H({ragInstances:undefined}).knowledge]));""")
    done, active, active_down, all_mode, unreachable_full, blocked, unknown = out
    assert done["ok"] is True and done["text"] == "“handbook” has 12 chunks"
    assert active == {"ok": False, "text": "Add your first documents", "target": "handbook"}
    assert active_down["target"] == "default"          # not into an instance that cannot be reached
    assert all_mode["target"] == "default"
    assert unreachable_full["ok"] is False             # chunks nobody can search do not count
    assert blocked["ok"] is None and blocked["blocked"] is True
    assert unknown["ok"] is None


def test_the_socket_state_and_a_waiting_message_reach_the_strip():
    out = _run("""console.log(JSON.stringify([
      H({wsState:'reconnecting'}).socket, (_wsQueued={}, H({wsState:'reconnecting'}).socket),
      (_wsQueued=null, H({wsState:undefined}).socket),
      H({wsState:'offline'}), H({wsState:'reconnecting'}).storage]));""")
    rec, queued, initial, offline, rec_storage = out
    assert rec == {"state": "reconnecting", "queued": False, "net": True}
    assert queued["queued"] is True
    assert initial["state"] == "connecting"
    # The app server is away: what it last said about storage and the model is not current.
    assert offline["storage"]["ok"] is None and offline["model"]["ok"] is None
    assert rec_storage["ok"] is True                   # a 2-s reconnect does not blank them
