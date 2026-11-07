# SPDX-License-Identifier: AGPL-3.0-or-later
"""One event loop serves every user: anything that blocks it stalls every other session's
token stream and its Stop button. These tests pin the fixes for the places that did:

  * a crawl parsing pages (and resolving hosts, and rewriting the ingestion log) on the
    loop — and smart mode parsing every page twice;
  * two tabs on one conversation sharing per-session state, so closing one cancelled the
    other's answer and a message from the second was dropped without a word;
  * provider HTTP clients (Ollama, Gemini) built per call, on the loop;
  * sentence-transformers progress bars written to the log on every embed and rerank;
  * compose_system_prompt re-diffing a customised base instruction every turn;
  * uploads, config saves and the feedback store written on the loop (the feedback store
    non-atomically).

Event-loop lag is measured, not inferred: a ticker asks for a 5 ms sleep and records how
late it wakes, which is exactly what a streaming answer sees. Bounds are generous — the
point is to catch a 200 ms stall, not to benchmark.
"""
import asyncio
import http.server
import json
import random
import socketserver
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from conftest import KEY_PREFIX

try:
    from visualweaver import pagetext     # absent before the fix: these tests then fail on behaviour
except ImportError:                       # pragma: no cover
    pagetext = None


# ── helpers ──────────────────────────────────────────────────────────────────

class _LoopLag:
    """How late a 5 ms sleep wakes, sampled for as long as the block runs."""

    def __init__(self):
        self.lags: list[float] = []
        self._stop = False

    async def _tick(self):
        while not self._stop:
            t = time.perf_counter()
            await asyncio.sleep(0.005)
            self.lags.append((time.perf_counter() - t - 0.005) * 1000)

    async def __aenter__(self):
        self._task = asyncio.create_task(self._tick())
        return self

    async def __aexit__(self, *exc):
        self._stop = True
        await self._task

    def pct(self, p: float) -> float:
        xs = sorted(self.lags)
        return xs[min(len(xs) - 1, int(len(xs) * p / 100))] if xs else 0.0


_VOCAB = ("index vector search cache answer session stream token embed chunk model "
          "crawler page source document retrieval rerank prompt latency loop thread "
          "worker process parse extract html text link query instance redis mongo").split()


def _page(i: int, n_pages: int, paragraphs: int = 150) -> bytes:
    """~80 KB of article HTML linking to every other page — the reviewer's benchmark shape."""
    rnd = random.Random(i)
    links = "".join(f"<a href='/p{k}.html'>p{k}</a>" for k in range(n_pages))
    body = "".join(f"<h2>S{j}</h2><p>{' '.join(rnd.choice(_VOCAB) for _ in range(80))}</p>"
                   for j in range(paragraphs))
    return ("<html><head><title>P</title>" + "<script>var x=1;</script>" * 20
            + f"</head><body><nav>{links}</nav><main><article>{body}</article></main>"
              "</body></html>").encode()


class _Site:
    """A local threaded HTTP server serving the given pages."""

    def __init__(self, pages: dict[str, bytes]):
        pages_ = pages

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                b = pages_.get(self.path)
                if b is None:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class _FakeRedis:
    def smembers(self, *a, **k): return set()
    def delete(self, *a, **k): return 0
    def pipeline(self, **k): return self
    def sadd(self, *a, **k): return self
    def execute(self): return []


def _isolate_crawl(monkeypatch, record_threads: dict):
    """Everything but fetching and parsing is faked: no embedding, no storage."""
    from visualweaver import config, crawler, ingest, rag, rag_admin
    monkeypatch.setattr(rag_admin, "rc_for_instance", lambda *a, **k: _FakeRedis())
    monkeypatch.setattr(ingest, "_prepare_chunks",
                        lambda inst, text, src, rc, force: [{"text": text[:200], "source": src}])
    monkeypatch.setattr(rag, "add_chunks", lambda inst, records, rc: len(records))

    def guard(url):                       # the SSRF guard is a getaddrinfo: where does it run?
        record_threads.setdefault("dns", set()).add(threading.get_ident())

    real_logs = getattr(config, "append_logs", None)
    real_log = config.append_log

    def logged_many(entries):
        record_threads.setdefault("log", set()).add(threading.get_ident())
        real_logs(entries)

    def logged(entry):
        record_threads.setdefault("log", set()).add(threading.get_ident())
        real_log(entry)

    monkeypatch.setattr(crawler, "assert_public_url", guard)
    monkeypatch.setattr(config, "append_log", logged)
    if real_logs is not None:
        monkeypatch.setattr(config, "append_logs", logged_many)


