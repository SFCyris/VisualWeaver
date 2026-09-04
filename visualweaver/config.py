# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.config — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import copy
import json
import os
import tempfile
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
    if constants.CONFIG_PATH.exists():
        try:
            with open(constants.CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            if not isinstance(cfg, dict):
                raise ValueError(f"expected a JSON object, got {type(cfg).__name__}")
            merged = {**constants.DEFAULT_CONFIG, **cfg}
            # Shallow-merge each nested dict so we pick up new sub-keys
            for k, v in constants.DEFAULT_CONFIG.items():
                if isinstance(v, dict):
                    merged[k] = {**v, **cfg.get(k, {})}
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
            except Exception:
                kept = " — could NOT be preserved"
            log.error("=" * 70)
            log.error(f"config.json could not be parsed ({state._config_load_error}){kept}.")
            log.error("Starting from built-in DEFAULTS — API keys and your Redis endpoint")
            log.error(f"are NOT the ones you configured (defaults point at "
                      f"{constants.DEFAULT_CONFIG['redis']['host']}:{constants.DEFAULT_CONFIG['redis']['port']}).")
            log.error("Restore the quarantined file or re-enter settings before ingesting.")
            log.error("=" * 70)
    return dict(constants.DEFAULT_CONFIG)


def save_config(cfg: dict):
    """
    Persist config to disk.
    API keys that were loaded from environment variables are always stripped
    before writing so they never end up on disk.
    """
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
    try:
        with open(tmp, "w") as f:
            json.dump(to_save, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, constants.CONFIG_PATH)      # atomic on POSIX and Windows
        # A good file is on disk now, so /api/health must stop reporting "degraded".
        state._config_load_error = ""
    except Exception:
        try:
            tmp.unlink(missing_ok=True)   # never leave a stray temp behind
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
    """Return a pooled SDK client for (kind, key, base_url), creating it on first use."""
    ck = (kind, api_key, base_url)
    c = _provider_clients.get(ck)
    if c is None:
        c = (_anthropic.AsyncAnthropic(api_key=api_key, base_url=base_url)
             if kind == "anthropic" else
             _AsyncOpenAI(api_key=api_key, base_url=base_url))
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


def compose_system_prompt(client_system: str | None) -> str:
    """
    Build the effective system prompt for a chat turn.

    The global base_instruction (Settings -> Templates -> Base Instruction) is
    always prepended; a selected template's system prompt is added on top of it
    (templates are additive to the base). If both are empty we fall back to a
    plain default so the model still gets a system message.
    """
    # isinstance, not `or ""`. Hardening only the config route moved the
    # symptom from "Settings will not open" to "the app answers nothing",
    # which is worse: this runs on every chat turn.
    _bi = state._config.get("base_instruction")
    base   = (_bi if isinstance(_bi, str) else "").strip()
    # Drop the large chart/diagram authoring section on text-only deployments
    # (config visual_instructions=False) to save ~2k billed tokens per turn. The
    # SHIPPED instruction's visual section is a trailing block beginning at
    # VISUAL_SECTION_MARKER, so cutting there keeps the core answer-style guidance.
    # Limitation: a customised base instruction that appends its own text AFTER that
    # marker loses it when gated off — the marker is treated as the start of a suffix.
    if not state._config.get("visual_instructions", True):
        cut = base.find(constants.VISUAL_SECTION_MARKER)
        if cut != -1:
            base = base[:cut].rstrip()
    else:
        # Fill in any shipped visual block the saved instruction predates, so a
        # circuit is drawn as ```circuit even when the stored copy never heard of it.
        supp = _visual_lane_supplement(base)
        if supp:
            base = (base + "\n\n" + supp).strip() if base else supp
    client = (client_system or "").strip()
    parts  = [p for p in (base, client) if p]
    return "\n\n".join(parts) if parts else "You are a helpful assistant."


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
    if isinstance(new_cfg.get("security"), dict) and new_cfg["security"].get("password") == _SECRET_SENTINEL:
        new_cfg["security"]["password"] = (old_cfg.get("security") or {}).get("password", "")
    old_eps = {e.get("name"): e for e in (old_cfg.get("redis_endpoints") or []) if isinstance(e, dict)}
    for ep in new_cfg.get("redis_endpoints", []) or []:
        if isinstance(ep, dict) and ep.get("password") == _SECRET_SENTINEL:
            ep["password"] = (old_eps.get(ep.get("name")) or {}).get("password", "")


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


def append_log(entry: dict):
    """Append an ingestion event and keep the last _MAX_LOGS entries."""
    state._ingestion_logs.append(entry)
    del state._ingestion_logs[:-_MAX_LOGS]          # trim in memory, not just on disk
    with open(constants.LOGS_PATH, "w") as f:
        json.dump(state._ingestion_logs, f)

