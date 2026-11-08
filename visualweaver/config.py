# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.config — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
try:
    import anthropic as _anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False
try:
    from openai import AsyncOpenAI as _AsyncOpenAI
    _OPENAI_SDK_AVAILABLE = True
except ImportError:
    _OPENAI_SDK_AVAILABLE = False
from . import constants, state

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG HELPERS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# Set when config.json existed but could not be parsed. Surfaced by /api/health
# so a corrupt config is visible instead of silently masquerading as a fresh install.


def load_config() -> dict:
    """Load config.json and deep-merge with defaults so new keys always exist.

    A corrupt file is NOT silently swallowed. Falling back to DEFAULT_CONFIG points
    ``redis.port`` at 6379 while a local install serves 6389/6390 — the app then
    starts "healthy" against an empty Redis and every RAG instance looks wiped.
    The bad file is preserved as ``config.json.corrupt-<ts>`` (so the keys in it are
    recoverable), the error is logged loudly, and ``_config_load_error`` is set so
    the health endpoint reports degraded rather than ok.
    """
    state._config_load_error = ""
    if not constants.CONFIG_PATH.exists():
        # A config.json that failed to parse was moved aside (below). A start without one
        # but with such a copy is not a fresh install: sign-in stays undecided until the
        # copy is restored or removed (see _unloaded_defaults).
        named = [k for k in _kept_copies() if _mentions_signin(k)]
        if named:
            state._config_load_error = (f"config.json is missing and an unreadable copy is kept "
                                        f"as {named[-1].name}")
            log.error(f"{state._config_load_error}: restore it as config.json (or delete the "
                      f"copies) — until then every request is refused unless VISUALWEAVER_AUTH is set.")
            return _unloaded_defaults()
    unparsable = None      # where the bytes of a config.json that did not parse now are
    if constants.CONFIG_PATH.exists():
        try:
            cfg = json.loads(_config_text(constants.CONFIG_PATH.read_bytes()))
            if not isinstance(cfg, dict):
                raise ValueError(f"expected a JSON object, got {type(cfg).__name__}")
            normalise_mongo_credentials(cfg)
            merged = {**constants.DEFAULT_CONFIG, **cfg}
            # Shallow-merge each nested dict so we pick up new sub-keys
            for k, v in constants.DEFAULT_CONFIG.items():
                if isinstance(v, dict):
                    merged[k] = {**v, **cfg.get(k, {})}
            normalise_session_retention(cfg, merged)
            return merged
        except OSError as e:
            # Unreadable is NOT corrupt. A permission error (e.g. the container UID
            # changed across an image update) must never rename the user's valid
            # config out of the way — renaming needs only directory write access,
            # so the quarantine below would succeed and destroy a good file.
            state._config_load_error = f"{type(e).__name__}: {e}"
            log.error("=" * 70)
            log.error(f"config.json could not be READ ({state._config_load_error}).")
            log.error("It was left untouched. Running on built-in DEFAULTS this session —")
            log.error("fix the file permissions and restart rather than re-entering settings,")
            log.error("otherwise the first save will overwrite your real configuration.")
            log.error("=" * 70)
        except Exception as e:
            state._config_load_error = f"{type(e).__name__}: {e}"
            quarantine = constants.CONFIG_PATH.with_name(
                f"{constants.CONFIG_PATH.name}.corrupt-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}")
            try:
                constants.CONFIG_PATH.replace(quarantine)
                kept = f" — kept as {quarantine.name}"
                unparsable = quarantine
            except Exception:
                kept = " — could NOT be preserved"
                unparsable = constants.CONFIG_PATH
            log.error("=" * 70)
            log.error(f"config.json could not be parsed ({state._config_load_error}){kept}.")
            log.error("Starting from built-in DEFAULTS — API keys and your Redis endpoint")
            log.error(f"are NOT the ones you configured (defaults point at "
                      f"{constants.DEFAULT_CONFIG['redis']['host']}:{constants.DEFAULT_CONFIG['redis']['port']}).")
            log.error("Restore the quarantined file or re-enter settings before ingesting.")
            log.error("=" * 70)
    if state._config_load_error:
        if unparsable is not None and not _mentions_signin(unparsable) \
                and not any(_mentions_signin(k) for k in _kept_copies()):
            # Damaged, but neither it nor an earlier damaged copy configured sign-in: the
            # app runs as before on the defaults (health says degraded), as an install
            # without sign-in always did. An empty or cut-short file is such a file.
            return copy.deepcopy(constants.DEFAULT_CONFIG)
        return _unloaded_defaults()
    # A deep copy: the nested sections are changed in place later (environment
    # overrides, a storage switch), which must not rewrite the defaults themselves.
    return copy.deepcopy(constants.DEFAULT_CONFIG)


