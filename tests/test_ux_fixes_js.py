# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure helpers behind the usability fixes, run as real JavaScript under node: the colour
sanitiser, the embedding-model filter, the "empty knowledge base" test and the server's own
reason on a connection test without vector search."""
import json
import pathlib
import shutil

import pytest

from _jsrun import extract_js_function, run_node

_INDEX = pathlib.Path(__file__).resolve().parents[1] / "visualweaver" / "index.html"


def _run(body: str, fns: tuple, prelude: str = "") -> object:
    if not shutil.which("node"):
        pytest.skip("node not available")
    html = _INDEX.read_text(encoding="utf-8")
    src = "\n".join(extract_js_function(html, n) for n in fns)
    r = run_node(f"{prelude}\n{src}\n{body}")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_only_hex_colours_reach_a_style_attribute():
    out = _run("""console.log(JSON.stringify([
      _safeColor('#7C6FF7'), _safeColor('#abc'), _safeColor('#abcd'), _safeColor('#11223344'),
      _safeColor('#f00"><img src=x onerror=alert(1)>'), _safeColor('red'), _safeColor('url(x)'),
      _safeColor(null), _safeColor('#12345'), _safeColor(' #00ff00 ')]));""", ("_safeColor",))
    assert out == ["#7c6ff7", "#aabbcc", "#aabbcc", "#112233",
                   "#6366f1", "#6366f1", "#6366f1", "#6366f1", "#6366f1", "#00ff00"]


def test_embedding_models_are_recognised_by_name_or_bert_family():
    out = _run("""console.log(JSON.stringify([
      {name:'nomic-embed-text:latest', details:{family:'nomic-bert'}},
      {name:'mxbai-embed-large'}, {name:'bge-m3:latest', details:{family:'bert', families:['bert']}},
      {name:'all-minilm', details:{families:['bert']}},
      {name:'llama3:latest', details:{family:'llama'}}, {name:'gemma3:4b', details:{families:['gemma3','clip']}},
      {name:'qwen2.5:7b'}].map(_isEmbeddingModel)));""", ("_isEmbeddingModel",))
    assert out == [True, True, True, True, False, False, False]


def test_chat_models_drop_embedding_models_only_for_ollama():
    out = _run("""console.log(JSON.stringify([
      _chatModels('ollama', [{name:'nomic-embed-text'}, {name:'llama3', size:1, vision:false}]),
      _chatModels('openai', [{id:'text-embedding-3-small'}, {id:'gpt-4o'}]).map(m => m.id),
      _chatModels('ollama', {error:'down'})]));""",
               ("_isEmbeddingModel", "_chatModels", "_normalizeModels"))
    assert out[0] == [{"id": "llama3", "label": "llama3", "vision": False, "size": 1}]
    assert out[1] == ["text-embedding-3-small", "gpt-4o"]     # hosted lists are the provider's own
    assert out[2] == []


def test_a_knowledge_base_is_empty_only_when_every_searched_instance_is():
    out = _run("""console.log(JSON.stringify([
      _emptyRagTargets(['handbook']), _emptyRagTargets(['handbook','default']),
      _emptyRagTargets(['blank','handbook']), _emptyRagTargets(['gone']), _emptyRagTargets([]),
      _emptyRagTargets(undefined)]));""", ("_emptyRagTargets",),
               prelude="const S={ragInstances:[{name:'handbook',chunks:0},{name:'blank',chunks:0},{name:'default',chunks:4}]};")
    assert out == [["handbook"], None, ["blank", "handbook"], None, None, None]


def test_a_connection_test_shows_the_servers_own_reason_for_no_vector_search():
    note = "Redis search works only in database 0; this connection uses database 3."
    out = _run(f"""console.log(JSON.stringify([
      _connectionVerdict({{ok:true, version:'8.2', search_available:false, search_note:{json.dumps(note)}}}, false),
      _connectionVerdict({{ok:true, version:'8.2', search_available:false}}, false),
      _connectionVerdict({{ok:true, version:'8.0', search_available:false}}, true),
      _connectionVerdict({{ok:true, version:'8.2', search_available:false, search_note:'<b>x</b>'}}, false)]));""",
               ("escHtml", "_searchAdvice", "_connectionVerdict"))
    assert note in out[0] and "Load the Search module" not in out[0]
    assert "Load the Search module" in out[1]
    assert "Use Atlas" in out[2]
    assert "<b>" not in out[3] and "&lt;b&gt;" in out[3]       # a server text is text