# ── 1. crawling ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", ["pool", "thread"])
def test_a_crawl_does_not_stall_the_event_loop(app_module, monkeypatch, mode):
    """40 pages of ~80 KB at concurrency 10. Parsed on the loop this measured p95 ~200 ms
    and max ~0.5 s of lag; off it, a few ms. The thread fallback (a platform that cannot
    run worker processes) has to keep the loop responsive too."""
    from visualweaver import crawler
    if mode == "thread" and pagetext is not None:
        monkeypatch.setattr(pagetext, "_pool_disabled", "test: thread fallback")
    threads: dict = {}
    _isolate_crawl(monkeypatch, threads)
    n = 40
    site = _Site({f"/p{i}.html": _page(i, n) for i in range(n)})
    seen: list = []

    async def cb(u, status, n_=0, err="", count=0):
        seen.append((status, err))

    async def main():
        loop_thread = threading.get_ident()
        async with _LoopLag() as lag:
            await crawler.crawl_url(f"{KEY_PREFIX}lag", f"http://127.0.0.1:{site.port}/p0.html",
                                    depth=1, respect_robots=False, smart_mode=False,
                                    js_render=False, concurrency=10, force_reindex=True,
                                    rc=_FakeRedis(), progress_cb=cb)
        return loop_thread, lag

    try:
        loop_thread, lag = asyncio.run(main())
    finally:
        site.close()
    if mode == "pool" and pagetext is not None:
        # Not a silent thread fallback: the pool must really be what parsed the pages.
        assert pagetext._pool is not None, f"no worker pool: {pagetext._pool_disabled or 'not started'}"
    indexed = [s for s, _ in seen if s == "indexed"]
    errors = [e for s, e in seen if s == "error"]
    assert len(indexed) == n and not errors, (len(indexed), errors[:3])
    p95 = lag.pct(95)
    assert p95 < 50, (f"[{mode}] event-loop lag p95 {p95:.0f} ms over {len(lag.lags)} ticks during "
                      f"a crawl — pages are being parsed on the loop again")
    assert loop_thread not in threads.get("dns", set()), "the SSRF guard's DNS lookup ran on the loop"
    assert threads.get("log") and loop_thread not in threads["log"], \
        "the ingestion log was rewritten on the loop"


def test_smart_mode_parses_each_page_once(app_module, monkeypatch):
    """Smart mode extracted the text to count its words, threw it away, and the page
    handler extracted the same HTML again — twice the parsing for every page."""
    import trafilatura
    from visualweaver import crawler
    if pagetext is not None:
        monkeypatch.setattr(pagetext, "_pool_disabled", "test: count parses in-process")
    _isolate_crawl(monkeypatch, {})
    n = 6
    pages = {f"https://smart.example/p{i}": _page(i, 0, paragraphs=8).decode() for i in range(n)}
    pages["https://smart.example/"] = "".join(
        f"<a href='https://smart.example/p{i}'>p</a>" for i in range(n)) + _page(99, 0, 8).decode()

    async def fake_fetch(url):
        return pages.get(crawler._strip_fragment(url), "")

    browser_calls = []

    class _FakeBrowser:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def arun(self, url, config=None):
            browser_calls.append(url)
            raise RuntimeError("no browser in this test")

    monkeypatch.setattr(crawler, "fetch_url", fake_fetch)
    monkeypatch.setattr(crawler, "HAS_CRAWL4AI", True)
    monkeypatch.setattr(crawler, "_C4AIWebCrawler", _FakeBrowser, raising=False)
    monkeypatch.setattr(crawler, "_C4AIBrowserConfig", lambda **k: None, raising=False)
    monkeypatch.setattr(crawler, "_C4AIRunConfig", lambda **k: None, raising=False)
    parses = []
    real_extract = trafilatura.extract

    def counting(html, *a, **k):
        parses.append(1)
        return real_extract(html, *a, **k)

    monkeypatch.setattr(trafilatura, "extract", counting)
    threads: dict = {}

    def guard(url):
        threads.setdefault("dns", set()).add(threading.get_ident())

    monkeypatch.setattr(crawler, "assert_public_url", guard)
    indexed = []

    async def cb(u, status, n_=0, err="", count=0):
        if status == "indexed":
            indexed.append(u)

    async def main():
        await crawler.crawl_url("t", "https://smart.example/", depth=1, respect_robots=False,
                                smart_mode=True, rc=_FakeRedis(), concurrency=3,
                                force_reindex=True, progress_cb=cb)
        return threading.get_ident()

    loop_thread = asyncio.run(main())
    assert len(indexed) == n + 1, indexed
    assert not browser_calls, "pages with plenty of text went to the browser fallback"
    assert len(parses) == n + 1, f"{len(parses)} parses for {n + 1} pages — smart mode parses twice"
    assert threads.get("dns") and loop_thread not in threads["dns"], \
        "smart mode's SSRF guard resolved the host on the event loop"


def test_a_page_that_kills_its_parser_is_an_error_not_a_server_crash(app_module, monkeypatch):
    """A page in flight when a worker died is re-run alone in a quarantine worker. One that
    kills that worker too is reported as an error — it is never re-run inside the server
    process, where the same crash would take down the whole app — while an unlucky page
    that was merely in flight beside it comes back with its text."""
    from concurrent.futures.process import BrokenProcessPool
    from visualweaver import pagetext  # noqa: F811 — required here

    class _DeadPool:
        def submit(self, *a, **k):
            raise BrokenProcessPool("a worker died")
        def shutdown(self, *a, **k):
            pass

    def isolated(html, url, want_links):
        if "POISON" in html:
            raise BrokenProcessPool("killed the quarantine worker too")
        return "fine", []

    in_process = []
    monkeypatch.setattr(pagetext, "_pool_disabled", "")
    monkeypatch.setattr(pagetext, "warm", lambda: None)
    monkeypatch.setattr(pagetext, "_run_isolated", isolated)
    monkeypatch.setattr(pagetext, "extract_page", lambda *a, **k: in_process.append(1))

    async def main():
        pagetext._pool = _DeadPool()
        with pytest.raises(RuntimeError, match="crashed"):
            await pagetext.extract("<html><p>POISON</p></html>", "https://x/poison")
        pagetext._pool = _DeadPool()
        return await pagetext.extract("<html><p>innocent</p></html>", "https://x/ok")

    try:
        assert asyncio.run(main()) == ("fine", [])
    finally:
        pagetext._pool = None
    assert not in_process, "a page that crashed a worker was re-run in the server process"