_BOMS = ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))


def _config_text(data: bytes) -> str:
    """config.json's text: UTF-8, or UTF-8 / UTF-16 with a byte-order mark (as some
    editors on Windows save it). Raises UnicodeDecodeError for anything else."""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            return data.decode(enc)
    return data.decode("utf-8")


_AUTH_KEY = re.compile(r'"\s*auth\s*"\s*:', re.IGNORECASE)


def _mentions_signin(path) -> bool:
    """Whether a config.json that could not be parsed may have configured sign-in: its
    text — decoded as UTF-8, by its byte-order mark, and as UTF-16 when it holds NUL
    bytes — names an "auth" key in any letter case, or its bytes cannot be read. Only
    then must the app stay closed."""
    try:
        data = path.read_bytes()
    except OSError:
        return True
    texts = []
    for bom, enc in _BOMS:
        if data.startswith(bom):
            texts.append(data.decode(enc, errors="replace"))
    texts.append(data.decode("utf-8", errors="replace"))
    if b"\x00" in data:
        texts += [data.decode("utf-16-le", errors="replace"), data.decode("utf-16-be", errors="replace")]
    return any(_AUTH_KEY.search(t) for t in texts)


def _kept_copies() -> list:
    """The config.json.corrupt-* copies beside config.json, oldest first."""
    return sorted(constants.CONFIG_PATH.parent.glob(f"{constants.CONFIG_PATH.name}.corrupt-*"))


def _unloaded_defaults() -> dict:
    """The defaults used when config.json exists but could not be loaded. Whether sign-in
    was on cannot be known, so the auth section says so: every request is refused (503)
    unless VISUALWEAVER_AUTH states the mode (auth.parse_settings), and a save in that
    state writes the same undecided section instead of an "off" nobody chose."""
    from . import auth
    cfg = copy.deepcopy(constants.DEFAULT_CONFIG)
    cfg["auth"] = {"mode": auth.UNREADABLE_MODE}
    return cfg


# Every config write is numbered when its copy is taken and lands in that order: a write
# that reaches the disk after a NEWER one is dropped instead of published over it. Saves
# run in threads now (save_config_async, and the sync routes FastAPI runs in its pool),
# so without this a slow older copy could bring back, say, a connection just deleted.
_config_writes = {"issued": 0, "written": 0}
_config_seq_lock = threading.Lock()     # held only to take a number — safe on the loop
_config_write_lock = threading.Lock()   # held for the write itself — never on the loop
_config_write_seq = threading.local()   # the number save_config_async took for its copy


def _next_config_seq() -> int:
    with _config_seq_lock:
        _config_writes["issued"] += 1
        return _config_writes["issued"]


