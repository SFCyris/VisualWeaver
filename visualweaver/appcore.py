# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.appcore — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FASTAPI APP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

app = FastAPI(title="VisualWeaver")

# CORS — same-origin by default. The bundled UI is served from this app, so it
# needs no cross-origin grant and none is given (a wildcard would let any site
# script the API from a user's browser). Set VISUALWEAVER_CORS_ORIGINS to a
# comma-separated list of trusted origins only if you host the UI separately.
_cors_origins = [
    o.strip() for o in os.environ.get("VISUALWEAVER_CORS_ORIGINS", "").split(",")
    if o.strip()
]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Security headers on every response — defense in depth alongside the frontend's
# DOMPurify sanitisation. 'unsafe-inline' is unavoidable (the single-file UI uses
# an inline <script>, inline styles, and static inline handlers), but the policy
# still blocks external script injection, plugins, framing, and cross-origin
# data exfiltration. cdnjs is allowed for marked / KaTeX / DOMPurify.
_CSP = (
    "default-src 'self'; "
    # cdnjs: marked/DOMPurify/KaTeX/abcjs/math.js + the lazy-loaded renderers
    # (mermaid, Chart.js, highlight.js, JSXGraph, Leaflet, Plotly).
    # jsDelivr: smiles-drawer (molecules) and @viz-js/viz (Graphviz) — neither is
    # published on cdnjs at a current version.
    # 'wasm-unsafe-eval': @viz-js/viz 3.x is a WebAssembly build (the old viz.js
    # 2.1.2 was asm.js). Without it the script loads but WebAssembly.instantiate()
    # is blocked and the ```dot lane dies with a CompileError. This directive
    # permits WebAssembly compilation ONLY — it does not enable eval() or
    # new Function().
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' "
    "https://cdnjs.cloudflare.com https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
    # https: is needed for map tiles (e.g. OpenStreetMap) in the ```map lane.
    "img-src 'self' data: blob: https:; "
    # cdn.jsdelivr.net: the Noto Music webfont, fetched only for text with music notation.
    "font-src 'self' https://cdnjs.cloudflare.com https://cdn.jsdelivr.net data:; "
    # paulrosen.github.io: abcjs fetches soundfont samples at play time for the
    # ```abc Play button. Scoped to that one host rather than opening connect-src.
    "connect-src 'self' https://paulrosen.github.io; "
    "worker-src 'self' blob:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "frame-ancestors 'self'; "
    "form-action 'self'"
)


@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("Content-Security-Policy", _CSP)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


