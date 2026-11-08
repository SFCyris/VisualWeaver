# SPDX-License-Identifier: AGPL-3.0-or-later
"""install.sh's validation also checks the model provider chosen in config.json.

Before 1.16.0 an install with every store up reported "ready to use" while the selected
provider could not answer anything — Ollama not running, Ollama with only an embedding
model, or a hosted provider without an API key — and the first sign was an empty model
picker. The check reports ✓ or ⚠ with the fix; it never fails the install, because
Ollama can be started and a key added afterwards.

These run the REAL validation block of install.sh in the harness of
test_install_storage_prompt.py (fake stores, fake venv), against a throwaway HTTP server
standing in for Ollama. Nothing reaches a real Ollama or a real data directory.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_install_storage_prompt import _validate


class _Tags(BaseHTTPRequestHandler):
    models: list = []

    def do_GET(self):  # noqa: N802 — http.server API
        body = json.dumps({"models": self.models}).encode()
        self.send_response(200 if self.path == "/api/tags" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Tags)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _run(tmp_path, cfg, extra=None):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if cfg is not None:
        (data / "config.json").write_text(json.dumps(cfg))
    return _validate(tmp_path, storage="redis", extra=extra)


def _line(out: str) -> str:
    return next((ln for ln in out.splitlines() if "Model provider" in ln), "")


def test_ollama_with_a_chat_model_passes(tmp_path, ollama):
    _Tags.models = [{"name": "nomic-embed-text:latest", "details": {"family": "nomic-bert"}},
                    {"name": "llama3:latest", "details": {"family": "llama"}}]
    rc, out = _run(tmp_path, {"provider": "ollama", "ollama": {"host": "http://127.0.0.1", "port": ollama}})
    assert rc == 0, out
    assert _line(out).strip() == f"✓ Model provider     Ollama at http://127.0.0.1:{ollama}: 1 chat model (llama3:latest)"
    assert "Before the first answer" not in out


def test_ollama_that_is_not_running_warns_with_the_fix_but_the_install_succeeds(tmp_path):
    port = _closed_port()
    rc, out = _run(tmp_path, {"provider": "ollama", "ollama": {"host": "http://127.0.0.1", "port": port}})
    assert rc == 0, out                                   # a warning, never a failed install
    assert _line(out).strip() == f"⚠ Model provider     Ollama is not reachable at http://127.0.0.1:{port}"
    assert "Before the first answer" in out and "start Ollama" in out
    assert "✗" not in out


def test_ollama_with_only_embedding_models_warns(tmp_path, ollama):
    _Tags.models = [{"name": "mxbai-embed-large:latest", "details": {"family": "bert"}},
                    {"name": "bge-m3:latest", "details": {"families": ["bert"]}}]
    rc, out = _run(tmp_path, {"provider": "ollama", "ollama": {"host": "http://127.0.0.1", "port": ollama}})
    assert rc == 0, out
    assert "⚠ Model provider" in _line(out) and "only embedding models (2)" in _line(out)
    assert "ollama pull llama3" in out


def test_ollama_with_no_models_warns(tmp_path, ollama):
    _Tags.models = []
    rc, out = _run(tmp_path, {"provider": "ollama", "ollama": {"host": "http://127.0.0.1", "port": ollama}})
    assert "⚠ Model provider" in _line(out) and "has no models" in _line(out)
    assert rc == 0


def test_the_ollama_url_variable_applies_only_to_the_default_address(tmp_path, ollama):
    _Tags.models = [{"name": "llama3:latest"}]
    rc, out = _run(tmp_path, {"provider": "ollama", "ollama": {"host": "http://localhost", "port": 11434}},
                   extra={"VISUALWEAVER_OLLAMA_URL": f"http://127.0.0.1:{ollama}"})
    assert f"✓ Model provider     Ollama at http://127.0.0.1:{ollama}" in out, out


def test_a_hosted_provider_with_a_key_in_the_config_passes_without_calling_it(tmp_path):
    rc, out = _run(tmp_path, {"provider": "claude", "claude": {"api_key": "sk-ant-test-not-real"}})
    assert rc == 0, out
    assert _line(out).strip() == "✓ Model provider     Claude: API key set in the app config"
    assert "sk-ant" not in out                            # the key itself is never printed


def test_a_hosted_provider_key_from_the_environment_passes(tmp_path):
    rc, out = _run(tmp_path, {"provider": "groq", "groq": {"api_key": ""}}, extra={"GROQ_API_KEY": "gsk_x"})
    assert _line(out).strip() == "✓ Model provider     Groq: API key from GROQ_API_KEY"
    assert "gsk_x" not in out


def test_a_hosted_provider_without_a_key_warns_with_where_to_add_it(tmp_path):
    rc, out = _run(tmp_path, {"provider": "openai", "openai": {"api_key": ""}})
    assert rc == 0, out
    assert _line(out).strip() == "⚠ Model provider     OpenAI is selected but has no API key"
    assert "Settings → Providers" in out and "OPENAI_API_KEY" in out


def test_a_failed_store_still_fails_the_install_and_the_provider_is_still_reported(tmp_path):
    rc, out = _run(tmp_path, {"provider": "openai", "openai": {"api_key": ""}}, extra={"FAKE_REDIS": "nosearch"})
    assert rc != 0
    assert "✗ Redis" in out and "⚠ Model provider" in out