def save_config(cfg: dict):
    """
    Persist config to disk.
    API keys that were loaded from environment variables are always stripped
    before writing so they never end up on disk.

    Blocking (an fsync'd write): async code calls save_config_async instead.
    """
    seq = getattr(_config_write_seq, "seq", None) or _next_config_seq()
    to_save = copy.deepcopy(cfg)
    if state._env_key and to_save.get("claude", {}).get("api_key") == state._env_key:
        to_save.setdefault("claude", {})["api_key"] = ""
    if state._openai_env_key and to_save.get("openai", {}).get("api_key") == state._openai_env_key:
        to_save.setdefault("openai", {})["api_key"] = ""
    if state._qwen_env_key and to_save.get("qwen", {}).get("api_key") == state._qwen_env_key:
        to_save.setdefault("qwen", {})["api_key"] = ""
    if state._mistral_env_key and to_save.get("mistral", {}).get("api_key") == state._mistral_env_key:
        to_save.setdefault("mistral", {})["api_key"] = ""
    if state._groq_env_key and to_save.get("groq", {}).get("api_key") == state._groq_env_key:
        to_save.setdefault("groq", {})["api_key"] = ""
    if state._gemini_env_key and to_save.get("gemini", {}).get("api_key") == state._gemini_env_key:
        to_save.setdefault("gemini", {})["api_key"] = ""
    # VISUALWEAVER_OLLAMA_URL applies to the run only: while the setting still holds the
    # address it set, the file keeps the shipped default.
    _o = to_save.get("ollama")
    if state._ollama_env_url and isinstance(_o, dict) and \
            f"{_o.get('host')}:{_o.get('port')}" == state._ollama_env_url:
        to_save["ollama"] = {**_o, "host": constants.DEFAULT_CONFIG["ollama"]["host"],
                             "port": constants.DEFAULT_CONFIG["ollama"]["port"]}
    # Storage set by VISUALWEAVER_STORAGE / VISUALWEAVER_MONGODB_* applies to the run,
    # not to the file: what config.json held before is written back.
    ov = state._storage_env_overlay
    if ov:
        if isinstance(to_save.get("storage"), dict):
            if "default_type" in ov:
                if ov["default_type"] is None:
                    to_save["storage"].pop("default_type", None)
                else:
                    to_save["storage"]["default_type"] = ov["default_type"]
        if "mongodb" in ov:
            if ov["mongodb"] is None:
                to_save.pop("mongodb", None)
            else:
                to_save["mongodb"] = copy.deepcopy(ov["mongodb"])
        if "endpoint" in ov and isinstance(to_save.get("redis_endpoints"), list):
            name, before = ov["endpoint"]
            eps = [e for e in to_save["redis_endpoints"] if not (isinstance(e, dict) and e.get("name") == name)]
            if before is not None:
                eps.append(copy.deepcopy(before))
            to_save["redis_endpoints"] = eps
    # VISUALWEAVER_MONGODB_PASSWORD: the built-in MongoDB (or the "local-mongodb"
    # connection) got it from the environment, so it stays there.
    if state._mongodb_env_password:
        if (to_save.get("mongodb") or {}).get("password") == state._mongodb_env_password:
            to_save["mongodb"]["password"] = ""
        for ep in to_save.get("redis_endpoints") or []:
            if (isinstance(ep, dict) and ep.get("builtin")
                    and ep.get("password") == state._mongodb_env_password):
                ep["password"] = ""
    # Atomic write: serialise fully, fsync, then rename over the target. A plain
    # open(path,"w") truncates first, so a crash / full disk / OOM mid-write leaves
    # a half-written file that parses as garbage on the next boot (see load_config).
    # A FIXED temp name lets two processes sharing DATA_DIR interleave writes into
    # the same file, and os.replace then publishes the mixture — the exact
    # corruption the atomic write exists to prevent.
    _fd, _tmp_name = tempfile.mkstemp(dir=str(constants.CONFIG_PATH.parent),
                                      prefix=f".{constants.CONFIG_PATH.name}.", suffix=".tmp")
    os.close(_fd)
    tmp = Path(_tmp_name)
    _config_write_lock.acquire()
    try:
        if seq < _config_writes["written"]:
            tmp.unlink(missing_ok=True)   # a newer copy is already on disk
            return
        with open(tmp, "w") as f:
            json.dump(to_save, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, constants.CONFIG_PATH)      # atomic on POSIX and Windows
        _config_writes["written"] = seq
        # A good file is on disk now, so /api/health must stop reporting "degraded".
        state._config_load_error = ""
    except Exception:
        try:
            tmp.unlink(missing_ok=True)   # never leave a stray temp behind
        except Exception:
            pass
        raise
    finally:
        _config_write_lock.release()


async def save_config_async(cfg: dict) -> None:
    """save_config, without blocking the event loop (it fsyncs; on a slow or network
    disk that is tens of milliseconds every other session waits through).

    The copy is taken here, on the loop, so a request that changes the config while the
    write is in flight cannot be caught half-copied; it is numbered here too, so it is
    ordered by when it was asked for, not by which thread reaches the disk first.
    """
    snap = copy.deepcopy(cfg)
    seq = _next_config_seq()

    def _write():
        _config_write_seq.seq = seq
        try:
            save_config(snap)     # the module attribute, so a test's stand-in applies
        finally:
            _config_write_seq.seq = None

    await asyncio.to_thread(_write)


def _write_json_atomic(path: Path, obj) -> None:
    """Write JSON to ``path`` the way save_config does: a unique temp file beside it,
    fsync, then rename over the target — so a crash or a full disk mid-write leaves the
    previous file intact instead of a truncated one."""
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            # mkstemp makes the file 0600; keep the mode the file had (0644 when new).
            if hasattr(os, "fchmod"):                       # POSIX
                try:
                    os.fchmod(f.fileno(), path.stat().st_mode & 0o777)
                except FileNotFoundError:
                    os.fchmod(f.fileno(), 0o644)
            f.write(json.dumps(obj))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── Provider SDK client reuse ────────────────────────────────────────────────
# A fresh SDK client per request means a fresh connection pool and a fresh TLS
# handshake on every turn (~30 ms per cloud provider, measured) — and a first-turn
# conversation pays it three times (answer + title + HyDE). Cache by credentials so
# a key change still creates a new client.
_provider_clients: dict = {}

