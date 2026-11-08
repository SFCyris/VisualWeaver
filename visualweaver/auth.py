# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.auth — sign-in through a trusted reverse proxy (off by default).

With ``auth.mode`` unset or ``"off"`` nothing here changes a request: there is no user,
every caller may do everything, and conversations are stored under the ids the browser
chose, exactly as in a single-user install.

With ``auth.mode == "header"`` an authenticating proxy (Authelia, Authentik, oauth2-proxy
behind Caddy/nginx/Traefik) signs people in and passes the user name in a header
(``Remote-User`` by default). The header is believed ONLY on a connection from one of
the trusted proxy addresses; the same header on any other connection is refused, and a
request without it is answered 401 — except the liveness probe, GET /api/health.

Every route is classified here, in one place: PUBLIC (no sign-in), USER (any signed-in
user) or — by default, for every route not listed — ADMIN. A route added later is
therefore administrator-only until someone deliberately lists it.

Configuration (config.json ``auth`` section; each value can be overridden by an env var):

    mode            "off" | "header"                 VISUALWEAVER_AUTH
    user_header     default "Remote-User"            VISUALWEAVER_AUTH_USER_HEADER
    groups_header   default "Remote-Groups"          VISUALWEAVER_AUTH_GROUPS_HEADER
    name_header     default "Remote-Name"            VISUALWEAVER_AUTH_NAME_HEADER
    trusted_proxies addresses/CIDRs, default loopback VISUALWEAVER_TRUSTED_PROXIES (comma-separated)
    admin_users     user names                       VISUALWEAVER_ADMIN_USERS (comma-separated)
    admin_groups    group names                      VISUALWEAVER_ADMIN_GROUPS (comma-separated)
    case_insensitive_names  true/false, default false VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES (1/true/yes/on)

User and group names are compared exactly ("Root" is not "root"); with
``case_insensitive_names`` true, ASCII letters are compared without regard to case (for a
proxy whose names are case-insensitive). See fold_name.

