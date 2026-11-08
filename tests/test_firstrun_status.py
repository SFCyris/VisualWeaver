# SPDX-License-Identifier: AGPL-3.0-or-later
"""The provider half of the first-run checklist and the chat status strip.

/api/status/ollama answered ``{"ok": true}`` whatever Ollama was doing: it awaited
``providers.ollama_models()``, which swallows every connection error and returns ``[]``, so
the probe could not fail. Settings → Status showed "● Online" for an Ollama that was not
running, the model picker's dot stayed green, and nothing in the chat could say that the
provider had gone away.
"""
import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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
def fake_ollama():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Tags)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture
def ollama_at(cfg):
    """Point the app's Ollama setting at a port, with the model cache emptied."""
    from visualweaver import state

    def point(port: int):
        cfg["ollama"] = {"host": "http://127.0.0.1", "port": port, "model": ""}
        state._ollama_models_cache = []
        state._ollama_models_ts = 0.0
    saved = (list(state._ollama_models_cache), state._ollama_models_ts)
    yield point
    state._ollama_models_cache, state._ollama_models_ts = saved


def _status():
    from visualweaver import routes_settings
    return asyncio.run(routes_settings.api_ollama_status())


def test_ollama_status_reports_an_ollama_that_is_not_running(ollama_at):
    """Old: {"ok": true} with nothing listening on the configured port."""
    ollama_at(_closed_port())
    out = _status()
    assert out["ok"] is False, out
    assert "127.0.0.1" in out.get("error", ""), out


def test_ollama_status_is_ok_and_counts_the_models_when_ollama_answers(ollama_at, fake_ollama):
    _Tags.models = [{"name": "llama3:latest", "details": {"family": "llama"}},
                    {"name": "nomic-embed-text:latest", "details": {"family": "nomic-bert"}}]
    ollama_at(fake_ollama.server_address[1])
    out = _status()
    assert out["ok"] is True, out
    assert out.get("models") == 2


def test_an_ollama_that_answers_with_no_models_is_reachable_not_down(ollama_at, fake_ollama):
    """Reachable but empty is a different fix (pull a model) from not running (start it)."""
    _Tags.models = []
    ollama_at(fake_ollama.server_address[1])
    out = _status()
    assert out["ok"] is True and out.get("models") == 0, out