def _cached_client(kind: str, api_key: str, base_url: str):
    """Return a pooled SDK client for (kind, key, base_url), creating it on first use.

    ``kind`` is "anthropic", "openai" (every OpenAI-compatible provider) or "gemini"
    (google-genai; it has no base URL setting, so pass ""). A google-genai Client builds
    two HTTP clients and their SSL contexts — ~25 ms — which gemini_stream, the model
    list and the key check each paid on every call.
    """
    ck = (kind, api_key, base_url)
    c = _provider_clients.get(ck)
    if c is None:
        if kind == "anthropic":
            c = _anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
        elif kind == "gemini":
            from . import providers   # late: providers imports this module
            c = providers._google_genai.Client(api_key=api_key)
        else:
            c = _AsyncOpenAI(api_key=api_key, base_url=base_url)
        _provider_clients[ck] = c
    return c

def invalidate_provider_clients():
    """Drop cached clients (called on config save, since keys may have changed)."""
    _provider_clients.clear()


# Words the prompt enumerates as a comma list after a colon — "…all you need to
# get right: mandelbrot, julia, fern, …", "Allowed types: point, line, segment,
# …". They are capability NAMES the model must reproduce exactly, and they are
# invisible to both a fence scan and a JSON-key scan.
#
# Parentheticals are dropped first so "koch (Koch snowflake)" contributes `koch`
# and not `snowflake`. A run of at least four is required: shorter comma lists in
# ordinary prose are not vocabulary.
_ENUM_STOPWORDS = frozenset({"and", "the", "for", "with", "see", "use", "only",
                             "not", "are", "its", "one", "per", "any", "also"})


def _enumerated_names(text: str) -> set:
    import re

    flat = re.sub(r"\([^)]*\)", " ", text)
    out = set()
    for m in re.finditer(r":\s*((?:[a-z][a-z0-9-]{2,}\s*,\s*){3,}[a-z][a-z0-9-]{2,})", flat):
        out |= {w for w in re.findall(r"\b([a-z][a-z0-9-]{2,})\b", m.group(1))
                if w not in _ENUM_STOPWORDS}
    return out


# How many missing options / names the drift notice lists before "+N more".
_DRIFT_LIST_CAP = 50


def base_instruction_drift(stored: str | None = None) -> dict:
    """What the SHIPPED Base Instruction offers that a saved copy does not.

    A saved Base Instruction replaces the shipped one outright and is never
    revisited, so from the first time Settings is saved the model stops hearing
    about anything added since — new blocks, new options. That is silent: the
    answer still renders, it just never uses the block it should, or names an
    option that no longer exists.

    Reports what is MISSING, never merely what is different. Customising this
    field is its documented purpose, and a notice that fires on any edit — then
    recommends the button that discards it — is worse than no notice at all.
    `stale` is the flag worth acting on; `differs` is descriptive only.
    """
    import re

    shipped = constants.DEFAULT_BASE_INSTRUCTION
    if stored is None:
        stored = state._config.get("base_instruction")
    # A hand-edited config can hold anything. This runs inside GET /api/config,
    # the route the whole UI bootstraps from, so a TypeError here would take the
    # settings screen down rather than one chat turn.
    if not isinstance(stored, str):
        stored = ""
    stored = stored.strip()

    # With the visual section switched off, everything after the marker is cut
    # before sending — so it is not missing, it is declined. Compare like for like.
    if not state._config.get("visual_instructions", True):
        marker = getattr(constants, "VISUAL_SECTION_MARKER", None)
        if marker:
            # BOTH sides: compose_system_prompt cuts the STORED instruction at
            # this marker before sending, so the visual section is not part of
            # what either one contributes. Truncating only the shipped side made
            # an untouched default read as different from itself.
            if marker in shipped:
                shipped = shipped[:shipped.index(marker)]
            if marker in stored:
                stored = stored[:stored.index(marker)]

    # Compare like with like: `shipped` may have been truncated at the visual
    # marker above, and measuring a stripped stored copy against an unstripped
    # shipped one made `differs` true for an untouched default and put the
    # trailing whitespace into the "N characters missing" figure.
    shipped, stored = shipped.strip(), stored.strip()
    empty = {"differs": False, "stale": False, "missing_lanes": [],
             "missing_options": [], "missing_options_total": 0,
             "missing_names": [], "missing_names_total": 0,
             "stored_chars": len(stored), "shipped_chars": len(shipped)}
    if not stored or stored == shipped.strip():
        return empty

    # Fenced blocks, filtered through the list of things that actually render —
    # the prose also contains "```language", the placeholder for an ordinary code
    # fence, and naming that as a missing capability invents one.
    lanes = [f for f in re.findall(r"```([a-z][a-z0-9]*)", shipped)
             if f in constants.RENDERABLE_FENCES]
    missing_lanes = sorted({l for l in dict.fromkeys(lanes) if f"```{l}" not in stored})

    # JSON KEYS the model has to write — `"order":`, `"plane":`, `"c":`. Matching
    # any quoted word instead swept up the example VALUES ("bar", "api", "svc",
    # "indefinite"), which are not options and made the notice meaningless.
    keys = re.findall(r'"([a-z][a-z0-9_]*)"\s*:', shipped)
    missing = sorted({k for k in dict.fromkeys(keys) if f'"{k}"' not in stored})

    # Enumerated capability names — the fractal presets, the geometry element
    # types. These are the words the model has to reproduce exactly, and the
    # earlier version could not see them at all: deleting all 21 preset names
    # from a stored copy left `stale` False, which is precisely the incident this
    # whole feature was built for.
    # Presence in the stored TEXT, the way missing_lanes and missing_options
    # are tested. Comparing two parsed enumerations meant the saved copy had
    # to reproduce the comma-list FORM: swapping one ", " for "; ", or
    # re-wrapping the list one name per line, reported all 21 preset names as
    # missing while every one of them was still there — a permanent warning
    # above the button that discards the wording.
    missing_names = sorted(
        w for w in _enumerated_names(shipped)
        if not re.search(rf"\b{re.escape(w)}\b", stored))

    return {"differs": True,
            "stale": bool(missing_lanes or missing or missing_names),
            "missing_lanes": missing_lanes,
            # The notice lists up to _DRIFT_LIST_CAP of each; the *_total fields carry
            # the true count so a longer list still says "+N more" rather than looking complete.
            "missing_options": missing[:_DRIFT_LIST_CAP],
            "missing_options_total": len(missing),
            "missing_names": missing_names[:_DRIFT_LIST_CAP],
            "missing_names_total": len(missing_names),
            "stored_chars": len(stored),
            "shipped_chars": len(shipped)}


