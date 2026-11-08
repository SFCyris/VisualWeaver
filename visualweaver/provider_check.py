# SPDX-License-Identifier: AGPL-3.0-or-later
"""Is the model provider chosen in config.json able to answer? Used by install.sh and
docker.sh after setting up, before anyone opens the page.

Ollama must answer and have a chat model (an embedding-only model cannot chat); a hosted
provider needs its API key, in the config or in its environment variable. Prints one line,
``<ok|warn>\\t<message>\\t<fix>``. Only the standard library, and nothing from the rest of
the package, so it runs as a plain file — before the app's dependencies are installed, or
piped into a container whose image is older than this checkout:

    python provider_check.py <config.json> [--docker]

``--docker``: the API-key fix names docker-compose.yml instead of ./start.sh.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

LABEL = {"ollama": "Ollama", "claude": "Claude", "openai": "OpenAI", "qwen": "Qwen",
         "mistral": "Mistral", "groq": "Groq", "gemini": "Gemini"}
ENV = {"claude": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "qwen": "DASHSCOPE_API_KEY",
       "mistral": "MISTRAL_API_KEY", "groq": "GROQ_API_KEY", "gemini": "GEMINI_API_KEY"}


def is_embedding_model(m: dict) -> bool:
    """An Ollama model that only embeds — known by its name or by its BERT family (the
    same rule as the model picker's)."""
    d = m.get("details") or {}
    fams = [d.get("family")] + list(d.get("families") or [])
    return "embed" in str(m.get("name", "")).lower() or any("bert" in str(f).lower() for f in fams if f)


def ollama_base(cfg: dict, environ=os.environ) -> str:
    """Where the app reaches Ollama: the setting, or VISUALWEAVER_OLLAMA_URL while the
    setting is still the default (as the app applies it at startup)."""
    o = cfg.get("ollama") if isinstance(cfg.get("ollama"), dict) else {}
    host = str(o.get("host") or "http://localhost").rstrip("/")
    try:
        port = int(o.get("port") or 11434)
    except (TypeError, ValueError):
        port = 11434
    env = environ.get("VISUALWEAVER_OLLAMA_URL", "").strip()
    if env and (host, port) == ("http://localhost", 11434):
        try:
            u = urllib.parse.urlsplit(env if "://" in env else "http://" + env)
            host, port = f"{u.scheme or 'http'}://{u.hostname}", int(u.port or 11434)
        except ValueError:
            pass
    return f"{host}:{port}"


def check(cfg, environ=os.environ, timeout: float = 3.0, key_where: str = "for ./start.sh") -> tuple:
    """(kind, message, fix) for the provider config.json selects; kind is "ok" or "warn"."""
    cfg = cfg if isinstance(cfg, dict) else {}
    p = str(cfg.get("provider") or "ollama").strip().lower()
    if p == "ollama":
        base = ollama_base(cfg, environ)
        try:
            with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as r:
                models = json.load(r).get("models") or []
        except Exception:
            return ("warn", f"Ollama is not reachable at {base}",
                    "start Ollama (https://ollama.com) and pull a chat model (ollama pull llama3), "
                    "or choose a hosted provider in Settings → Providers and add its API key.")
        chat = [str(m.get("name", "")) for m in models if isinstance(m, dict) and not is_embedding_model(m)]
        if chat:
            n = len(chat)
            return ("ok", f"Ollama at {base}: {n} chat model{'' if n == 1 else 's'} "
                          f"({', '.join(chat[:3])}{', …' if n > 3 else ''})", "")
        if models:
            return ("warn", f"Ollama at {base} has only embedding models ({len(models)})",
                    "pull a chat model: ollama pull llama3")
        return ("warn", f"Ollama at {base} has no models", "pull a chat model: ollama pull llama3")
    if p in ENV:
        sect = cfg.get(p) if isinstance(cfg.get(p), dict) else {}
        if str(sect.get("api_key") or "").strip():
            return ("ok", f"{LABEL[p]}: API key set in the app config", "")
        if environ.get(ENV[p], "").strip():
            return ("ok", f"{LABEL[p]}: API key from {ENV[p]}", "")
        return ("warn", f"{LABEL[p]} is selected but has no API key",
                f"add it in Settings → Providers, or set {ENV[p]} {key_where}.")
    return ("warn", f"unknown provider '{p}' in the app config", "choose one in Settings → Providers.")


def main(argv) -> int:
    try:
        with open(argv[1], encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    where = "in docker-compose.yml (environment)" if "--docker" in argv[2:] else "for ./start.sh"
    print("\t".join(check(cfg, key_where=where)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
