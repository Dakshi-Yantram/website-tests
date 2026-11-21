"""Environment declarations and origin handling for the login agent (fail closed).

Reads the SAME file the rest of the project uses, config/environments.yml (override: QA_ENV_FILE or an explicit path):
  kind: production -> read-only, always.      kind: staging | local -> authenticated tests allowed (guard.py still limits writes).
Anything unknown, unreadable or inconsistent raises SafetyBlocked; the agent then exits with code 2 before opening a browser.
"""
import json
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .errors import SafetyBlocked

ROOT = Path(__file__).resolve().parent.parent
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def default_env_file() -> Path:
    return Path(os.getenv("QA_ENV_FILE") or ROOT / "config" / "environments.yml")


def normalize_origin(url: str) -> str:
    """scheme://host[:port], lower-case, default ports removed. Raises SafetyBlocked for anything that is not http(s)."""
    try:
        u = urlsplit(str(url).strip())
        scheme, host, port = (u.scheme or "").lower(), (u.hostname or "").lower(), u.port
    except ValueError as e:
        raise SafetyBlocked(f"cannot parse URL origin: {e}")
    if scheme not in ("http", "https") or not host:
        raise SafetyBlocked(f"URL has no usable http(s) origin: {str(url)[:80]!r}")
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        return f"{scheme}://{host}:{port}"
    return f"{scheme}://{host}"


def _host(origin: str) -> str:
    return urlsplit(origin).hostname or ""


def _is_local(origin: str) -> bool:
    return _host(origin) in ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class Environment:
    name: str
    kind: str                                              # production | staging | local
    base_urls: dict = field(default_factory=dict)          # app -> base url
    allowed_origins: tuple = ()                            # explicit allowlist (staging/local only)
    auth_origins: tuple = ()                               # extra origins (the API host) that may receive the login POST
    api_urls: dict = field(default_factory=dict)           # app -> api_base_url (optional)
    production_origins: tuple = ()                         # every origin declared production anywhere in the file
    verify: dict = field(default_factory=dict)             # optional probe proving this really is staging

    @property
    def is_staging_like(self) -> bool:
        return self.kind in ("staging", "local")


def load_environments(path=None) -> dict:
    p = Path(path) if path else default_env_file()
    if not p.is_file():
        raise SafetyBlocked(f"environment file not found: {p} (cannot tell production from staging, so nothing runs; "
                            f"create it or set QA_ENV_FILE)")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SafetyBlocked(f"environment file {p} is not valid YAML: {str(e)[:200]}")
    if not isinstance(data, dict) or not isinstance(data.get("environments"), dict):
        raise SafetyBlocked(f"environment file {p}: a top-level 'environments' mapping is required")

    raw_envs = data["environments"]
    prod = {normalize_origin(o) for o in (data.get("production_origins") or [])}
    for e in raw_envs.values():                                    # every production base/api URL joins the block list
        if isinstance(e, dict) and e.get("kind") == "production":
            for a in (e.get("apps") or {}).values():
                for key in ("base_url", "api_base_url"):
                    if isinstance(a, dict) and a.get(key):
                        prod.add(normalize_origin(a[key]))
    prod_hosts = {_host(o) for o in prod}

    out = {}
    for name, e in raw_envs.items():
        if not isinstance(e, dict):
            raise SafetyBlocked(f"environment {name!r} must be a mapping")
        kind = e.get("kind")
        if kind not in ("production", "staging", "local"):
            raise SafetyBlocked(f"environment {name!r}: kind must be production|staging|local, got {kind!r}")
        apps = {k: a for k, a in (e.get("apps") or {}).items() if isinstance(a, dict)}
        base_urls = {k: str(a.get("base_url", "")).strip() for k, a in apps.items()}
        api_urls = {k: str(a["api_base_url"]).strip() for k, a in apps.items() if a.get("api_base_url")}
        allowed = tuple(sorted({normalize_origin(o) for o in (e.get("allowed_origins") or [])}))
        auth = tuple(sorted({normalize_origin(o) for o in (e.get("auth_origins") or [])}))
        if kind != "production":
            if not allowed:
                raise SafetyBlocked(f"environment {name!r} ({kind}) must list explicit allowed_origins")
            for o in allowed:
                if o in prod or _host(o) in prod_hosts:
                    raise SafetyBlocked(f"environment {name!r}: origin {o} is a production origin and cannot be a test target")
                if o.startswith("http://") and not _is_local(o) and not e.get("allow_insecure"):
                    raise SafetyBlocked(f"environment {name!r}: {o} is plain http; set allow_insecure: true to accept that explicitly")
            for app, b in base_urls.items():
                if b and normalize_origin(b) not in allowed:
                    raise SafetyBlocked(f"environment {name!r}: base_url of {app} is not in allowed_origins")
            for app, b in api_urls.items():
                if normalize_origin(b) not in allowed:
                    raise SafetyBlocked(f"environment {name!r}: api_base_url of {app} is not in allowed_origins")
        verify = e.get("verify") or {}
        if not isinstance(verify, dict):
            raise SafetyBlocked(f"environment {name!r}: verify must be a mapping")
        out[name] = Environment(name, kind, base_urls, allowed, auth, api_urls, tuple(sorted(prod)), dict(verify))
    return out


def get_environment(name: str, path=None) -> Environment:
    envs = load_environments(path)
    if name not in envs:
        raise SafetyBlocked(f"unknown environment {name!r}; known: {sorted(envs)}")
    return envs[name]


def _default_fetch(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nurseconnect-login-agent/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310  (origin is allowlisted before this call)
        return r.read(65536).decode("utf-8", "replace")


def verify_environment(env: Environment, app: str, fetch=None) -> None:
    """Run the optional `verify:` probe. When one is declared it MUST pass; otherwise this does nothing."""
    v = env.verify
    if not v:
        return
    path, key, equals = v.get("path"), v.get("json_key"), v.get("equals")
    if not (path and key and equals):
        raise SafetyBlocked(f"environment {env.name!r}: verify needs path, json_key and equals")
    origin = normalize_origin(env.base_urls.get(app, ""))
    if origin not in env.allowed_origins:
        raise SafetyBlocked(f"verification refused: {origin} is not an allowed origin")
    try:
        value = json.loads((fetch or _default_fetch)(origin + "/" + str(path).lstrip("/")))
        for part in str(key).split("."):
            value = value[part]
    except Exception as e:
        raise SafetyBlocked(f"environment {env.name!r} could not be verified ({type(e).__name__}); treating it as not staging")
    if str(value) != str(equals):
        raise SafetyBlocked(f"environment {env.name!r}: the server reports a different environment than declared")