def _visual_lane_supplement(base: str) -> str:
    """Instructions for shipped visual blocks a saved base_instruction never mentions.

    A base_instruction saved before a lane existed shadows it forever (see
    base_instruction_drift): the model is never told the fence exists, so it draws
    the subject with whatever it already knows — a circuit as a mermaid graph, a
    3-D scene as a plot. Rather than depend on a manual "Reset to shipped default",
    the missing lanes' OWN bullets (and the routing rule) from the shipped default
    are appended to what the model receives each turn. The user's customised prose
    is untouched — only capability the app can actually render is added back.
    Returns "" when the base already mentions every lane, or when visual blocks are
    switched off for this deployment (the whole section is cut before sending, so
    adding one back would contradict that).
    """
    if not state._config.get("visual_instructions", True):
        return ""
    try:
        missing = base_instruction_drift(base).get("missing_lanes") or []
    except Exception:
        return ""
    if not missing:
        return ""
    lines = constants.DEFAULT_BASE_INSTRUCTION.split("\n")
    bullets = []
    for lane in missing:
        for ln in lines:
            if ln.startswith(f"- ```{lane} \u2014") or ln.startswith(f"- ```{lane} -"):
                bullets.append(ln)
                break
    if not bullets:
        return ""
    routing = next((ln for ln in lines if ln.strip().startswith("Routing \u2014")), "")
    header = ("=== ALSO AVAILABLE \u2014 visual blocks your saved instruction predates; "
              "use these too, and the routing rule below ===")
    return "\n".join([header] + ([routing] if routing else []) + bullets)


# The composed base of the system prompt, keyed on everything it is computed from. A
# saved instruction that differs from the shipped one is diffed against it on every
# composition (_visual_lane_supplement → base_instruction_drift: a dozen regex passes
# over ~20 KB) — 4.4 ms of event-loop time per chat turn, for a result that changes only
# when Settings is saved or the app is upgraded. Small and bounded: a handful of
# distinct keys exist over a process's life.
_composed_base: dict = {}
_COMPOSED_BASE_MAX = 16


def compose_system_prompt(client_system: str | None) -> str:
    """
    Build the effective system prompt for a chat turn.

    The global base_instruction (Settings -> Templates -> Base Instruction) is
    always prepended; a selected template's system prompt is added on top of it
    (templates are additive to the base). If both are empty we fall back to a
    plain default so the model still gets a system message.

    The base part is memoised (see _composed_base) on every input it depends on —
    the stored instruction (its type too, so 1 and True stay apart), the visual-blocks
    flag, the shipped instruction, its section marker, the renderable fences and the
    two functions that diff them — so a change to any of them is a new key, never a
    stale prompt.
    """
    _bi = state._config.get("base_instruction")
    visual = bool(state._config.get("visual_instructions", True))
    key = (type(_bi), _bi, visual, constants.DEFAULT_BASE_INSTRUCTION,
           getattr(constants, "VISUAL_SECTION_MARKER", None), constants.RENDERABLE_FENCES,
           _visual_lane_supplement, base_instruction_drift)
    try:
        base = _composed_base.get(key)
    except TypeError:            # an unhashable value (a hand-edited list): just compute it
        key, base = None, None
    if base is None:
        base = _compose_base(_bi, visual)
        if key is not None:
            if len(_composed_base) >= _COMPOSED_BASE_MAX:
                _composed_base.clear()
            _composed_base[key] = base
    client = (client_system or "").strip()
    parts  = [p for p in (base, client) if p]
    return "\n\n".join(parts) if parts else "You are a helpful assistant."


