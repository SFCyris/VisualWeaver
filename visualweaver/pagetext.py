# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.pagetext — text and link extraction for crawled pages, off the event loop.

Parsing a fetched page is pure CPU: trafilatura takes ~9 ms on an 80 KB page and the
BeautifulSoup link pass a few more, all of it holding the GIL. The crawler used to do
both on the event loop for every page, so while a crawl ran every other user's token
stream and Stop button stalled for that long, page after page (loop lag p95 ~200 ms,
max ~0.5 s with ten pages in flight).

A thread alone does not cure it — the GIL is still held — so the work goes to a small
pool of worker processes, each with its own interpreter. Those workers import THIS
module to unpickle a job, which is why it depends on nothing but the two parsers:
importing visualweaver.crawler would pull in the whole app (~3.7 s and torch, per
worker). If the platform cannot run worker processes, extraction falls back to two
threads of its own, which still leave the loop free between GIL switches.

Cross-module references stay module-qualified, as in the rest of the package.
"""
import asyncio
import contextlib
import logging
import multiprocessing
import os
import signal
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import urljoin, urlparse
try:
    import trafilatura   # Best-in-class web content extraction
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False
try:
    from bs4 import BeautifulSoup   # HTML parsing fallback + link extraction
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EXTRACTION (runs in a worker process)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_text(html: str, url: str) -> str:
    """
    Extract clean readable text from a fetched page.

    Plain-text and Markdown files (URL ends in .md/.txt, or content has no
    HTML tags) are returned directly — no stripping needed and HTML parsers
    would mangle them.  For real HTML pages we try trafilatura first (best
    quality), then BeautifulSoup, then fall back to the raw content.
    """
    url_lower = url.split("?")[0].lower()
    if url_lower.endswith(".md") or url_lower.endswith(".txt"):
        return html.strip()

    stripped = html.strip()
    if stripped and not stripped.startswith("<"):
        return stripped

    if HAS_TRAFILATURA:
        t = trafilatura.extract(html, include_links=False, include_images=False)
        if t:
            return t
    if HAS_BS4:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    return html


def _strip_fragment(url: str) -> str:
    """Remove the #anchor from a URL — same page regardless of anchor."""
    parsed = urlparse(url)
    return parsed._replace(fragment="").geturl()


# File extensions that are never useful to crawl as text content.
# Binary downloads, media, archives, executables, and data files.
_SKIP_EXTENSIONS: frozenset[str] = frozenset({
    # Archives / compressed
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst", ".rar", ".7z",
    # Executables / installers
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".apk", ".appimage",
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tiff",
    # Audio / video
    ".mp3", ".mp4", ".wav", ".ogg", ".flac", ".avi", ".mov", ".mkv", ".webm",
    # Documents (handled separately via file upload, not crawl)
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt",
    # Data / binary
    ".bin", ".dat", ".iso", ".img", ".whl", ".jar",
    # Fonts
    ".woff", ".woff2", ".ttf", ".eot",
})


def _is_crawlable_url(url: str) -> bool:
    """Return False if the URL points to a known binary/non-text file type."""
    path = urlparse(url).path.lower().split("?")[0]
    _, ext = os.path.splitext(path)
    return ext not in _SKIP_EXTENSIONS


def _extract_html_links(html: str, base_url: str) -> list[str]:
    """Return all absolute HTTP(S) links found in an HTML page, excluding binary file types."""
    if not HAS_BS4:
        return []
    try:
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = _strip_fragment(urljoin(base_url, a["href"]))
            p = urlparse(href)
            if p.scheme in ("http", "https") and p.netloc and _is_crawlable_url(href):
                links.append(href)
        return links
    except Exception:
        return []


def extract_page(html: str, url: str, want_links: bool = True) -> tuple[str, list[str]]:
    """(readable text, absolute links) for one fetched page — one job for a worker."""
    if not html:
        return "", []
    return extract_text(html, url), (_extract_html_links(html, url) if want_links else [])