def test_a_worker_dying_mid_crawl_costs_no_page_and_no_loop_stall(app_module):
    """A worker killed (an OOM kill, a crash) while pages are in flight broke every one of
    them at once: all were reported as errors, and rebuilding the pool held a lock the
    event loop then waited on. Now the pages in flight come back with their text and the
    loop keeps ticking."""
    import os
    import signal
    from visualweaver import pagetext  # noqa: F811 — required here
    pagetext.warm()
    if pagetext._pool is None:
        pytest.fail(f"no worker pool: {pagetext._pool_disabled}")
    old = pagetext._pool
    pages = [_page(100 + i, 0, paragraphs=600).decode() for i in range(12)]

    async def main():
        async with _LoopLag() as lag:
            jobs = [asyncio.create_task(pagetext.extract(h, f"https://x/{i}"))
                    for i, h in enumerate(pages)]
            await asyncio.sleep(0.03)
            victim = next(iter(old._processes.values()))
            os.kill(victim.pid, signal.SIGKILL)
            out = await asyncio.gather(*jobs, return_exceptions=True)
        return out, lag

    out, lag = asyncio.run(main())
    errors = [o for o in out if isinstance(o, BaseException)]
    assert not errors, f"{len(errors)} of {len(out)} pages failed because a worker died: {errors[:2]}"
    assert all(len(t) > 1000 for t, _ in out)
    assert pagetext._pool is not old, "the broken pool was not replaced"
    assert max(lag.lags) < 100, f"the event loop stalled {max(lag.lags):.0f} ms while the pool recovered"


_HEAVY = ("torch", "sentence_transformers", "fastapi", "visualweaver.main", "visualweaver.crawler")


def test_the_pool_workers_hold_only_the_parsers(app_module):
    from visualweaver import pagetext  # noqa: F811 — required here
    pagetext.warm()
    if pagetext._pool is None:
        pytest.fail(f"no worker pool: {pagetext._pool_disabled}")
    mods = pagetext._pool.submit(eval, "sorted(__import__('sys').modules)").result(timeout=60)
    assert "visualweaver.pagetext" in mods and "trafilatura" in mods
    heavy = [m for m in _HEAVY if m in mods]
    assert not heavy, f"a page-extraction worker imported {heavy}"


def test_workers_do_not_rerun_a_script_main(tmp_path):
    """A spawned worker re-runs the parent's main SCRIPT before it unpickles a job. The
    `visualweaver` console script (the Docker CMD) starts with `from visualweaver.main
    import cli`, so each worker would load the whole app, torch included — ~0.6 GB and
    ~4 s apiece — to run a parser. Reproduced with a script shaped the same way."""
    import os
    import subprocess
    import sys
    import textwrap
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "console_script_like.py"
    script.write_text(textwrap.dedent("""
        import sys
        import visualweaver.main            # what the console script does first
        if __name__ == "__main__":
            from visualweaver import pagetext
            pagetext.warm()
            if pagetext._pool is None:
                print("UNAVAILABLE", pagetext._pool_disabled)
                sys.exit(0)
            mods = pagetext._pool.submit(eval, "sorted(__import__('sys').modules)").result(timeout=60)
            print("MODS", " ".join(mods))
            pagetext.shutdown()
    """))
    env = {**os.environ, "PYTHONPATH": str(root)}
    r = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                       timeout=300, cwd=str(root), env=env)
    line = next((l for l in r.stdout.splitlines() if l.startswith(("MODS", "UNAVAILABLE"))), "")
    assert line, f"script failed: rc={r.returncode} {r.stderr[-1500:]}"
    assert not line.startswith("UNAVAILABLE"), line
    mods = set(line.split()[1:])
    heavy = [m for m in _HEAVY if m in mods]
    assert not heavy, f"a worker re-ran the main script and imported {heavy}"


def test_workers_exit_when_the_server_is_killed(tmp_path):
    """Killed outright (SIGKILL, or stop.sh's fallback after its grace period), the server
    never tells its workers to stop, and they used to block on the job queue forever,
    reparented to init — four more orphans per restart."""
    import os
    import signal
    import subprocess
    import sys
    import textwrap
    root = Path(__file__).resolve().parents[1]
    script = tmp_path / "server_like.py"
    script.write_text(textwrap.dedent("""
        import sys, time
        from visualweaver import pagetext
        pagetext.warm()
        print("PIDS", " ".join(str(p) for p in pagetext._pool._processes), flush=True)
        time.sleep(600)
    """))
    proc = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True,
                            cwd=str(root), env={**os.environ, "PYTHONPATH": str(root)})
    pids = []
    try:
        line = proc.stdout.readline()
        assert line.startswith("PIDS"), line
        pids = [int(p) for p in line.split()[1:]]
        assert pids
    finally:
        proc.kill()
        proc.wait(timeout=30)

    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    deadline = time.time() + 10
    while time.time() < deadline and any(alive(p) for p in pids):
        time.sleep(0.1)
    left = [p for p in pids if alive(p)]
    for p in left:                      # never leave them behind, even on failure
        os.kill(p, signal.SIGKILL)
    assert not left, f"{len(left)} extraction workers outlived the killed server: {left}"


