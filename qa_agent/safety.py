"""Environment verification and write gating. Everything here fails CLOSED.

Rules (all must hold before any workflow may perform a write):
  1. The active environment is declared in config/environments.yml with kind: staging
     (the word "staging" in a URL or name proves nothing - only the explicit file does).
  2. The app's base URL origin is in that environment's explicit `allowed_origins` list.
  3. The origin (and its hostname) is not in `production_origins` or any production environment.
  4. If the environment declares a `verify` probe, the probe must succeed and match.
  5. ALLOW_WRITE_TESTS=true (or --allow-write) AND the operator typed the confirmation phrase
     for this exact environment AND named this exact workflow id in --confirm-write.
Anything unknown, missing or unparsable => read-only.
"""
import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .errors import SafetyBlocked

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = Path(os.getenv("QA_ENV_FILE") or ROOT / "config" / "environments.yml")
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# Login-style POSTs create a session, not business data. They are allowed to the app's own origin only.
AUTH_PATH_RX = re.compile(r"(?:^|/)(?:auth|authenticate|login|signin|sign-in|token|session)(?:/|$)", re.I)
CONFIRM_PREFIX = "I-CONFIRM-STAGING-WRITES-ON-"


def normalize_origin(url: str) -> str:
    """scheme://host[:port] lower-cased, default ports removed. Raises SafetyBlocked if unparsable."""
    try:
        u = urlsplit(str(url).strip())
        scheme, host = (u.scheme or "").lower(), (u.hostname or "").lower()
        port = u.port
    except ValueError as e:
        raise SafetyBlocked(f"Cannot parse URL origin: {e}")
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
    kind: str                                   # production | staging | local
    base_urls: dict = field(default_factory=dict)       # app -> base url
    allowed_origins: tuple = ()                 # explicit allowlist (staging/local only)
    allow_insecure: bool = False
    verify: dict = field(default_factory=dict)  # optional probe
    production_origins: tuple = ()              # global production list (for blocking)
    auth_origins: tuple = ()                    # extra origins (e.g. an API host) that may receive login/session POSTs

    @property
    def is_staging_like(self) -> bool:
        return self.kind in ("staging", "local")

    def base_url(self, app: str) -> str:
        try:
            return self.base_urls[app]
        except KeyError:
            raise SafetyBlocked(f"Environment {self.name!r} has no base_url for app {app!r}")


def load_environments(path=None) -> dict:
    """Load and validate the environments file. Returns {name: Environment}."""
    p = Path(path) if path else DEFAULT_ENV_FILE
    if not p.is_file():
        raise SafetyBlocked(f"Environment file not found: {p} (cannot verify any environment -> read-only)")
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise SafetyBlocked(f"Environment file {p} is not valid YAML: {str(e)[:200]}")
    if not isinstance(data, dict) or not isinstance(data.get("environments"), dict):
        raise SafetyBlocked(f"Environment file {p}: top-level 'environments' mapping is required")

    prod_origins = {normalize_origin(o) for o in (data.get("production_origins") or [])}
    envs_raw = data["environments"]
    for name, e in envs_raw.items():                      # production environments also feed the block list
        if isinstance(e, dict) and e.get("kind") == "production":
            for a in (e.get("apps") or {}).values():
                if isinstance(a, dict) and a.get("base_url"):
                    prod_origins.add(normalize_origin(a["base_url"]))
    prod_hosts = {_host(o) for o in prod_origins}

    out = {}
    for name, e in envs_raw.items():
        if not isinstance(e, dict):
            raise SafetyBlocked(f"Environment {name!r} must be a mapping")
        kind = e.get("kind")
        if kind not in ("production", "staging", "local"):
            raise SafetyBlocked(f"Environment {name!r}: kind must be production|staging|local, got {kind!r}")
        base_urls = {app: str(a.get("base_url", "")).strip() for app, a in (e.get("apps") or {}).items() if isinstance(a, dict)}
        allowed = tuple(sorted({normalize_origin(o) for o in (e.get("allowed_origins") or [])}))
        if kind in ("staging", "local"):
            if not allowed:
                raise SafetyBlocked(f"Environment {name!r} ({kind}) must list explicit allowed_origins")
            for o in allowed:
                if o in prod_origins or _host(o) in prod_hosts:
                    raise SafetyBlocked(f"Environment {name!r}: origin {o} is a production origin and cannot be a write target")
                if o.startswith("http://") and not _is_local(o) and not e.get("allow_insecure"):
                    raise SafetyBlocked(f"Environment {name!r}: {o} is plain http; set allow_insecure: true to accept that explicitly")
            for app, b in base_urls.items():
                if normalize_origin(b) not in allowed:
                    raise SafetyBlocked(f"Environment {name!r}: base_url of {app} ({normalize_origin(b)}) is not in allowed_origins")
        auth_origins = tuple(sorted({normalize_origin(o) for o in (e.get("auth_origins") or [])}))
        out[name] = Environment(name=name, kind=kind, base_urls=base_urls, allowed_origins=allowed, auth_origins=auth_origins,
                                allow_insecure=bool(e.get("allow_insecure")), verify=dict(e.get("verify") or {}),
                                production_origins=tuple(sorted(prod_origins)))
    return out