def _compose_base(_bi, visual: bool) -> str:
    """The base-instruction part of compose_system_prompt (uncached)."""
    # isinstance, not `or ""`. Hardening only the config route moved the
    # symptom from "Settings will not open" to "the app answers nothing",
    # which is worse: this runs on every chat turn.
    base   = (_bi if isinstance(_bi, str) else "").strip()
    # Drop the large chart/diagram authoring section on text-only deployments
    # (config visual_instructions=False) to save ~2k billed tokens per turn. The
    # SHIPPED instruction's visual section is a trailing block beginning at
    # VISUAL_SECTION_MARKER, so cutting there keeps the core answer-style guidance.
    # Limitation: a customised base instruction that appends its own text AFTER that
    # marker loses it when gated off — the marker is treated as the start of a suffix.
    if not visual:
        cut = base.find(constants.VISUAL_SECTION_MARKER)
        if cut != -1:
            base = base[:cut].rstrip()
    else:
        # Fill in any shipped visual block the saved instruction predates, so a
        # circuit is drawn as ```circuit even when the stored copy never heard of it.
        supp = _visual_lane_supplement(base)
        if supp:
            base = (base + "\n\n" + supp).strip() if base else supp
    return base


# ── Secret redaction ──────────────────────────────────────────────────────────
# Config sent to the browser must never carry provider keys or passwords. Each
# stored secret is swapped for a sentinel on the way out and restored from the
# stored value on the way back in, so the UI can round-trip settings without
# ever seeing a secret — and a blank field still clears a key (blank != sentinel).

_SECRET_SENTINEL = "__VISUALWEAVER_SECRET_KEPT__"
_PROVIDER_SECRET_KEYS = ("claude", "openai", "qwen", "mistral", "groq", "gemini")


def _redact_secrets(cfg: dict) -> dict:
    """Deep copy of cfg with every set secret replaced by _SECRET_SENTINEL."""
    red = copy.deepcopy(cfg)
    for p in _PROVIDER_SECRET_KEYS:
        if isinstance(red.get(p), dict) and red[p].get("api_key"):
            red[p]["api_key"] = _SECRET_SENTINEL
    if isinstance(red.get("redis"), dict) and red["redis"].get("password"):
        red["redis"]["password"] = _SECRET_SENTINEL
    if isinstance(red.get("mongodb"), dict) and red["mongodb"].get("password"):
        red["mongodb"]["password"] = _SECRET_SENTINEL
    if isinstance(red.get("security"), dict) and red["security"].get("password"):
        red["security"]["password"] = _SECRET_SENTINEL
    # isinstance, not `or []`: a non-list here (5, or a bare string) either
    # raised TypeError or iterated characters, out of GET /api/config.
    _eps = red.get("redis_endpoints")
    for ep in (_eps if isinstance(_eps, list) else []):
        if not isinstance(ep, dict):
            continue
        if isinstance(ep, dict) and ep.get("password"):
            ep["password"] = _SECRET_SENTINEL
    return red


def _unredact_secrets(new_cfg: dict, old_cfg: dict) -> None:
    """In place: swap any sentinel secret in new_cfg back to the stored value."""
    for p in _PROVIDER_SECRET_KEYS:
        if isinstance(new_cfg.get(p), dict) and new_cfg[p].get("api_key") == _SECRET_SENTINEL:
            new_cfg[p]["api_key"] = (old_cfg.get(p) or {}).get("api_key", "")
    if isinstance(new_cfg.get("redis"), dict) and new_cfg["redis"].get("password") == _SECRET_SENTINEL:
        new_cfg["redis"]["password"] = (old_cfg.get("redis") or {}).get("password", "")
    if isinstance(new_cfg.get("mongodb"), dict) and new_cfg["mongodb"].get("password") == _SECRET_SENTINEL:
        new_cfg["mongodb"]["password"] = (old_cfg.get("mongodb") or {}).get("password", "")
    if isinstance(new_cfg.get("security"), dict) and new_cfg["security"].get("password") == _SECRET_SENTINEL:
        new_cfg["security"]["password"] = (old_cfg.get("security") or {}).get("password", "")
    old_eps = {e.get("name"): e for e in (old_cfg.get("redis_endpoints") or []) if isinstance(e, dict)}
    for ep in new_cfg.get("redis_endpoints", []) or []:
        if isinstance(ep, dict) and ep.get("password") == _SECRET_SENTINEL:
            ep["password"] = (old_eps.get(ep.get("name")) or {}).get("password", "")


