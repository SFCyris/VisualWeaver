# SPDX-License-Identifier: AGPL-3.0-or-later
"""visualweaver.startup — extracted from main.py (see main.py for architecture).

Split out mechanically for maintainability; cross-module references are
module-qualified so runtime rebinding and test monkeypatching stay live.
"""
import logging
import asyncio
import copy
import os
import platform
import socket
from . import appcore, auth, cache, config, constants, embeddings, mongo_store, pagetext, rag, rag_admin, redis_store, sessions, state, ws

log = logging.getLogger(__name__)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STARTUP
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _primary_lan_ip() -> str | None:
    """Best-effort primary LAN IPv4 (the interface used for outbound traffic).
    Uses a UDP socket's chosen source address — no packet is actually sent."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        return ip if not ip.startswith("127.") else None
    except Exception:
        return None
    finally:
        s.close()


_STORAGE_CHOICES = ("redis", "mongodb", "both")
LOCAL_MONGODB = "local-mongodb"


def _mongodb_from_env() -> dict | None:
    """The built-in MongoDB connection from VISUALWEAVER_MONGODB_URI / _USERNAME /
    _PASSWORD / _DATABASE, or None when no usable URI is set. Credentials inside the URI
    are moved to username/password, as a connection saved in Settings has them."""
    raw = os.environ.get("VISUALWEAVER_MONGODB_URI", "").strip()
    if not raw:
        return None
    uri, u_user, u_pw = mongo_store.split_uri_credentials(mongo_store.server_uri(raw))
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        log.warning("  VISUALWEAVER_MONGODB_URI is not a MongoDB server or connection string — ignored")
        return None
    password = os.environ.get("VISUALWEAVER_MONGODB_PASSWORD", "") or u_pw
    if mongo_store.is_password_placeholder(password):
        log.warning("  VISUALWEAVER_MONGODB_URI still holds the <db_password> placeholder; set "
                    "VISUALWEAVER_MONGODB_PASSWORD — ignored")
        return None
    database = os.environ.get("VISUALWEAVER_MONGODB_DATABASE", "").strip() or mongo_store.DEFAULT_DB
    if not mongo_store.valid_database_name(database):
        log.warning(f"  VISUALWEAVER_MONGODB_DATABASE={database!r} is not a valid database name "
                    f"— using {mongo_store.DEFAULT_DB!r}")
        database = mongo_store.DEFAULT_DB
    state._mongodb_env_password = password
    return {"type": "mongodb", "uri": uri,
            "username": os.environ.get("VISUALWEAVER_MONGODB_USERNAME", "").strip() or u_user,
            "password": password, "database": database, "builtin": True}


def apply_ollama_env() -> None:
    """VISUALWEAVER_OLLAMA_URL (e.g. http://host.docker.internal:11434): where Ollama is
    when the app runs in a container, where "localhost" is the container itself. It
    applies only while the Ollama setting is still the shipped default — an address
    chosen in Settings always wins — and is never written to config.json."""
    url = os.environ.get("VISUALWEAVER_OLLAMA_URL", "").strip()
    if not url:
        return
    from urllib.parse import urlsplit
    o = state._config.setdefault("ollama", {})
    default = constants.DEFAULT_CONFIG["ollama"]
    if (o.get("host"), int(o.get("port") or 0)) != (default["host"], default["port"]):
        log.info(f"  Ollama:            env ignored — Settings chose {o.get('host')}:{o.get('port')}")
        return
    try:
        p = urlsplit(url if "://" in url else f"http://{url}")
        host, port = f"{p.scheme or 'http'}://{p.hostname}", int(p.port or default["port"])
    except ValueError:
        log.warning(f"  Ollama:            VISUALWEAVER_OLLAMA_URL={url!r} is not a URL — ignored")
        return
    o["host"], o["port"] = host, port
    state._ollama_env_url = f"{host}:{port}"
    log.info(f"  Ollama:            env ({host}:{port})")


def apply_storage_env() -> None:
    """VISUALWEAVER_STORAGE=redis|mongodb|both with VISUALWEAVER_MONGODB_* (the Docker
    Compose deployment), applied to this run on top of config.json:

    mongodb + URI → the built-in "default" connection is that MongoDB;
    both + URI    → that MongoDB is the built-in connection "local-mongodb" (or, when
                    the built-in store already is MongoDB, the built-in MongoDB itself);
    otherwise     → nothing changes: config.json (written by the installer) describes
                    the stores, and VISUALWEAVER_STORAGE alone only records the choice.

    The built-in store is never switched to Redis from here — the data on "default"
    would vanish from view. Nothing from the environment is written to config.json
    (state._storage_env_overlay keeps what it replaced; config.save_config writes that).
    """
    choice = os.environ.get("VISUALWEAVER_STORAGE", "").strip().lower()
    if not choice:
        return
    if choice not in _STORAGE_CHOICES:
        log.warning(f"  VISUALWEAVER_STORAGE={choice!r} is not one of {', '.join(_STORAGE_CHOICES)} — ignored")
        return
    mongo = _mongodb_from_env() if choice in ("mongodb", "both") else None
    if not isinstance(state._config.get("storage"), dict):
        state._config["storage"] = {}
    storage = state._config["storage"]
    ov = state._storage_env_overlay
    ov.clear()
    if mongo and (choice == "mongodb" or storage.get("default_type") == "mongodb"):
        ov["default_type"] = storage.get("default_type")
        ov["mongodb"] = copy.deepcopy(state._config.get("mongodb"))
        state._config["mongodb"] = {**(state._config.get("mongodb") or {}), **mongo}
        storage["default_type"] = "mongodb"
        log.info(f"  Storage:           env ({choice}) — built-in MongoDB at {mongo_store.display_host(mongo['uri'])}")
    elif mongo:
        eps = state._config.get("redis_endpoints") or []
        before = next((e for e in eps if isinstance(e, dict) and e.get("name") == LOCAL_MONGODB), None)
        ov["endpoint"] = (LOCAL_MONGODB, copy.deepcopy(before))
        state._config["redis_endpoints"] = ([e for e in eps if e is not before]
                                            + [{"name": LOCAL_MONGODB, **mongo}])
        log.info(f"  Storage:           env ({choice}) — '{LOCAL_MONGODB}' at {mongo_store.display_host(mongo['uri'])}")
    else:
        if choice == "mongodb" and redis_store.default_type() != "mongodb":
            log.warning("  VISUALWEAVER_STORAGE=mongodb, but VISUALWEAVER_MONGODB_URI is not set and "
                        "config.json's built-in store is Redis — staying on it")
        log.info(f"  Storage:           env ({choice}) — as config.json describes")


@appcore.app.on_event("startup")
async def startup():
    """
    Called once when Uvicorn starts.
    - Loads config from disk (merged with defaults)
    - Reads API keys from environment (they override config-file keys)
    - Connects to Redis and warms up the embedding model
    - Prints diagnostic info to stdout
    """

    state._config = config.load_config()
    config.load_logs()
    config.load_feedback()   # without this the first rating after a restart truncates the store

    log.info("=" * 60)
    log.info(f"VisualWeaver v{constants.__version__} — startup diagnostics")
    log.info(f"  Python:   {platform.python_version()}")
    from . import routes_storage   # late: the routes load after startup
    log.info("  Libraries: " + ", ".join(f"{l['name']} {l['version'] or 'not installed'}"
                                         for l in routes_storage.libraries()))
    log.info(f"  Config:   {constants.CONFIG_PATH} ({'found' if constants.CONFIG_PATH.exists() else 'NOT FOUND — using defaults'})")
    log.info(f"  Provider: {state._config.get('provider', 'ollama')}")

    # ── Access URLs ────────────────────────────────────────────────────────
    # Show every URL the UI can be reached at, so the user can copy one from the
    # console. Host/port mirror what cli() binds (VISUALWEAVER_HOST / VISUALWEAVER_PORT);
    # a raw `uvicorn --host/--port` bypasses these env vars, so the URLs below assume
    # the standard `visualweaver` entrypoint.
    _bind_host = os.environ.get("VISUALWEAVER_HOST", "127.0.0.1")
    _port      = os.environ.get("VISUALWEAVER_PORT", "8420")
    # Sign-in (auth.py). start.sh repeats these warning lines on the console.
    log.info(f"  {auth.describe()}")
    for _w in auth.startup_warnings(_bind_host):
        log.warning(f"  SIGN-IN WARNING: {_w}")
    _in_docker = os.path.exists("/.dockerenv")
    _urls: list[str] = []
    if _bind_host in ("0.0.0.0", "::", ""):
        # Bound to all interfaces — reachable on loopback AND the LAN.
        _urls.append(f"http://localhost:{_port}")
        # Inside a container the detected IP is the container's, not the host's, and the
        # published-port mapping is unknown here — so don't print a misleading LAN URL.
        _lan_ip = None if _in_docker else _primary_lan_ip()
        if _lan_ip:
            _urls.append(f"http://{_lan_ip}:{_port}  (LAN — no built-in auth; trusted networks only)")
        elif _in_docker:
            _urls.append(f"(in Docker: reach it at http://<host>:<published-port>)")
    else:
        _urls.append(f"http://{_bind_host}:{_port}")
        if _bind_host == "127.0.0.1":
            _urls.append(f"http://localhost:{_port}")
    log.info(f"  Access:   {_urls[0]}")
    for _u in _urls[1:]:
        log.info(f"            {_u}")

    # ── Anthropic Claude API key ───────────────────────────────────────────
    state._env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    stored_claude = state._config.get("claude", {}).get("api_key", "")
    if state._env_key:
        state._config.setdefault("claude", {})["api_key"] = state._env_key
        masked = state._env_key[:8] + "…" + state._env_key[-4:] if len(state._env_key) > 12 else "***"
        log.info(f"  ANTHROPIC_API_KEY: env ({masked}) — takes precedence over config.json")
    elif stored_claude:
        log.info("  ANTHROPIC_API_KEY: from config.json")
    else:
        log.info("  ANTHROPIC_API_KEY: not set — Claude unavailable")

    # ── OpenAI API key ─────────────────────────────────────────────────────
    state._openai_env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    stored_openai = state._config.get("openai", {}).get("api_key", "")
    if state._openai_env_key:
        state._config.setdefault("openai", {})["api_key"] = state._openai_env_key
        masked = state._openai_env_key[:8] + "…" + state._openai_env_key[-4:] if len(state._openai_env_key) > 12 else "***"
        log.info(f"  OPENAI_API_KEY:    env ({masked}) — takes precedence over config.json")
    elif stored_openai:
        log.info("  OPENAI_API_KEY:    from config.json")
    else:
        log.info("  OPENAI_API_KEY:    not set — OpenAI unavailable")

    # ── Qwen / DashScope API key ───────────────────────────────────────────
    state._qwen_env_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    stored_qwen = state._config.get("qwen", {}).get("api_key", "")
    if state._qwen_env_key:
        state._config.setdefault("qwen", {})["api_key"] = state._qwen_env_key
        masked = state._qwen_env_key[:8] + "…" + state._qwen_env_key[-4:] if len(state._qwen_env_key) > 12 else "***"
        log.info(f"  DASHSCOPE_API_KEY: env ({masked}) — takes precedence over config.json")
    elif stored_qwen:
        log.info("  DASHSCOPE_API_KEY: from config.json")
    else:
        log.info("  DASHSCOPE_API_KEY: not set — Qwen unavailable")

    # ── Mistral API key ────────────────────────────────────────────────────
    state._mistral_env_key = os.environ.get("MISTRAL_API_KEY", "").strip()
    stored_mistral = state._config.get("mistral", {}).get("api_key", "")
    if state._mistral_env_key:
        state._config.setdefault("mistral", {})["api_key"] = state._mistral_env_key
        masked = state._mistral_env_key[:8] + "…" + state._mistral_env_key[-4:] if len(state._mistral_env_key) > 12 else "***"
        log.info(f"  MISTRAL_API_KEY:   env ({masked}) — takes precedence over config.json")
    elif stored_mistral:
        log.info("  MISTRAL_API_KEY:   from config.json")
    else:
        log.info("  MISTRAL_API_KEY:   not set — Mistral unavailable")

    # ── Groq API key ───────────────────────────────────────────────────────
    state._groq_env_key = os.environ.get("GROQ_API_KEY", "").strip()
    stored_groq = state._config.get("groq", {}).get("api_key", "")
    if state._groq_env_key:
        state._config.setdefault("groq", {})["api_key"] = state._groq_env_key
        masked = state._groq_env_key[:8] + "…" + state._groq_env_key[-4:] if len(state._groq_env_key) > 12 else "***"
        log.info(f"  GROQ_API_KEY:      env ({masked}) — takes precedence over config.json")
    elif stored_groq:
        log.info("  GROQ_API_KEY:      from config.json")
    else:
        log.info("  GROQ_API_KEY:      not set — Groq unavailable")

    # ── Google Gemini API key ──────────────────────────────────────────────
    state._gemini_env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    stored_gemini = state._config.get("gemini", {}).get("api_key", "")
    if state._gemini_env_key:
        state._config.setdefault("gemini", {})["api_key"] = state._gemini_env_key
        masked = state._gemini_env_key[:8] + "…" + state._gemini_env_key[-4:] if len(state._gemini_env_key) > 12 else "***"
        log.info(f"  GEMINI_API_KEY:    env ({masked}) — takes precedence over config.json")
    elif stored_gemini:
        log.info("  GEMINI_API_KEY:    from config.json")
    else:
        log.info("  GEMINI_API_KEY:    not set — Gemini unavailable")

    # Redis host/port env overrides — used by the Docker image to point at the
    # `redis` compose service without editing config.json.
    _redis_host_env = os.environ.get("VISUALWEAVER_REDIS_HOST", "").strip()
    _redis_port_env = os.environ.get("VISUALWEAVER_REDIS_PORT", "").strip()
    if _redis_host_env:
        state._config.setdefault("redis", {})["host"] = _redis_host_env
        log.info(f"  REDIS host:        env ({_redis_host_env})")
    if _redis_port_env:
        state._config.setdefault("redis", {})["port"] = int(_redis_port_env)
        log.info(f"  REDIS port:        env ({_redis_port_env})")

    apply_storage_env()
    apply_ollama_env()

    rc = state._config.get("redis", {})
    log.info(f"  Ollama:   {state._config.get('ollama', {}).get('host')}:{state._config.get('ollama', {}).get('port')}")
    if redis_store.default_type() == "mongodb":
        log.info(f"  Storage:  built-in MongoDB at "
                 f"{mongo_store.display_host((state._config.get('mongodb') or {}).get('uri', ''))}")
    else:
        log.info(f"  Redis:    {rc.get('host')}:{rc.get('port')}  db={rc.get('db', 0)}")
    _app = (state._config.get("storage") or {}).get("app") or "default"
    if _app != "default":
        log.info(f"  App data: connection '{_app}'")

    extra = state._config.get("redis_endpoints", [])
    if extra:
        log.info(f"  Extra storage connections: {[e.get('name') for e in extra]}")
    log.info("=" * 60)

    # ── All slow work runs in background so startup returns immediately ────
    # SentenceTransformer model loading and Redis SCAN over all endpoints
    # can both take 5-60 s.  Doing them here would block Uvicorn from
    # accepting connections, causing a blank browser until they finish.
    async def _bg_init():
        # 1. Probe all endpoints — fast PING, marks unreachable ones offline
        await asyncio.to_thread(redis_store.refresh_endpoint_health)
        # Retrieval counters drive the measured advice under the RAG controls.
        # They used to live only in memory, so Settings — most often opened right
        # after a restart — had nothing to show.
        await asyncio.to_thread(rag_admin.load_rag_stats)
        # The conversation registry, and the retention applied to what is stored — at
        # boot, so an upgraded install's one-day expiries are lifted before they run out.
        try:
            await asyncio.to_thread(sessions.ensure_index)
        except Exception as e:
            log.warning(f"  Conversation registry: not ready yet ({str(e)[:200]})")
        log.info(f"  Endpoint health: { {k: ('OK' if v else 'OFFLINE') for k, v in state._endpoint_health.items()} }")
        # What each connection runs, built-in first (short timeouts, probed in parallel).
        try:
            from . import routes_storage
            for line in await asyncio.to_thread(routes_storage.component_log_lines):
                log.info(f"  Storage {line}")
        except Exception as e:
            log.warning(f"  Storage components: could not be read ({e})")

        # 2. Warm up embedding model (5-15 s, CPU-bound)
        try:
            await asyncio.to_thread(embeddings.get_embed_model)
            log.info(f"  Embedding model ready: {state._embed_model_name}")
            embeddings._migrate_chunk_size_to_model_limit()
        except Exception as e:
            log.warning(f"  Embedding model warmup failed: {e}")

        # 2b. Warm the semantic cache and (if enabled) the cross-encoder reranker.
        # Both used to initialise lazily inside handle_chat, ON the event loop, so the
        # first question after a restart stalled seconds while the whole server froze.
        try:
            await asyncio.to_thread(cache._get_semantic_cache)
            log.info("  Semantic cache ready")
        except Exception as e:
            log.warning(f"  Semantic cache warmup failed: {e}")
        finally:
            # Warm done (success or not): from here cache_lookup/store use the cache
            # normally instead of skipping to avoid the inline build.
            state._semantic_cache_ready = True
        if state._config.get("reranker", {}).get("enabled", False):
            try:
                await asyncio.to_thread(embeddings.get_reranker)
                log.info("  Reranker ready")
            except Exception as e:
                log.warning(f"  Reranker warmup failed: {e}")

        # 3. Ensure FT indexes only on reachable endpoints
        try:
            instances = await asyncio.to_thread(rag_admin.list_rag_instances)
            for inst in instances:
                inst_name = inst.get("name", "")
                ep_name   = inst.get("redis_endpoint", "default")
                if inst_name and state._endpoint_health.get(ep_name, True):
                    try:
                        rc_inst = rag_admin.rc_for_instance(inst_name)
                        rag._index_ensured.discard(inst_name)
                        await asyncio.to_thread(rag.ensure_rag_index, inst_name, rc_inst)
                    except Exception as idx_err:
                        log.warning(f"  Could not ensure index for '{inst_name}': {idx_err}")
        except Exception:
            pass

    asyncio.create_task(_bg_init())

    # ── Start background recrawl scheduler ────────────────────────────────
    state._recrawl_task = asyncio.create_task(ws._recrawl_loop())
    log.info("  Recrawl scheduler: started")

    # ── Start watched-folder scanner ──────────────────────────────────────
    state._watch_task = asyncio.create_task(ws._watch_folder_loop())
    log.info("  Watched-folder scanner: started")


@appcore.app.on_event("shutdown")
async def on_shutdown():
    """Stop the page-extraction worker processes (pagetext) with the server."""
    await asyncio.to_thread(pagetext.shutdown)