# ── 2. two tabs on one conversation ──────────────────────────────────────────

@pytest.fixture
def chat(app_module, cfg, monkeypatch):
    """The chat WebSocket against a slow fake model, every socket on ONE event loop (as
    under uvicorn) and without the app's startup (which reloads config from disk)."""
    import anyio
    from visualweaver import appcore, cache, providers, sessions, state

    calls: list = []

    async def slow_stream(messages, model, images=None, usage=None):
        calls.append([m.get("content") for m in messages])
        for i in range(12):
            await asyncio.sleep(0.04)
            yield f"t{i} ", False
        yield "", True

    monkeypatch.setattr(providers, "ollama_stream", slow_stream)
    monkeypatch.setattr(cache, "cache_lookup", lambda *a, **k: None)
    monkeypatch.setattr(cache, "cache_store", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "save_session", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "load_session", lambda *a, **k: [])
    monkeypatch.setattr(sessions, "record_usage", lambda *a, **k: None)
    cfg.setdefault("sessions", {})["persist"] = False
    client = TestClient(appcore.app)
    client.model_calls = calls
    with anyio.from_thread.start_blocking_portal() as portal:
        client.portal = portal
        yield client
    for sid in [s for s in state._sessions if s.startswith(KEY_PREFIX)]:
        state._sessions.pop(sid, None)


_ASK = {"provider": "ollama", "model": "fake", "rag_instances": []}