# ── Installed storage ─────────────────────────────────────────────────────────
# ``storage`` (what the built-in "default" connection is, and which connection holds
# the app data), the built-in MongoDB (``mongodb``) and the built-in entries of
# ``redis_endpoints`` describe what was installed on this machine. They change only
# through the installer and POST /api/storage/app, never through a settings save or
# a config import.

# "auth" (sign-in, see auth.py) is set in config.json or the environment only: a settings
# save or an imported file must never be able to turn sign-in off or name a new admin.
_INSTALLED_KEYS = ("storage", "mongodb", "auth")


# sessions.ttl until 1.16: the shipped default, with no control in Settings.
LEGACY_SESSION_TTL = 86400
# The shortest conversation retention accepted anywhere (0 = forever is always accepted):
# Settings, a config import and config.json itself. sessions.MIN_RETENTION is this value.
MIN_SESSION_TTL = 3600


def session_ttl_error(t) -> str:
    """Why ``t`` cannot be a conversation retention (sessions.ttl), or "" when it can."""
    if isinstance(t, bool) or not isinstance(t, int) or t < 0:
        return "sessions.ttl must be a whole number of seconds (0 = forever)"
    if 0 < t < MIN_SESSION_TTL:
        return f"sessions.ttl must be 0 (forever) or at least {MIN_SESSION_TTL} seconds (one hour)"
    return ""


def normalise_session_retention(file_cfg: dict, merged: dict) -> None:
    """Read the old one-day default as the current one (kept forever), and a retention
    below MIN_SESSION_TTL as MIN_SESSION_TTL.

    Every config.json an earlier version saved holds sessions.ttl 86400 — the whole
    config is written on each save — so that value says nothing about a choice. A
    retention chosen in Settings carries retention_chosen and is kept as it is, as is
    any other number (only a hand edit could have set one)."""
    raw = file_cfg.get("sessions") if isinstance(file_cfg, dict) else None
    if (isinstance(raw, dict) and raw.get("ttl") == LEGACY_SESSION_TTL
            and not raw.get("retention_chosen") and isinstance(merged.get("sessions"), dict)):
        merged["sessions"] = {**merged["sessions"], "ttl": 0}
    # A retention below the floor (only a hand edit can write one) is read as the floor:
    # one second would give every conversation a one-second expiry.
    sess = merged.get("sessions")
    if isinstance(sess, dict):
        t = sess.get("ttl")
        if isinstance(t, int) and not isinstance(t, bool) and 0 < t < MIN_SESSION_TTL:
            log.warning(f"config.json: sessions.ttl {t} is below the {MIN_SESSION_TTL} s minimum; "
                        f"using {MIN_SESSION_TTL} s (one hour)")
            merged["sessions"] = {**sess, "ttl": MIN_SESSION_TTL}


def normalise_mongo_credentials(cfg: dict) -> dict:
    """In place: move a user:password pasted into a MongoDB connection string into the
    username/password fields, which are redacted for the browser and exports. A URI
    edited into config.json by hand, or imported, would otherwise carry its password
    to every client that reads the config."""
    from . import mongo_store
    targets = [cfg.get("mongodb")] + list(cfg.get("redis_endpoints") or [])
    for c in targets:
        if not isinstance(c, dict) or (c is not cfg.get("mongodb") and c.get("type") != "mongodb"):
            continue
        uri = c.get("uri")
        if not isinstance(uri, str) or "@" not in uri:
            continue
        clean, user, pw = mongo_store.split_uri_credentials(uri)
        if clean != uri:
            c["uri"] = clean
            if user and not c.get("username"):
                c["username"] = user
            if pw and not c.get("password"):
                c["password"] = pw
    return cfg