def _worker_init() -> None:
    """Set up a worker process.

    Ctrl+C in a terminal reaches the whole process group; the server shuts the pool
    down itself, so a worker printing a KeyboardInterrupt traceback is noise.

    A worker must not outlive the server. Killed outright (SIGKILL, or stop.sh's
    fallback after its grace period), the server never tells its workers to stop, and
    they block on the job queue forever — every worker holds that pipe's write end, so
    it never reports end-of-file. The parent's sentinel does: it becomes readable the
    moment the server is gone, whatever killed it.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    parent = multiprocessing.parent_process()
    if parent is not None:
        def _exit_with_parent():
            parent.join()
            os._exit(0)
        threading.Thread(target=_exit_with_parent, name="vw-parent-watch", daemon=True).start()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# THE POOL (lives in the server process)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Small: extraction is ~10 ms a page, so a few workers outrun any realistic fetch rate,
# and every core they do not take stays with embedding and chat.
_POOL_WORKERS = max(1, min(4, (os.cpu_count() or 2) // 2))

_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()      # building the shared pool (only ever taken off the loop)
_spawn_lock = threading.Lock()     # every process start: _main_not_reimported edits __main__
_pool_disabled = ""                # why worker processes cannot run here; threads from then on

# A page that was in flight when a worker died runs again here, alone, so a crash is pinned
# on the page that caused it (see extract). One page at a time, on one thread and one
# single-worker pool of its own.
_quarantine_pool: ProcessPoolExecutor | None = None
_quarantine_runner = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vw-extract-quarantine")
# Where extraction runs when worker processes are unavailable. Two threads: more only
# contend for the GIL, and the default executor stays free for chat's own blocking calls.
_fallback_threads = ThreadPoolExecutor(max_workers=2, thread_name_prefix="vw-extract")


@contextlib.contextmanager
def _main_not_reimported():
    """Spawn workers without re-running the server's own __main__ in them.

    multiprocessing makes a spawned child re-import the parent's __main__ before it
    unpickles a job: by path for a script, by name for ``python -m``. ``-m uvicorn`` is
    exempt (a package __main__), but the ``visualweaver`` console script — the Docker
    CMD — and ``python -m visualweaver.main`` are not, and both import visualweaver.main:
    every worker would load the whole app, torch included (~0.6 GB and ~4 s each), to run
    a function that needs neither. The job's function lives in this module and nothing
    the workers receive is defined in __main__, so the re-import is never needed.

    multiprocessing reads these two attributes while it starts a process, so they are
    hidden only around the start calls, and only under _spawn_lock (two threads saving
    and restoring them at once could leave them hidden for good).
    """
    main = sys.modules.get("__main__")
    if main is None:
        yield
        return
    missing = object()
    saved_spec = getattr(main, "__spec__", missing)
    saved_file = getattr(main, "__file__", missing)
    try:
        if saved_spec is not missing:
            main.__spec__ = None
        if saved_file is not missing:
            del main.__file__
        yield
    finally:
        if saved_spec is not missing:
            main.__spec__ = saved_spec
        if saved_file is not missing:
            main.__file__ = saved_file


def _start_pool(workers: int) -> ProcessPoolExecutor:
    """A pool whose workers are all running before it is handed out. Blocking.

    ProcessPoolExecutor starts workers on demand inside submit(); starting them here,
    in a worker thread, keeps those fork+exec calls off the event loop as well.
    """
    pool = ProcessPoolExecutor(max_workers=workers,
                               mp_context=multiprocessing.get_context("spawn"),
                               initializer=_worker_init)
    try:
        with _spawn_lock, _main_not_reimported():
            # One job per worker: with none of them idle yet, each submit starts one more.
            futs = [pool.submit(extract_page, "", "") for _ in range(workers)]
        for f in futs:
            f.result(timeout=60)   # a worker that cannot even start fails here, not mid-crawl
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    return pool


def warm() -> None:
    """Start the shared pool if it is not running. Blocking — call it from a thread.

    Called at the start of every crawl, so the first page does not wait for workers to
    boot (~0.3 s) and the spawn never lands on the event loop. Never raises: a platform
    that cannot run worker processes is remembered and extraction uses threads.
    """
    global _pool, _pool_disabled
    if _pool_disabled:
        return
    with _pool_lock:
        if _pool is not None or _pool_disabled:
            return
        try:
            _pool = _start_pool(_POOL_WORKERS)
        except Exception as e:
            _pool_disabled = f"{type(e).__name__}: {e}"
            log.warning(f"Page extraction: worker processes unavailable ({_pool_disabled}) "
                        f"— extracting in threads instead")


def _forget(pool: ProcessPoolExecutor) -> None:
    """Drop a broken pool; the next warm() builds a fresh one.

    Runs on the event loop, so it takes no lock (warm() holds _pool_lock for a whole
    rebuild) and leaves the shutdown, which can wait on the pool's own teardown, to a
    thread. Only the first caller for a given pool does anything.
    """
    global _pool
    if _pool is not pool:
        return
    _pool = None
    threading.Thread(target=pool.shutdown, kwargs={"wait": False, "cancel_futures": True},
                     name="vw-extract-teardown", daemon=True).start()


def _run_isolated(html: str, url: str, want_links: bool) -> tuple[str, list[str]]:
    """Extract one page alone in the quarantine worker. Blocking; runs on
    _quarantine_runner, so pages pass through it one at a time and a crash there can
    only be this page's. Raises BrokenProcessPool if the page kills the worker."""
    global _quarantine_pool
    if _quarantine_pool is None:
        _quarantine_pool = _start_pool(1)
    try:
        return _quarantine_pool.submit(extract_page, html, url, want_links).result()
    except BrokenProcessPool:
        bad, _quarantine_pool = _quarantine_pool, None
        bad.shutdown(wait=False, cancel_futures=True)
        raise