def get_environment(name: str, path=None) -> Environment:
    envs = load_environments(path)
    if name not in envs:
        raise SafetyBlocked(f"Unknown environment {name!r}; known: {sorted(envs)}")
    return envs[name]


def _default_fetch(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "nurseconnect-qa-agent/2"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 (origin is allowlisted before this call)
        return r.read(65536).decode("utf-8", "replace")


def verify_environment(env: Environment, app: str, fetch=None) -> None:
    """Run the optional verification probe. If the env declares one it MUST pass; failure => SafetyBlocked."""
    v = env.verify
    if not v:
        return
    path, key, equals = v.get("path"), v.get("json_key"), v.get("equals")
    if not (path and key and equals):
        raise SafetyBlocked(f"Environment {env.name!r}: verify needs path, json_key and equals")
    origin = normalize_origin(env.base_url(app))
    if origin not in env.allowed_origins:
        raise SafetyBlocked(f"Verification refused: {origin} is not an allowed origin")
    try:
        body = (fetch or _default_fetch)(origin + "/" + str(path).lstrip("/"))
        value = json.loads(body)
        for part in str(key).split("."):
            value = value[part]
    except Exception as e:
        raise SafetyBlocked(f"Environment {env.name!r} could not be verified ({type(e).__name__}); treating it as not staging")
    if str(value) != str(equals):
        raise SafetyBlocked(f"Environment {env.name!r} verification mismatch (server reports a different environment)")


def check_write_allowed(env: Environment, app: str, workflow_id: str, *, allow_write: bool,
                        confirm_phrase: str = "", confirmed_workflows=(), fetch=None) -> None:
    """Raise SafetyBlocked unless every rule in the module docstring holds."""
    if env is None:
        raise SafetyBlocked("No verified environment selected (set TEST_ENV / --env): writes are blocked")
    if not allow_write:
        raise SafetyBlocked("Write tests are not enabled (ALLOW_WRITE_TESTS / --allow-write is off)")
    if not env.is_staging_like:
        raise SafetyBlocked(f"Environment {env.name!r} is kind={env.kind}: production is read-only")
    origin = normalize_origin(env.base_url(app))
    if origin not in env.allowed_origins:
        raise SafetyBlocked(f"Origin {origin} is not in the staging allowlist of {env.name!r}")
    if origin in env.production_origins or _host(origin) in {_host(o) for o in env.production_origins}:
        raise SafetyBlocked(f"Origin {origin} is a known production origin")
    if confirm_phrase != CONFIRM_PREFIX + env.name:
        raise SafetyBlocked(f"Missing/incorrect staging confirmation phrase for {env.name!r}")
    if workflow_id not in set(confirmed_workflows):
        raise SafetyBlocked(f"Workflow {workflow_id!r} was not explicitly confirmed for writing (--confirm-write)")
    verify_environment(env, app, fetch=fetch)


class RequestGuard:
    """Network-level defence in depth. Attach to a Playwright context; blocks non-GET requests that
    the safety rules do not allow, no matter what the workflow or page script tries to do."""

    def __init__(self, env: Environment | None, app_origin: str, writes_enabled: bool = False, auth_any_origin: bool = False):
        self.env = env
        self.app_origin = normalize_origin(app_origin)
        self.writes_enabled = bool(writes_enabled)
        self.auth_any_origin = bool(auth_any_origin)      # legacy pytest suite only: login API host is not known in advance
        self.block_all = False            # temporarily True inside a guarded (no-write) step
        self.blocked: list = []           # human-readable, already redacted-by-construction (no bodies/headers)
        self.sent_writes: list = []       # non-GET requests that were ALLOWED through (excluding login/session requests)

    def decide(self, method: str, url: str):
        method = method.upper()
        if method in SAFE_METHODS:
            return True, "safe method"
        try:
            origin = normalize_origin(url)
        except Exception:
            return False, "unparsable URL"
        if self.block_all:
            return False, "write blocked inside a no-write step"
        path = urlsplit(url).path or "/"
        if self.writes_enabled and self.env and origin in self.env.allowed_origins:
            return True, "write allowed: verified staging origin"
        auth_ok = origin == self.app_origin or self.auth_any_origin or bool(self.env and origin in self.env.auth_origins)
        if auth_ok and AUTH_PATH_RX.search(path):
            return True, "login/session request"
        if self.env is None:
            return False, "no verified environment: read-only"
        if origin in self.env.production_origins:
            return False, "production origin is read-only"
        return False, "origin not in the staging allowlist" if origin not in self.env.allowed_origins else "writes not enabled"

    def handler(self, route):
        req = route.request
        ok, why = self.decide(req.method, req.url)
        if ok:
            if why.startswith("write allowed"):
                u = urlsplit(req.url)
                self.sent_writes.append(f"{req.method} {u.scheme}://{u.netloc}{u.path[:80]}")
            route.continue_()
        else:
            u = urlsplit(req.url)
            self.blocked.append(f"{req.method} {u.scheme}://{u.netloc}{u.path[:80]} -> {why}")
            route.abort("blockedbyclient")

    def attach(self, context):
        context.route("**/*", self.handler)
        return self