def keep_installed_storage(new_cfg: dict, old_cfg: dict) -> None:
    """In place: give new_cfg the installed-storage sections of old_cfg.

    Built-in connections are kept exactly as they are — on a Redis-first install that
    includes ``redis`` itself, which IS the built-in store (an imported config from
    another machine would otherwise move every conversation, the answer cache and the
    default instances onto its Redis). The connection holding the app data is kept too:
    edits to it are accepted, but it cannot be dropped or change type (the app data
    would silently land on another store)."""
    keys = list(_INSTALLED_KEYS)
    st_old = old_cfg.get("storage") if isinstance(old_cfg.get("storage"), dict) else {}
    if st_old.get("default_type") != "mongodb" and isinstance(old_cfg.get("redis"), dict):
        keys.append("redis")
    for k in keys:
        if k in old_cfg:
            new_cfg[k] = copy.deepcopy(old_cfg[k])
        else:
            new_cfg.pop(k, None)
    normalise_mongo_credentials(new_cfg)
    eps = new_cfg.get("redis_endpoints")
    if not isinstance(eps, list):
        return
    old_eps = [e for e in (old_cfg.get("redis_endpoints") if isinstance(old_cfg.get("redis_endpoints"), list)
                           else []) if isinstance(e, dict) and e.get("name")]
    builtin = {e["name"]: e for e in old_eps if e.get("builtin")}
    st = old_cfg.get("storage") if isinstance(old_cfg.get("storage"), dict) else {}
    app = st.get("app") or "default"
    app_ep = next((e for e in old_eps if e["name"] == app and not e.get("builtin")), None)
    out = []
    for e in eps:
        if not isinstance(e, dict):
            out.append(e)
            continue
        if e.get("name") in builtin:
            out.append(copy.deepcopy(builtin.pop(e["name"])))
            continue
        e = {k: v for k, v in e.items() if k != "builtin"}
        if app_ep is not None and e.get("name") == app:
            if (e.get("type") or "redis") != (app_ep.get("type") or "redis"):
                e = copy.deepcopy(app_ep)
            app_ep = None
        out.append(e)
    out += [copy.deepcopy(e) for e in builtin.values()]   # dropped by the payload: kept
    if app_ep is not None:
        out.append(copy.deepcopy(app_ep))
    new_cfg["redis_endpoints"] = out


# Retention caps for the two append-only JSON stores. Both are held in memory
# and rewritten whole, so the in-memory list must be trimmed too — otherwise it
# grows without bound while only the tail is ever persisted.
_MAX_LOGS     = 500
_MAX_FEEDBACK = 2000
# Per-field cap on a feedback POST. The store is rewritten in full on every
# rating, so one oversized body permanently inflates the cost of every later one.
_MAX_FEEDBACK_FIELD = 8000

# Largest single upload accepted. api_ingest_files reads the whole body into
# memory before touching disk, so without a bound one file can exhaust RAM and
# then the volume. Override with VISUALWEAVER_MAX_UPLOAD_MB.
_MAX_UPLOAD_BYTES = int(os.environ.get("VISUALWEAVER_MAX_UPLOAD_MB", "100")) * 1024 * 1024


def load_logs():
    """Load persisted ingestion log from disk into memory."""
    if constants.LOGS_PATH.exists():
        try:
            with open(constants.LOGS_PATH) as f:
                state._ingestion_logs = json.load(f)[-_MAX_LOGS:]
        except Exception as e:
            log.warning(f"Could not read {constants.LOGS_PATH.name} ({e}) — starting with an empty log")


def load_feedback():
    """Load persisted feedback from disk into memory.

    Without this the module-level ``_feedback`` list starts empty on every boot
    and ``api_feedback`` — which rewrites the whole file — truncates the store to
    a single entry on the first rating after a restart.
    """
    if constants.FEEDBACK_PATH.exists():
        try:
            with open(constants.FEEDBACK_PATH) as f:
                state._feedback = json.load(f)[-_MAX_FEEDBACK:]
        except Exception as e:
            log.warning(f"Could not read {constants.FEEDBACK_PATH.name} ({e}) — starting with empty feedback")


_logs_lock = threading.Lock()
_feedback_lock = threading.Lock()


def append_log(entry: dict):
    """Append an ingestion event and keep the last _MAX_LOGS entries.

    Blocking — it rewrites the whole file — so async callers run it in a thread.
    """
    append_logs([entry])


def append_logs(entries: list[dict]):
    """append_log for several events with ONE rewrite of the file (a crawl's batch).

    Serialised: crawl pages, file ingests and resets log from several threads at once,
    and two writers interleaving in one open(..., "w") leave a corrupt file.
    """
    with _logs_lock:
        state._ingestion_logs.extend(entries)
        del state._ingestion_logs[:-_MAX_LOGS]      # trim in memory, not just on disk
        data = json.dumps(state._ingestion_logs)    # the C encoder; json.dump streams in Python
        with open(constants.LOGS_PATH, "w") as f:
            f.write(data)


def save_feedback() -> None:
    """Persist the feedback store — atomically, and off the event loop (call it in a
    thread). It is rewritten whole on every rating, and a plain open(path, "w")
    truncated it first: a crash mid-write lost every rating ever given. Serialised, and
    each write takes the list as it is at that moment, so the last write always holds
    every rating appended before it."""
    with _feedback_lock:
        _write_json_atomic(constants.FEEDBACK_PATH, list(state._feedback))