A configuration that cannot be read safely (an ``auth`` value that is not an object, an
unknown mode, an address that does not parse or has host bits set, proxy ranges that
cover every address) does not fall back to "off": every request is refused with 503
until it is fixed. Nor does a config.json that could not be loaded at all
(config.load_config puts UNREADABLE_MODE in its place): 503 until it is restored, or
VISUALWEAVER_AUTH states the mode. In both states GET /api/health alone answers, 200
with ``status: "misconfigured"`` and the refusal message only, so start scripts and
container health checks see the app is up but refusing.
"""
from __future__ import annotations

import contextvars
import hashlib
import ipaddress
import json
import logging
import os
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

DEFAULT_USER_HEADER = "Remote-User"
DEFAULT_GROUPS_HEADER = "Remote-Groups"
DEFAULT_NAME_HEADER = "Remote-Name"
DEFAULT_PROXIES = ("127.0.0.1/32", "::1/128")
_MAX_NAME = 256

# ── Route classes ────────────────────────────────────────────────────────────
# (method, route path as registered). "WS" is the WebSocket handshake. HEAD is
# classified as GET. Anything not listed is ADMIN.
PUBLIC_ROUTES = frozenset({
    ("GET", "/api/health"),          # liveness probe (Docker HEALTHCHECK, start.sh)
})

USER_ROUTES = frozenset({
    # the page and who is signed in
    ("GET", "/"),
    ("GET", "/api/me"),
    # chatting
    ("WS", "/ws/chat/{sid}"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/upload-file"),
    ("GET", "/api/files/image"),
    ("POST", "/api/feedback"),
    ("DELETE", "/api/cache/entry"),   # "remove this cached answer" on an answer served to you
    # the caller's own conversations (sessions.scoped_sid keeps them apart)
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/{sid}"),
    ("PATCH", "/api/sessions/{sid}"),
    ("DELETE", "/api/sessions/{sid}"),
    ("POST", "/api/sessions/{sid}/fork"),
    # projects: everyone may list them and file their own conversations under one
    # (PATCH /api/sessions/{sid}); creating, changing and deleting them is for admins
    ("GET", "/api/projects"),
    # reading the settings the chat page needs (secrets are redacted by the route)
    ("GET", "/api/config"),
    ("GET", "/api/templates"),
    # knowledge bases are shared: everyone may list, browse and search them
    ("GET", "/api/rag/instances"),
    ("GET", "/api/rag/{instance}/sources"),
    ("GET", "/api/rag/{instance}/documents"),
    ("GET", "/api/rag/{instance}/chunks"),
    ("GET", "/api/rag/{instance}/embedding-models"),
    ("GET", "/api/embedding/models"),
    ("GET", "/api/ingest/active"),
    ("GET", "/api/crawl/active"),
    # status, analytics and About (read-only)
    ("GET", "/api/status/redis"),
    ("GET", "/api/status/redis/{endpoint_name}"),
    ("GET", "/api/status/ollama"),
    ("GET", "/api/status/claude"),
    ("GET", "/api/status/openai"),
    ("GET", "/api/status/qwen"),
    ("GET", "/api/status/mistral"),
    ("GET", "/api/status/groq"),
    ("GET", "/api/status/gemini"),
    ("GET", "/api/ollama/models"),
    ("GET", "/api/claude/models"),
    ("GET", "/api/openai/models"),
    ("GET", "/api/qwen/models"),
    ("GET", "/api/mistral/models"),
    ("GET", "/api/groq/models"),
    ("GET", "/api/gemini/models"),
    ("GET", "/api/storage"),
    ("GET", "/api/components"),
    ("GET", "/api/redis/memory"),
    ("GET", "/api/redis/all-stats"),
    ("GET", "/api/cache/stats"),      # the cached questions themselves: admins only (route)
    ("GET", "/api/rag/stats"),
    ("GET", "/api/rag/advice"),
    ("GET", "/api/usage"),
})


@dataclass(frozen=True)
class User:
    id: str                 # the owner key: the header value, trimmed (see fold_name)
    name: str               # what the UI shows
    admin: bool
    groups: tuple = ()


@dataclass(frozen=True)
class Settings:
    on: bool = False
    user_header: str = DEFAULT_USER_HEADER.lower()
    groups_header: str = DEFAULT_GROUPS_HEADER.lower()
    name_header: str = DEFAULT_NAME_HEADER.lower()
    proxies: tuple = ()
    admin_users: frozenset = frozenset()
    admin_groups: frozenset = frozenset()
    fold_case: bool = False         # auth.case_insensitive_names (see fold_name)
    error: str = ""
    warnings: tuple = field(default=())


_current: contextvars.ContextVar = contextvars.ContextVar("visualweaver_user", default=None)


def current() -> User | None:
    """The signed-in user of this request; None when sign-in is off."""
    return _current.get()


def owner_id() -> str | None:
    u = _current.get()
    return u.id if u else None


def is_admin() -> bool:
    """Administrator rights for this request. With sign-in off everyone has them."""
    u = _current.get()
    return u is None or u.admin


def partition(owner: str) -> str:
    """A short, store-safe tag for one user: used in conversation ids and answer-cache scopes."""
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


def cache_partition() -> str:
    """The answer-cache partition of the signed-in user ("" when sign-in is off)."""
    owner = owner_id()
    return partition(owner) if owner else ""


# ── Settings ─────────────────────────────────────────────────────────────────

def _list(v) -> list:
    if v is None:
        return []
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()]
    if isinstance(v, (list, tuple)):
        return [str(p).strip() for p in v if str(p).strip()]
    return [str(v).strip()]


# The ``auth`` value config.load_config puts in place of a config.json it could not read
# or parse: whether sign-in was on is unknown, so every request is refused (503) until
# the file is fixed or VISUALWEAVER_AUTH states the mode.
UNREADABLE_MODE = "unknown (config.json could not be loaded)"


def _raw_settings() -> dict:
    from . import state
    a = state._config.get("auth")
    if a is None:
        raw = {}
    elif isinstance(a, dict):
        raw = dict(a)
    else:
        # "auth": "header" and the like: a config error, never a silent "off".
        raw = {"mode": f"(auth is a {type(a).__name__}, not an object)"}
    env = os.environ
    for key, var in (("mode", "VISUALWEAVER_AUTH"), ("user_header", "VISUALWEAVER_AUTH_USER_HEADER"),
                     ("groups_header", "VISUALWEAVER_AUTH_GROUPS_HEADER"),
                     ("name_header", "VISUALWEAVER_AUTH_NAME_HEADER"),
                     ("trusted_proxies", "VISUALWEAVER_TRUSTED_PROXIES"),
                     ("admin_users", "VISUALWEAVER_ADMIN_USERS"),
                     ("admin_groups", "VISUALWEAVER_ADMIN_GROUPS"),
                     ("case_insensitive_names", "VISUALWEAVER_AUTH_CASE_INSENSITIVE_NAMES")):
        if env.get(var) is not None and env.get(var) != "":
            raw[key] = env[var]
    return raw


def _on(v) -> bool:
    """A switch that is on only when clearly stated: true, or "1"/"true"/"yes"/"on"."""
    if isinstance(v, bool):
        return v
    return isinstance(v, str) and v.strip().lower() in ("1", "true", "yes", "on")


def fold_name(name: str, fold_case: bool = False) -> str | None:
    """The key a user (or group) name is compared by: the name exactly as given — "Root"
    is not "root", as on a proxy whose names are case-sensitive. With ``fold_case``
    (auth.case_insensitive_names) an ASCII name is compared without regard to case; any
    other name stays exact. None for a name that Unicode compatibility normalisation
    (NFKC) changes — a look-alike such as the Kelvin sign in "\u212aate" or fullwidth
    letters — which is never accepted."""
    if unicodedata.normalize("NFKC", name) != name:
        return None
    return name.lower() if fold_case and name.isascii() else name


# Broader than this draws a warning: a proxy range should name the proxy, not a network.
_WIDE_V4, _WIDE_V6 = 24, 64


def _parse_proxies(raw: dict) -> tuple[tuple, str, list]:
    """(networks, error, warnings) for the trusted proxy addresses."""
    nets, warnings = [], []
    for p in _list(raw.get("trusted_proxies")) or list(DEFAULT_PROXIES):
        try:
            net = ipaddress.ip_network(p, strict=True)
        except ValueError as e:
            if "has host bits set" in str(e):
                return (), f"auth.trusted_proxies: {p!r} has host bits set (did you mean " \
                           f"{p.split('/')[0]} or {ipaddress.ip_network(p, strict=False)}?)", []
            return (), f"auth.trusted_proxies: {p!r} is not an IP address or CIDR range", []
        mapped = getattr(net.network_address, "ipv4_mapped", None)
        if mapped is not None and net.prefixlen >= 96:
            # ::ffff:10.0.0.5 is how an IPv4 peer looks on a dual-stack socket; peers are
            # compared as IPv4 (see _peer), so the range is too.
            net = ipaddress.ip_network(f"{mapped}/{net.prefixlen - 96}")
        if net.prefixlen == 0:
            return (), f"auth.trusted_proxies: {p!r} would trust every address", []
        if net.prefixlen < (_WIDE_V4 if net.version == 4 else _WIDE_V6):
            warnings.append(f"auth.trusted_proxies: {p} trusts {net.num_addresses:,} addresses; "
                            f"list the proxy's own address instead of a network")
        nets.append(net)
    for v, total in ((4, 2 ** 32), (6, 2 ** 128)):
        cover = sum(n.num_addresses for n in ipaddress.collapse_addresses(
            [n for n in nets if n.version == v]))
        if cover >= total:
            return (), f"auth.trusted_proxies: together the ranges trust every IPv{v} address", []
    return tuple(nets), "", warnings


def parse_settings(raw: dict) -> Settings:
    """Settings from the merged config/env values (see the module docstring)."""
    mode = str(raw.get("mode") or "off").strip().lower()
    if mode in ("off", "none", "false", "0", ""):
        return Settings()
    if mode not in ("header", "trusted-header", "trusted_header"):
        if raw.get("mode") == UNREADABLE_MODE:
            return Settings(on=True, error="config.json could not be loaded, so whether sign-in is on "
                                           "is unknown: restore config.json, or set VISUALWEAVER_AUTH "
                                           "(off or header)")
        return Settings(on=True, error=f"auth.mode {raw.get('mode')!r} is not 'off' or 'header'")
    hdr = {}
    for key, default in (("user_header", DEFAULT_USER_HEADER), ("groups_header", DEFAULT_GROUPS_HEADER),
                         ("name_header", DEFAULT_NAME_HEADER)):
        v = str(raw.get(key) or default).strip().lower()
        if not v or any(c in v for c in " :\r\n\t"):
            return Settings(on=True, error=f"auth.{key} {raw.get(key)!r} is not a header name")
        hdr[key] = v
    nets, err, warnings = _parse_proxies(raw)
    if err:
        return Settings(on=True, error=err)
    fold = _on(raw.get("case_insensitive_names"))
    admins = frozenset(f for f in (fold_name(u, fold) for u in _list(raw.get("admin_users"))) if f)
    groups = frozenset(f for f in (fold_name(g, fold) for g in _list(raw.get("admin_groups"))) if f)
    if not admins and not groups:
        warnings.append("no admin_users or admin_groups are set: nobody can change Settings")
    return Settings(on=True, proxies=nets, admin_users=admins, admin_groups=groups,
                    fold_case=fold, warnings=tuple(warnings), **hdr)


_cache: tuple = (None, Settings())


def settings() -> Settings:
    """The sign-in settings now in force (re-parsed only when config or env changed)."""
    global _cache
    raw = _raw_settings()
    key = json.dumps(raw, sort_keys=True, default=str)
    if _cache[0] != key:
        _cache = (key, parse_settings(raw))
    return _cache[1]


_proxy_cache: tuple = (None, Settings())


def proxy_settings() -> Settings:
    """The trusted proxy addresses, with sign-in on or off: the cross-site guard believes
    X-Forwarded-Host only from them. Sign-in off: auth.trusted_proxies /
    VISUALWEAVER_TRUSTED_PROXIES if set, else loopback; a list that does not parse trusts
    no one."""
    global _proxy_cache
    s = settings()
    if s.on:
        return s
    raw = _raw_settings()
    key = json.dumps(raw.get("trusted_proxies"), sort_keys=True, default=str)
    if _proxy_cache[0] != key:
        nets, err, _w = _parse_proxies(raw)
        _proxy_cache = (key, Settings(proxies=() if err else nets))
    return _proxy_cache[1]


def enabled() -> bool:
    return settings().on


def describe() -> str:
    """One line for the startup log."""
    s = settings()
    if not s.on:
        return "Sign-in: off (single user)"
    if s.error:
        return f"Sign-in: MISCONFIGURED — every request is refused ({s.error})"
    return (f"Sign-in: trusted header '{s.user_header}' from {', '.join(map(str, s.proxies))}; "
            f"admins: {', '.join(sorted(s.admin_users)) or '-'}; "
            f"admin groups: {', '.join(sorted(s.admin_groups)) or '-'}")


def startup_warnings(host: str | None) -> list[str]:
    """What the operator must hear at start: a broken config, no admins, a server bound to
    a network address while only loopback is trusted (a proxy elsewhere is refused)."""
    s = settings()
    if not s.on:
        return []
    out = [f"sign-in configuration error: {s.error}"] if s.error else list(s.warnings)
    loopback_only = all(n.is_loopback for n in s.proxies)
    h = (host or "").strip()
    try:
        bound_loopback = ipaddress.ip_address(h.strip("[]")).is_loopback
    except ValueError:
        bound_loopback = h == "localhost"
    if not s.error and not bound_loopback and loopback_only:
        out.append(f"bound to {h or 'all interfaces'} but only loopback is a trusted proxy: requests "
                   f"from other machines are refused. Set auth.trusted_proxies "
                   f"(VISUALWEAVER_TRUSTED_PROXIES) to your proxy's address, and keep this port "
                   f"reachable only from that proxy.")
    elif not s.error and not bound_loopback:
        out.append(f"bound to {h or 'all interfaces'}: make sure this port is reachable ONLY from "
                   f"the trusted proxy ({', '.join(map(str, s.proxies))}).")
    return out


# ── Request handling ─────────────────────────────────────────────────────────

def _peer(scope) -> ipaddress._BaseAddress | None:
    client = scope.get("client")
    if not client:
        return None
    try:
        ip = ipaddress.ip_address(str(client[0]).split("%")[0])
    except ValueError:
        return None
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped or ip


def trusted(scope, s: Settings | None = None) -> bool:
    s = s or settings()
    ip = _peer(scope)
    return ip is not None and any(ip.version == n.version and ip in n for n in s.proxies)


_route_memo: "OrderedDict[tuple, str | None]" = OrderedDict()
_ROUTE_MEMO_MAX = 4096


def route_key(scope) -> tuple[str, str | None]:
    """(method, registered path) of the route this request reaches; path None when no
    route matches it in full (a 404, or a 405 for a method the route does not take)."""
    from starlette.routing import Match
    from . import appcore
    method = "WS" if scope["type"] == "websocket" else (scope.get("method") or "GET").upper()
    if method == "HEAD":
        method = "GET"
    memo = (scope["type"], scope.get("method"), scope.get("path"), len(appcore.app.router.routes))
    if memo in _route_memo:
        _route_memo.move_to_end(memo)
        return method, _route_memo[memo]
    found = None
    for r in appcore.app.router.routes:
        try:
            m, _ = r.matches(scope)
        except Exception:
            continue
        if m == Match.FULL:
            found = getattr(r, "path", None)
            break
    _route_memo[memo] = found
    while len(_route_memo) > _ROUTE_MEMO_MAX:
        _route_memo.popitem(last=False)
    return method, found


def policy(method: str, path: str | None) -> str:
    """"public", "user" or "admin" for a route. A request that matches no route needs a
    signed-in user (it is answered 404/405 and runs nothing)."""
    if path is None:
        return "user"
    if (method, path) in PUBLIC_ROUTES:
        return "public"
    if (method, path) in USER_ROUTES:
        return "user"
    return "admin"


def _decode(v: bytes) -> str:
    try:
        return v.decode("utf-8")
    except UnicodeDecodeError:
        return v.decode("latin-1")


def identify(scope, s: Settings) -> tuple[User | None, int, str]:
    """(user, status, why). status 0 = go on (user may be None: no identity header)."""
    name_key = s.user_header.encode("latin-1")
    headers = scope.get("headers") or []
    vals = [v for k, v in headers if k.lower() == name_key]
    if vals and not trusted(scope, s):
        return None, 403, "untrusted"
    if not vals:
        return None, 0, ""
    if len(vals) > 1:
        return None, 400, "duplicate"
    name = _decode(vals[0]).strip()
    if not name or len(name) > _MAX_NAME or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None, 400, "invalid"
    uid = fold_name(name, s.fold_case)
    if uid is None:
        return None, 400, "invalid"
    groups: list = []
    gkey = s.groups_header.encode("latin-1")
    for k, v in headers:
        if k.lower() == gkey:
            # Authelia / oauth2-proxy separate groups with commas, Authentik with "|".
            groups += [g.strip() for g in re.split(r"[,|]", _decode(v)) if g.strip()]
    display = name
    nkey = s.name_header.encode("latin-1")
    for k, v in headers:
        if k.lower() == nkey:
            d = _decode(v).strip()
            if d and len(d) <= _MAX_NAME and not any(ord(c) < 32 or ord(c) == 127 for c in d):
                display = d
            break
    admin = uid in s.admin_users or any(fold_name(g, s.fold_case) in s.admin_groups for g in groups)
    return User(id=uid, name=display, admin=admin, groups=tuple(groups)), 0, ""


_MESSAGES = {
    401: "Sign-in required: VisualWeaver is set to take the signed-in user from its reverse proxy.",
    403: "Not allowed.",
    400: "Bad sign-in header.",
    503: "Sign-in is misconfigured on the server; see its log.",
    "config": "VisualWeaver's config.json could not be loaded; see the server log.",
}


async def _deny(scope, send, status: int, detail: str) -> None:
    if scope["type"] == "websocket":
        # Before accept(): the handshake is refused (the browser sees the socket close).
        await send({"type": "websocket.close", "code": 1008})
        return
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


def _is_health_probe(scope) -> bool:
    return (scope["type"] == "http" and scope.get("method") == "GET"
            and scope.get("path") == "/api/health")


async def _misconfigured_health(send, detail: str) -> None:
    """GET /api/health while sign-in is misconfigured (or config.json could not be loaded):
    200 with ``status: "misconfigured"`` and the refusal message, so start.sh, docker.sh
    and the container health check can tell "up but refusing" from "not up". Nothing
    else is given — no version, provider or services — and every other route stays 503."""
    from . import constants
    body = json.dumps({"status": "misconfigured", "app": "VisualWeaver", "detail": detail,
                       "license": "AGPL-3.0-or-later", "source": constants.SOURCE_URL}).encode()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                            (b"cache-control", b"no-store")]})
    await send({"type": "http.response.body", "body": body})


_logged: "OrderedDict[str, float]" = OrderedDict()


def _log_once(key: str, every: float = 60.0) -> bool:
    """Rate-limit the refusal log line to one a minute per source address."""
    now = time.monotonic()
    last = _logged.get(key)
    if last is not None and now - last < every:
        return False
    _logged[key] = now
    _logged.move_to_end(key)
    while len(_logged) > 1024:
        _logged.popitem(last=False)
    return True


class AuthGuard:
    """ASGI middleware: sign-in and the admin check (inside the cross-site guard)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        s = settings()
        if not s.on:
            return await self.app(scope, receive, send)
        if s.error:
            detail = _MESSAGES["config"] if s.error.startswith("config.json") else _MESSAGES[503]
            if _is_health_probe(scope):
                return await _misconfigured_health(send, detail)
            return await _deny(scope, send, 503, detail)
        user, status, why = identify(scope, s)
        path = scope.get("path")
        if status:
            if why == "untrusted" and _log_once(str(_peer(scope) or scope.get("client"))):
                ip = _peer(scope)
                log.warning(f"Refused {scope['type']} {path}: it carries the sign-in header "
                            f"'{s.user_header}' but comes from {ip or scope.get('client')}, which is "
                            f"not a trusted proxy (auth.trusted_proxies). If your proxy runs on this "
                            f"machine and the address shown is a browser's, the server is rewriting "
                            f"the client address from X-Forwarded-For: start it with --no-proxy-headers.")
            return await _deny(scope, send, status, _MESSAGES.get(status, "Refused."))
        method, rpath = route_key(scope)
        need = policy(method, rpath)
        if user is None:
            if need == "public":
                return await self.app(scope, receive, send)
            return await _deny(scope, send, 401, _MESSAGES[401])
        if need == "admin" and not user.admin:
            return await _deny(scope, send, 403, "Administrators only.")
        token = _current.set(user)
        try:
            await self.app(scope, receive, send)
        finally:
            _current.reset(token)