def _frames_until(ws, kind: str, limit: float = 10.0) -> list[dict]:
    """Frames up to and including the first of type `kind`; fails rather than hangs."""
    out: list = []
    box: dict = {}

    def run():
        try:
            while True:
                d = ws.receive_json()
                out.append(d)
                if d.get("type") == kind:
                    return
        except Exception as e:            # socket closed under us
            box["err"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(limit)
    assert not t.is_alive(), f"no {kind!r} within {limit}s; got {[f.get('type') for f in out]}"
    assert "err" not in box, f"socket failed before {kind!r}: {box.get('err')!r}; got {out}"
    return out


def test_closing_one_tab_does_not_cancel_the_other_tabs_answer(chat):
    from visualweaver import state
    from visualweaver import ws as _ns_ws
    sid = f"{KEY_PREFIX}twotabs"
    with chat.websocket_connect(f"/ws/chat/{sid}") as a:
        with chat.websocket_connect(f"/ws/chat/{sid}") as b:
            b.send_json({"content": "hello", **_ASK})
            _frames_until(b, "stream_start")
            a.close()                                    # tab A closes mid-answer
            frames = _frames_until(b, "stream_end")
            tokens = [f for f in frames if f.get("type") == "token"]
            assert len(tokens) == 13, f"tab B's answer was cut off after {len(tokens)} tokens"
            assert not frames[-1].get("aborted")
            assert _ns_ws.mgr.active.get(sid) and len(_ns_ws.mgr.active[sid]) == 1, \
                "closing tab A unregistered tab B"
    assert not any(k for k in state._chat_tasks if getattr(k, "scope", {}).get("path", "").endswith(sid))
    assert sid not in _ns_ws.mgr.active


def test_stop_in_one_tab_does_not_stop_the_other(chat):
    sid = f"{KEY_PREFIX}twostop"
    with chat.websocket_connect(f"/ws/chat/{sid}") as a, \
            chat.websocket_connect(f"/ws/chat/{sid}") as b:
        b.send_json({"content": "hello", **_ASK})
        _frames_until(b, "stream_start")
        a.send_json({"type": "abort"})                   # A has nothing running
        assert _frames_until(a, "stream_end")[-1].get("aborted")
        frames = _frames_until(b, "stream_end")
        assert len([f for f in frames if f.get("type") == "token"]) == 13
        assert not frames[-1].get("aborted"), "tab A's Stop cancelled tab B's answer"


def test_a_message_from_the_second_tab_waits_its_turn_and_is_answered(chat):
    from visualweaver import state
    sid = f"{KEY_PREFIX}queued"
    with chat.websocket_connect(f"/ws/chat/{sid}") as a, \
            chat.websocket_connect(f"/ws/chat/{sid}") as b:
        a.send_json({"content": "first", **_ASK})
        _frames_until(a, "stream_start")
        b.send_json({"content": "second", **_ASK})
        first = _frames_until(b, "status")
        assert first[0] == {"type": "status", "phase": "queued"}, first
        b_frames = _frames_until(b, "stream_end")
        a_frames = _frames_until(a, "stream_end")
        assert len([f for f in b_frames if f.get("type") == "token"]) == 13
        assert len([f for f in a_frames if f.get("type") == "token"]) == 13
        roles = [(m["role"], m["content"][:6]) for m in state._sessions[sid]]
        assert roles[0] == ("user", "first") and roles[2] == ("user", "second"), roles
        assert len(roles) == 4, roles
        # The queued turn ran AFTER the first one, so it saw that exchange as history.
        b_call = next(c for c in chat.model_calls if c[-1] == "second")
        assert "first" in b_call and any(str(x).startswith("t0 ") for x in b_call), b_call
    assert sid not in state._session_turns, "a finished conversation left its turn lock behind"


def test_a_malformed_frame_is_answered_and_the_socket_keeps_working(chat):
    from visualweaver import state
    sid = f"{KEY_PREFIX}malformed"
    before = len(state._chat_tasks)
    with chat.websocket_connect(f"/ws/chat/{sid}") as a:
        a.send_text("{not json")
        err = _frames_until(a, "error")
        assert err[-1]["type"] == "error"
        a.send_text("[1, 2]")                            # valid JSON, not a request
        assert _frames_until(a, "error")[-1]["type"] == "error"
        a.send_json({"content": "still there?", **_ASK})
        assert _frames_until(a, "stream_end")[-1].get("type") == "stream_end"
    time.sleep(0.2)
    assert len(state._chat_tasks) == before, "the closed socket's task entry was left behind"


def test_a_second_message_from_the_same_tab_queues_behind_its_answer(chat):
    """The UI lets ↻ (regenerate) and "re-run without cache" fire while an answer is still
    streaming. Such a message used to be dropped without a word; now it waits for the
    answer ahead of it — which keeps streaming untouched — and is then answered."""
    from visualweaver import state
    sid = f"{KEY_PREFIX}samesock"
    with chat.websocket_connect(f"/ws/chat/{sid}") as a:
        a.send_json({"content": "first", **_ASK})
        _frames_until(a, "stream_start")
        a.send_json({"content": "impatient", **_ASK})
        first = _frames_until(a, "stream_end")
        types = [f.get("type") for f in first]
        assert {"type": "status", "phase": "queued"} in first, types
        assert "error" not in types, "the queued message cut the running answer short"
        assert len([t for t in types if t == "token"]) == 13
        second = _frames_until(a, "stream_end")
        assert len([f for f in second if f.get("type") == "token"]) == 13
    assert [m["content"] for m in state._sessions[sid] if m["role"] == "user"] == ["first", "impatient"]


def test_a_regenerate_clicked_mid_answer_applies_after_it(chat):
    """↻ on the previous answer while the next one streams: the tab has already dropped
    that answer and everything after it, so once the streaming turn lands the regenerate
    must truncate there — the transcripts then agree."""
    from visualweaver import state
    sid = f"{KEY_PREFIX}regen"
    with chat.websocket_connect(f"/ws/chat/{sid}") as a:
        a.send_json({"content": "q1", **_ASK})
        _frames_until(a, "stream_end")
        a.send_json({"content": "q2", **_ASK})
        _frames_until(a, "stream_start")
        a.send_json({"content": "q1", "replace_from": 1, "bypass_cache": True, **_ASK})
        _frames_until(a, "stream_end")                       # q2's answer
        frames = _frames_until(a, "stream_end")              # the regenerated q1
        assert not any(f.get("type") == "error" for f in frames), frames
    assert [(m["role"], m["content"][:2]) for m in state._sessions[sid]] == \
        [("user", "q1"), ("assistant", "t0")], state._sessions[sid]


def test_a_regenerate_from_a_tab_that_missed_a_change_is_refused(chat):
    """Tab B's ↻ names a position in the transcript B was showing. If tab A answered a
    question while B's regenerate waited, that position no longer means what B's user
    clicked on, and truncating there would delete A's answer."""
    from visualweaver import state
    sid = f"{KEY_PREFIX}staleregen"
    with chat.websocket_connect(f"/ws/chat/{sid}") as a, \
            chat.websocket_connect(f"/ws/chat/{sid}") as b:
        a.send_json({"content": "q1", **_ASK})
        _frames_until(a, "stream_end")
        a.send_json({"content": "q2", **_ASK})
        _frames_until(a, "stream_start")
        b.send_json({"content": "q1", "replace_from": 1, "bypass_cache": True, **_ASK})
        err = _frames_until(b, "error")[-1]
        assert "another tab" in err["content"], err
        _frames_until(a, "stream_end")
    assert [m["content"] for m in state._sessions[sid] if m["role"] == "user"] == ["q1", "q2"]


def test_a_turn_that_fails_says_so(chat, monkeypatch):
    """An exception escaping a turn died as an unretrieved task exception: the tab's typing
    indicator stayed up forever. It is reported — but never after stream_end, when the
    tab may already be streaming its next answer."""
    from visualweaver import routes_chat
    sid = f"{KEY_PREFIX}fails"

    async def boom(ws, sid_, msg, release=None):
        if msg["content"] == "after":
            await ws.send_json({"type": "stream_end", "latency": {}})
            release()
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(routes_chat, "handle_chat", boom)
    with chat.websocket_connect(f"/ws/chat/{sid}") as a:
        a.send_json({"content": "before", **_ASK})
        err = _frames_until(a, "error")[-1]
        assert "disk on fire" in err["content"], err
        a.send_json({"content": "after", **_ASK})
        _frames_until(a, "stream_end")
        a.send_json({"type": "abort"})
        frames = _frames_until(a, "stream_end")
        assert not any(f.get("type") == "error" for f in frames), \
            f"an error frame followed a completed answer: {frames}"


# ── 3. provider clients ──────────────────────────────────────────────────────

class _FakeOllama:
    def __init__(self, word: str):
        word_ = word

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = b"".join((json.dumps(d) + "\n").encode() for d in (
                    {"message": {"content": word_}, "done": False},
                    {"message": {"content": ""}, "done": True,
                     "prompt_eval_count": 3, "eval_count": 1}))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                b = json.dumps({"models": [{"name": word_}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def log_message(self, *a):
                pass

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_ollama_reuses_one_client_and_follows_a_settings_change(app_module, cfg, monkeypatch):
    """One AsyncClient per call cost ~12 ms of SSL setup on the loop, three times on a
    first turn. One pooled client now — and a changed host/port still applies at once,
    with each call keeping its own timeout."""
    import httpx
    from visualweaver import providers, state
    one, two = _FakeOllama("ONE"), _FakeOllama("TWO")
    built, timeouts = [], []
    real_init, real_stream = httpx.AsyncClient.__init__, httpx.AsyncClient.stream

    def init(self, *a, **k):
        built.append(1)
        real_init(self, *a, **k)

    def stream(self, *a, **k):
        timeouts.append(k.get("timeout"))
        return real_stream(self, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    monkeypatch.setattr(state, "_provider_http_client", None, raising=False)

    async def ask() -> str:
        out = ""
        async for tok, done in providers.ollama_stream([{"role": "user", "content": "q"}], "m"):
            out += tok
            if done:
                break
        return out

    async def main():
        cfg["ollama"] = {"host": "http://127.0.0.1", "port": one.port, "model": "m"}
        a = [await ask() for _ in range(3)]
        cfg["ollama"] = {**cfg["ollama"], "port": two.port}      # Settings → Ollama port
        b = await ask()
        return a, b

    try:
        a, b = asyncio.run(main())
    finally:
        one.close(); two.close()
        monkeypatch.setattr(state, "_provider_http_client", None, raising=False)
    assert a == ["ONE"] * 3 and b == "TWO", (a, b)
    assert len(built) == 1, f"{len(built)} HTTP clients built for 4 Ollama calls"
    assert timeouts == [120] * 4, timeouts


def test_gemini_builds_one_client_per_key(app_module, cfg, monkeypatch):
    """google-genai's Client builds two HTTP clients (~25 ms); it was built on every
    stream, model listing and key check. One per key now, dropped on a settings save."""
    import types
    from visualweaver import config, providers
    if not providers._GENAI_AVAILABLE:
        pytest.skip("google-genai not installed")
    made = []

    class _Chunk:
        text, usage_metadata = "hi", None

    class _Models:
        async def generate_content_stream(self, **kw):
            async def gen():
                yield _Chunk()
            return gen()

    class _FakeClient:
        def __init__(self, api_key):
            made.append(api_key)
            self.aio = types.SimpleNamespace(models=_Models())

    monkeypatch.setattr(providers, "_google_genai", types.SimpleNamespace(Client=_FakeClient))
    config.invalidate_provider_clients()

    async def ask():
        return [t async for t, _ in providers.gemini_stream([{"role": "user", "content": "q"}], "g")]

    try:
        cfg["gemini"] = {"api_key": "k1", "model": "g"}
        for _ in range(3):
            assert asyncio.run(ask())[0] == "hi"
        assert made == ["k1"], made
        cfg["gemini"]["api_key"] = "k2"                    # a different key: its own client
        asyncio.run(ask())
        config.invalidate_provider_clients()               # a settings save drops them
        cfg["gemini"]["api_key"] = "k1"
        asyncio.run(ask())
        assert made == ["k1", "k2", "k1"], made
    finally:
        config.invalidate_provider_clients()


def test_the_key_checks_reuse_their_clients(app_module, cfg, monkeypatch):
    """Settings polls every provider's key check; each built its own client per call on
    the loop (Gemini ~25 ms, httpx ~12 ms). Same pooling as the chat path."""
    import types
    import anyio
    import httpx
    from visualweaver import appcore, config, providers, routes_settings, state
    if not providers._GENAI_AVAILABLE:
        pytest.skip("google-genai not installed")
    made, built = [], []

    class _FakeGenai:
        def __init__(self, api_key):
            made.append(api_key)
            self.models = types.SimpleNamespace(list=lambda: iter([object()]))

    real_init = httpx.AsyncClient.__init__

    def init(self, *a, **k):
        built.append(1)
        real_init(self, *a, **k)

    fake_genai = types.SimpleNamespace(Client=_FakeGenai)
    monkeypatch.setattr(providers, "_google_genai", fake_genai)
    # The route's own module reference too, so no variant of it can reach Google.
    monkeypatch.setattr(routes_settings, "_google_genai", fake_genai, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    monkeypatch.setattr(state, "_provider_http_client", None, raising=False)
    config.invalidate_provider_clients()
    mistral = _FakeOllama("x")                    # any 200 answers GET /models
    cfg["gemini"] = {"api_key": "g1", "model": "g"}
    cfg["mistral"] = {"api_key": "m1", "base_url": f"http://127.0.0.1:{mistral.port}"}
    client = TestClient(appcore.app)
    try:
        with anyio.from_thread.start_blocking_portal() as portal:
            client.portal = portal              # one loop, as under uvicorn
            for _ in range(3):
                assert client.get("/api/status/gemini").json() == {"ok": True}
                assert client.get("/api/status/mistral").json() == {"ok": True}
    finally:
        mistral.close()
        config.invalidate_provider_clients()
        monkeypatch.setattr(state, "_provider_http_client", None, raising=False)
    assert made == ["g1"], f"{len(made)} Gemini clients for 3 key checks"
    assert len(built) == 1, f"{len(built)} HTTP clients for 3 Mistral key checks"


# ── 4. progress bars ─────────────────────────────────────────────────────────

def test_embedding_and_reranking_never_draw_progress_bars(app_module, cfg, monkeypatch):
    """Left at its default, sentence-transformers draws a tqdm bar whenever its logger is
    at INFO — the app's is — so every question wrote "Batches: 100%" into the log."""
    import numpy as np
    from visualweaver import embeddings
    calls = []

    class _Model:
        def encode(self, *a, **k):
            calls.append(("encode", k.get("show_progress_bar")))
            return np.zeros(4, dtype=np.float32)

        def predict(self, pairs, *a, **k):
            calls.append(("predict", k.get("show_progress_bar")))
            return np.zeros(len(pairs))

    monkeypatch.setattr(embeddings, "get_embed_model", lambda *a, **k: _Model())
    monkeypatch.setattr(embeddings, "get_reranker", lambda: _Model())
    embeddings.embed("q", is_query=True)
    embeddings.embed("passage")
    embeddings.embed_batch(["a", "b"])
    embeddings.rerank_chunks("q", [{"text": "a"}, {"text": "b"}], 1)
    assert calls == [("encode", False)] * 3 + [("predict", False)], calls


# ── 5. system prompt ─────────────────────────────────────────────────────────

def test_a_customised_base_instruction_is_not_rediffed_every_turn(app_module, cfg, monkeypatch):
    """A saved instruction that differs from the shipped one was diffed against it on
    every chat turn (~4.4 ms on the loop). Composed once per distinct input now — and any
    input changing (the text, the visual flag, the shipped default) recomposes."""
    from visualweaver import config, constants
    real = config.base_instruction_drift
    diffs = []

    def counting(*a, **k):
        diffs.append(1)
        return real(*a, **k)

    monkeypatch.setattr(config, "base_instruction_drift", counting)
    stale = constants.DEFAULT_BASE_INSTRUCTION.replace("```circuit", "```zzz") + "\nMy own line."
    cfg["base_instruction"] = stale
    cfg["visual_instructions"] = True
    first = config.compose_system_prompt("")
    for _ in range(20):
        assert config.compose_system_prompt("") == first
    assert len(diffs) == 1, f"the base instruction was diffed {len(diffs)} times for 21 turns"
    assert "My own line." in first and "```circuit" in first        # topped up, kept
    assert config.compose_system_prompt("Be a pirate.").endswith("Be a pirate.")

    cfg["base_instruction"] = stale + "\nAnother line."                # Settings saved
    assert "Another line." in config.compose_system_prompt("")
    cfg["visual_instructions"] = False                                  # visual blocks off
    assert constants.VISUAL_SECTION_MARKER not in config.compose_system_prompt("")
    cfg["visual_instructions"] = True
    cfg["base_instruction"] = stale
    assert config.compose_system_prompt("") == first                     # back: cached again
    n = len(diffs)
    monkeypatch.setattr(constants, "DEFAULT_BASE_INSTRUCTION",          # an upgrade
                        constants.DEFAULT_BASE_INSTRUCTION + "\n- one more shipped line")
    config.compose_system_prompt("")
    assert len(diffs) == n + 1, "a new shipped instruction did not recompose the prompt"
    for bad in (5, ["x"], {"a": 1}, None):                              # hand-edited config
        cfg["base_instruction"] = bad
        assert isinstance(config.compose_system_prompt(""), str)


# ── 6. blocking writes ───────────────────────────────────────────────────────

def test_uploads_are_written_off_the_loop(app_module, cfg, monkeypatch):
    import io
    from fastapi import UploadFile
    from visualweaver import ingest, rag_admin, routes_ingestion
    where = []
    real = Path.write_bytes

    def write_bytes(self, data):
        where.append(threading.get_ident())
        return real(self, data)

    async def fake_ingest(instance, dest, name, rc):
        return {"source": name, "status": "ok", "chunks": 1}

    monkeypatch.setattr(Path, "write_bytes", write_bytes)
    monkeypatch.setattr(ingest, "ingest_file", fake_ingest)
    monkeypatch.setattr(rag_admin, "_rc_for", lambda *a, **k: None)

    async def main():
        f = UploadFile(filename=f"{KEY_PREFIX}up.txt", file=io.BytesIO(b"x" * 1024))
        out = await routes_ingestion.api_ingest_files("uploadtest", [f])
        return threading.get_ident(), out

    loop_thread, out = asyncio.run(main())
    assert out and out[0]["status"] == "ok"
    assert where and loop_thread not in where, "the upload was written on the event loop"


def test_config_saves_from_async_routes_run_off_the_loop(app_module, cfg, monkeypatch):
    from visualweaver import config, embeddings, redis_store, routes_misc, routes_settings
    where = []
    monkeypatch.setattr(config, "save_config", lambda c: where.append(threading.get_ident()))
    monkeypatch.setattr(redis_store, "get_redis", lambda *a, **k: None)      # Settings' rewarm
    monkeypatch.setattr(embeddings, "get_embed_model", lambda *a, **k: None)

    async def main():
        await routes_misc.api_save_templates([{"name": "t", "system": "s"}])
        await routes_settings.api_save_config({"history_max_tokens": 3000})
        await asyncio.sleep(0.05)                         # let the background rewarm finish
        return threading.get_ident()

    loop_thread = asyncio.run(main())
    assert len(where) == 2 and loop_thread not in where, "the config was written on the event loop"


def test_file_ingest_logs_off_the_loop(app_module, cfg, monkeypatch, tmp_path):
    from visualweaver import config, ingest
    where = []
    monkeypatch.setattr(config, "append_log", lambda e: where.append(threading.get_ident()))
    monkeypatch.setattr(ingest, "ingest_text", lambda *a, **k: 3)
    f = tmp_path / "note.txt"
    f.write_text("some text worth indexing")

    async def main():
        out = await ingest.ingest_file("logtest", f, "note.txt", None)
        return threading.get_ident(), out

    loop_thread, out = asyncio.run(main())
    assert out["status"] == "ok" and where and loop_thread not in where, \
        "a file ingest rewrote the ingestion log on the event loop"


def test_concurrent_config_saves_leave_the_newest_on_disk(app_module, cfg, data_dir, monkeypatch):
    """Saves run in threads now, so they can finish out of order: an OLDER copy reaching
    the disk last would undo the newer one (bring back a deleted connection, say). Each
    copy is numbered when it is taken and an older one is dropped."""
    import copy as _copy
    from visualweaver import config, constants
    monkeypatch.setattr(constants, "CONFIG_PATH", data_dir / "config.json")
    real_deepcopy = _copy.deepcopy
    loop_thread = {}

    def slow_for_the_older(x, *a, **k):
        # save_config's own copy, in its thread: hold the older save back so the newer
        # one reaches the disk first.
        if (isinstance(x, dict) and x.get("marker") == 1
                and threading.get_ident() != loop_thread.get("id")):
            time.sleep(0.3)
        return real_deepcopy(x, *a, **k)

    monkeypatch.setattr(config.copy, "deepcopy", slow_for_the_older)

    async def main():
        loop_thread["id"] = threading.get_ident()
        cfg["marker"] = 1
        older = asyncio.create_task(config.save_config_async(cfg))
        await asyncio.sleep(0.05)
        cfg["marker"] = 2
        await config.save_config_async(cfg)                  # newer, and on disk first
        await older

    try:
        asyncio.run(main())
        on_disk = json.loads((data_dir / "config.json").read_text())
        assert on_disk.get("marker") == 2, f"the older copy was published last: {on_disk.get('marker')}"

        # A plain save_config — the sync routes, in FastAPI's threadpool — takes part in
        # the same order: an older threaded copy still in flight does not land over it.
        async def mixed():
            cfg["marker"] = 1
            older = asyncio.create_task(config.save_config_async(cfg))
            await asyncio.sleep(0.05)
            cfg["marker"] = 3
            await asyncio.to_thread(config.save_config, cfg)
            await older

        asyncio.run(mixed())
        assert json.loads((data_dir / "config.json").read_text())["marker"] == 3, \
            "an older threaded save landed over a newer plain save_config"
    finally:
        cfg.pop("marker", None)
    assert not list(data_dir.glob(".config.json.*")), "a dropped write left its temp file"


def test_feedback_is_written_off_the_loop_and_atomically(app_module, data_dir, monkeypatch):
    """The store (up to 2,000 entries) was rewritten on the loop with open(path, "w"),
    which truncates first: a failure mid-write lost every rating ever given."""
    from visualweaver import config, constants, routes_misc, state
    path = data_dir / "feedback.json"
    monkeypatch.setattr(constants, "FEEDBACK_PATH", path)
    monkeypatch.setattr(state, "_feedback", [])
    asyncio.run(routes_misc.api_feedback({"value": 1, "comment": "good"}))
    assert [e["comment"] for e in json.loads(path.read_text())] == ["good"]

    # A failure while serialising must leave the previous file whole, and no temp behind.
    def boom(*a, **k):
        raise OSError("disk full")

    with monkeypatch.context() as mp:
        mp.setattr(json, "dumps", boom)
        mp.setattr(json, "dump", boom)
        with pytest.raises(OSError):
            asyncio.run(routes_misc.api_feedback({"value": 1, "comment": "lost"}))
    kept = json.loads(path.read_text())
    assert [e["comment"] for e in kept] == ["good"], "a failed write destroyed the stored ratings"
    assert not list(data_dir.glob(".feedback.json.*")), "a temp file was left behind"

    # And it is written in a thread, not on the loop.
    where = []
    real_open = open

    def tracked_open(file, *a, **k):
        if Path(str(file)).parent == data_dir:
            where.append(threading.get_ident())
        return real_open(file, *a, **k)

    import builtins
    import os
    real_fdopen = os.fdopen

    def tracked_fdopen(*a, **k):
        where.append(threading.get_ident())
        return real_fdopen(*a, **k)

    monkeypatch.setattr(builtins, "open", tracked_open)
    monkeypatch.setattr(os, "fdopen", tracked_fdopen)

    async def rate():
        await routes_misc.api_feedback({"value": -1, "comment": "bad"})
        return threading.get_ident()

    loop_thread = asyncio.run(rate())
    monkeypatch.undo()
    assert where and loop_thread not in where, "feedback was written on the event loop"
    assert [e["comment"] for e in json.loads(path.read_text())][-1] == "bad"