def shutdown() -> None:
    """Stop every worker (server shutdown). Blocking — call it from a thread. A later page
    starts a fresh pool, so an app restarted in the same process (tests) still works."""
    global _pool, _quarantine_pool
    with _pool_lock:
        pool, _pool = _pool, None
    quarantine, _quarantine_pool = _quarantine_pool, None
    for p in (pool, quarantine):
        if p is not None:
            p.shutdown(wait=False, cancel_futures=True)


async def extract(html: str, url: str, want_links: bool = True) -> tuple[str, list[str]]:
    """(text, links) for a fetched page, computed off the event loop.

    In the shared worker pool normally; on two threads of our own when worker processes
    are unavailable here. When a worker dies, every page in flight with it fails at once
    — so each of them is re-run alone in a quarantine worker: the pages that were merely
    unlucky come back with their text, and only a page that kills its parser on its own
    is reported as an error. A crashing page is never re-run inside the server process,
    where the same crash would take the whole app down.
    """
    if not html:
        return "", []
    loop = asyncio.get_running_loop()
    if _pool is None and not _pool_disabled:
        await asyncio.to_thread(warm)
    if _pool_disabled:
        return await loop.run_in_executor(_fallback_threads, extract_page, html, url, want_links)
    pool = _pool
    if pool is not None:
        try:
            return await loop.run_in_executor(pool, extract_page, html, url, want_links)
        except BrokenProcessPool:
            pass
        except RuntimeError as e:
            # submit() on a pool that was closed meanwhile: shut down, or another page
            # already dropped it as broken.
            if "shutdown" not in str(e):
                raise
        _forget(pool)
    # A worker died with this page in flight — or the pool was dropped between warm()
    # and here because one did. Never in-process: alone, in the quarantine worker.
    try:
        return await loop.run_in_executor(_quarantine_runner, _run_isolated, html, url, want_links)
    except BrokenProcessPool:
        log.warning(f"Page extraction: {url} crashes the page parser — skipped")
        raise RuntimeError("the page parser crashed on this page") from None