# Cross-site request guard. There is no login, so any page the user opens could
# otherwise drive this API from their browser: run chats on the stored API keys over
# the WebSocket and read the answers, or post a config import / delete. A request
# that carries another site's Origin (or Sec-Fetch-Site: cross-site) may only be a
# plain navigation; everything else from another site is refused. Clients that send
# neither header (curl, scripts) are unaffected. A reverse proxy that rewrites the
# Host header makes the app's own pages look cross-site: the refusal is logged with
# the fix (forward the Host header, or list the public origin in
# VISUALWEAVER_CORS_ORIGINS).
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# DNS rebinding: an attacker's domain re-pointed at this machine makes its page
# "same-origin" with the app, so the Origin test above cannot tell. What it cannot
# fake is the host NAME the browser asks for. A browser request is therefore served
# only for host names that cannot belong to an outside attacker — IP addresses,
# localhost, single-label intranet names, private-use suffixes and this machine's own
# names — or names listed in VISUALWEAVER_ALLOWED_HOSTS (comma-separated; "*.corp.example"
# matches subdomains; "*" turns the check off), e.g. the domain of a reverse proxy.
_PRIVATE_SUFFIXES = (".localhost", ".local", ".lan", ".internal", ".home.arpa")
_allowed_hosts = {h.strip().lower().rstrip(".") for h in
                  os.environ.get("VISUALWEAVER_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _machine_names() -> set:
    names = {"localhost"}
    try:
        for n in (socket.gethostname(), socket.getfqdn()):
            n = (n or "").lower().rstrip(".")
            if n:
                names |= {n, n.split(".")[0], n.split(".")[0] + ".local"}
    except Exception:
        pass
    return names


_MACHINE_NAMES = _machine_names()


def _host_allowed(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host or host.startswith("["):            # no Host, or an IPv6 literal
        return True
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if "." not in name or name.endswith(_PRIVATE_SUFFIXES) or name in _MACHINE_NAMES:
        return True
    extra = _allowed_hosts | {urlsplit(o).hostname or "" for o in _cors_origins}
    return "*" in extra or name in extra or any(
        e.startswith("*.") and name.endswith(e[1:]) for e in extra)


def _norm_netloc(netloc: str, scheme: str) -> str:
    netloc = netloc.strip().lower()
    default = {"http": "80", "https": "443", "ws": "80", "wss": "443"}.get(scheme, "")
    if default and netloc.endswith(":" + default):
        netloc = netloc[: -len(default) - 1]
    return netloc


def _origin_allowed(scope) -> tuple[bool, str, str]:
    h = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    origin = h.get("origin", "").strip()
    host = h.get("x-forwarded-host") or h.get("host", "")
    site, mode = h.get("sec-fetch-site", ""), h.get("sec-fetch-mode", "")
    if (origin or site or mode) and not _host_allowed(host):
        return False, "host", host
    ws = scope["type"] == "websocket"
    method = (scope.get("method") or "GET").upper()
    navigation = not ws and method in _SAFE_METHODS and mode in ("navigate", "")
    if origin and origin != "null":
        if origin.rstrip("/") in {o.rstrip("/") for o in _cors_origins}:
            return True, origin, host
        parts = urlsplit(origin)
        scheme = parts.scheme or "http"
        if _norm_netloc(parts.netloc, scheme) in {_norm_netloc(host, scheme),
                                                   _norm_netloc(host, "https" if scheme == "http" else "http")}:
            return True, origin, host
        return navigation, origin, host
    if origin == "null":
        return navigation, origin, host
    if site in ("cross-site", "same-site") and not navigation:
        return False, f"(no Origin; Sec-Fetch-Site: {site})", host
    return True, origin, host


class _CrossSiteGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        ok, origin, host = _origin_allowed(scope)
        if ok:
            return await self.app(scope, receive, send)
        if origin == "host":
            name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            log.warning(f"Refused a browser request for host name '{host}' (DNS-rebinding guard). "
                        f"If this is how you reach VisualWeaver, set VISUALWEAVER_ALLOWED_HOSTS={name}")
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = (f"VisualWeaver does not answer browsers that reach it as '{name}'. If that is "
                    f"the address you use (for example a reverse proxy's domain), start VisualWeaver "
                    f"with VISUALWEAVER_ALLOWED_HOSTS={name}\n").encode()
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        log.warning(f"Refused a cross-site {scope['type']} request to {scope.get('path')} from {origin} "
                     f"(Host {host}). If this is VisualWeaver's own page behind a reverse proxy, forward "
                     f"the Host header or set VISUALWEAVER_CORS_ORIGINS={origin}.")
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = b'{"detail":"Cross-site request refused"}'
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(_CrossSiteGuard)


# Compress the ~400 KB single-file UI and all JSON API responses. Added last so it
# is the outermost layer (compresses the fully-headered response). Real-time SSE
# progress streams (text/event-stream) opt OUT by declaring Content-Encoding:
# identity, because gzip's internal buffering would hold their trickled events
# until the stream closes — GZipMiddleware forwards any response that already sets
# a content-encoding untouched.
# compresslevel 6 (zlib default): ~same ratio as 9 on text/JSON for markedly less
# CPU per response. Already-compressed payloads (PNG/JPEG via the image proxy) opt
# out with Content-Encoding: identity to avoid a wasted pass for ~0 byte gain.
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)

