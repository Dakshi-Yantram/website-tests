"""Staging preflight: verify the environment BEFORE any browser or API test runs.

Each check returns one of the agent's existing labels:
  PASS          verified by this code
  FAIL          a safety rule is violated (production origin, live payment key, bad database name...)
  BLOCKED       something required is not configured, so the dependent tests cannot run
  NOT VERIFIED  declared but cannot be proven from here (e.g. test-data isolation), or probe skipped (offline)

`ready` is true only if there is no FAIL and no BLOCKED. Secret VALUES are never printed or returned:
checks look at env vars but report only names and verdicts.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, asdict
from urllib.parse import urlsplit

from .errors import SafetyBlocked
from .provenance import describe
from .safety import Environment, _host, normalize_origin, _default_fetch

QUICK_TUNNEL_HOSTS = ("trycloudflare.com", "ngrok-free.app", "ngrok.io", "loca.lt", "serveo.net")
# Payment key shapes. We only ever look at the prefix, never print the key.
_LIVE_KEY_RX = re.compile(r"^(?:rzp_live_|sk_live_|pk_live_|rk_live_|live[_-])|[_-]live[_-]", re.I)
_TEST_KEY_RX = re.compile(r"^(?:rzp_test_|sk_test_|pk_test_|rk_test_|test[_-])|[_-](?:test|sandbox)[_-]", re.I)


@dataclass
class Check:
    name: str
    status: str
    detail: str

    def as_dict(self):
        return asdict(self)


def classify_payment_key(value: str) -> str:
    """'live' | 'test' | 'unknown'. Live wins if both patterns match."""
    v = (value or "").strip()
    if _LIVE_KEY_RX.search(v):
        return "live"
    if _TEST_KEY_RX.search(v):
        return "test"
    return "unknown"


def payment_key_findings(env: Environment, environ=None) -> list:
    """Hard-stop findings (used by the write gate): configured payment keys that look LIVE. Names only, never values."""
    environ = os.environ if environ is None else environ
    bad = []
    for field_name in ("key_id_env", "key_secret_env", "webhook_secret_env"):
        var = (env.payment or {}).get(field_name)
        if var and classify_payment_key(environ.get(var, "")) == "live":
            bad.append(f"environment variable {var} holds a LIVE-looking payment key")
    return bad


def _probe(origin: str, spec: dict, fetch):
    import json
    path, key, equals = spec.get("path"), spec.get("json_key"), spec.get("equals")
    if not (path and key and equals):
        raise SafetyBlocked("probe needs path, json_key and equals")
    body = (fetch or _default_fetch)(origin + "/" + str(path).lstrip("/"))
    value = json.loads(body)
    for part in str(key).split("."):
        value = value[part]
    if str(value) != str(equals):
        raise SafetyBlocked("server reports a different environment than declared")


def _origin_check(label: str, url: str, env: Environment) -> Check:
    try:
        o = normalize_origin(url)
    except SafetyBlocked as e:
        return Check(label, "FAIL", str(e))
    prod_hosts = {_host(x) for x in env.production_origins}
    if o in env.production_origins or _host(o) in prod_hosts:
        return Check(label, "FAIL", f"{o} is a PRODUCTION origin")
    if env.is_staging_like and o not in env.allowed_origins:
        return Check(label, "FAIL", f"{o} is not in allowed_origins of {env.name!r}")
    if not env.is_staging_like:
        return Check(label, "FAIL", f"environment {env.name!r} is kind={env.kind}: writes are never allowed")
    if o.startswith("http://") and _host(o) not in ("localhost", "127.0.0.1", "::1") and not env.allow_insecure:
        return Check(label, "FAIL", f"{o} is plain http")
    return Check(label, "PASS", f"{o} is an allowed {env.kind} origin and not production")


def run_preflight(env: Environment, app: str, *, online: bool = False, fetch=None, environ=None, roles=None, sources=None) -> list:
    """Return a list of Check. Offline by default (no network); `online=True` also runs the declared probes."""
    environ = os.environ if environ is None else environ
    checks: list[Check] = []

    # 1. environment kind
    if env.is_staging_like:
        checks.append(Check("environment_kind", "PASS", f"{env.name!r} is declared kind={env.kind} in the environments file"))
    else:
        checks.append(Check("environment_kind", "FAIL", f"{env.name!r} is kind={env.kind}; production is read-only, preflight for writes cannot pass"))

    # 2. frontend URL
    try:
        front = env.base_url(app)
        c = _origin_check("frontend_url", front, env)
        checks.append(c)
        host = _host(normalize_origin(front))
        if any(host.endswith(t) for t in QUICK_TUNNEL_HOSTS):
            checks.append(Check("frontend_url_stability", "NOT VERIFIED", f"{host} is a temporary tunnel host; its address changes when restarted"))
    except SafetyBlocked as e:
        checks.append(Check("frontend_url", "BLOCKED", str(e)))

    # 3. frontend probe
    if env.verify:
        if not online:
            checks.append(Check("frontend_probe", "NOT VERIFIED", "probe is declared but skipped (offline); run preflight with --online"))
        else:
            try:
                _probe(normalize_origin(env.base_url(app)), env.verify, fetch)
                checks.append(Check("frontend_probe", "PASS", "server confirmed the declared environment"))
            except Exception as e:
                checks.append(Check("frontend_probe", "FAIL", f"could not confirm environment ({type(e).__name__}): {str(e)[:100]}"))
    else:
        checks.append(Check("frontend_probe", "NOT VERIFIED", "no `verify` probe declared: staging is trusted by allowlist only"))

    # 4. API base URL (+ probe)
    if app in env.api_urls:
        checks.append(_origin_check("api_base_url", env.api_urls[app], env))
        if env.api_verify:
            if not online:
                checks.append(Check("api_probe", "NOT VERIFIED", "api_verify is declared but skipped (offline)"))
            else:
                try:
                    _probe(normalize_origin(env.api_urls[app]), env.api_verify, fetch)
                    checks.append(Check("api_probe", "PASS", "API confirmed the declared environment"))
                except Exception as e:
                    checks.append(Check("api_probe", "FAIL", f"could not confirm API environment ({type(e).__name__}): {str(e)[:100]}"))
        else:
            checks.append(Check("api_probe", "NOT VERIFIED", "no `api_verify` declared: API host is trusted by allowlist only"))
    else:
        checks.append(Check("api_base_url", "BLOCKED", f"no api_base_url declared for {app} in {env.name!r}; API tests cannot run"))

    # 5. payment sandbox
    pay = env.payment or {}
    if not pay:
        checks.append(Check("payment_sandbox", "BLOCKED", "no `payment` section declared; payment tests cannot run (and will never use real payments)"))
    else:
        mode = str(pay.get("mode", "")).lower()
        if mode not in ("sandbox", "test"):
            checks.append(Check("payment_sandbox", "FAIL", f"payment.mode is {mode!r}; only sandbox/test is allowed"))
        else:
            missing = [pay[k] for k in ("key_id_env",) if pay.get(k) and not environ.get(pay[k], "").strip()]
            if not pay.get("key_id_env"):
                checks.append(Check("payment_sandbox", "BLOCKED", "payment.key_id_env not declared (name of the env var that holds the SANDBOX key id)"))
            elif missing:
                checks.append(Check("payment_sandbox", "BLOCKED", f"environment variable {missing[0]} is not set"))
            else:
                live = payment_key_findings(env, environ)
                kind = classify_payment_key(environ.get(pay["key_id_env"], ""))
                if live:
                    checks.append(Check("payment_sandbox", "FAIL", "; ".join(live)))
                elif kind == "test":
                    checks.append(Check("payment_sandbox", "PASS", f"{pay.get('provider', 'provider')}: {pay['key_id_env']} looks like a sandbox/test key"))
                else:
                    checks.append(Check("payment_sandbox", "NOT VERIFIED", f"{pay['key_id_env']} has an unrecognised key format; cannot prove it is a sandbox key"))

    # 6. database
    db = env.database or {}
    if not db:
        checks.append(Check("database", "NOT VERIFIED", "no `database` section declared; the agent cannot confirm which database the staging backend uses"))
    elif not db.get("url_env"):
        checks.append(Check("database", "BLOCKED", "database.url_env not declared"))
    elif not environ.get(db["url_env"], "").strip():
        checks.append(Check("database", "BLOCKED", f"environment variable {db['url_env']} is not set"))
    else:
        u = urlsplit(environ[db["url_env"]].strip())
        host, dbname = (u.hostname or "").lower(), u.path.lstrip("/")
        forbidden = [str(h).lower() for h in db.get("forbidden_hosts") or []]
        pattern = db.get("name_pattern")
        if host in forbidden:
            checks.append(Check("database", "FAIL", "database host is on the forbidden (production) host list"))
        elif pattern and not re.search(pattern, dbname, re.I):
            checks.append(Check("database", "FAIL", f"database name does not match required pattern {pattern!r}"))
        elif not pattern and not forbidden:
            checks.append(Check("database", "NOT VERIFIED", "database configured but no name_pattern/forbidden_hosts to check against"))
        else:
            checks.append(Check("database", "PASS", "database host/name satisfy the declared staging rules"))

    # 7. test credentials (names only)
    if roles is None:
        import config
        roles = {**config.ROLES, **config.AGENT_ROLES}
    app_roles = {n: r for n, r in roles.items() if r.app == app}
    missing = sorted(n for n, r in app_roles.items() if not (environ.get(r.email_env, "").strip() and environ.get(r.password_env, "")))
    td = env.test_data or {}
    if not app_roles:
        checks.append(Check("test_credentials", "BLOCKED", f"no roles defined for {app}"))
    elif missing:
        checks.append(Check("test_credentials", "BLOCKED", "credentials not set for role(s): " + ", ".join(missing) + " (set the *_EMAIL / *_PASSWORD env vars)"))
    else:
        rx = td.get("account_email_regex")
        if rx:
            bad = sorted(n for n, r in app_roles.items() if not re.search(rx, environ.get(r.email_env, ""), re.I))
            checks.append(Check("test_credentials", "FAIL", "account email does not match account_email_regex for role(s): " + ", ".join(bad))
                          if bad else Check("test_credentials", "PASS", f"all {len(app_roles)} role(s) have credentials and match the dedicated test-account pattern"))
        else:
            checks.append(Check("test_credentials", "NOT VERIFIED", f"credentials set for {len(app_roles)} role(s) but no test_data.account_email_regex to prove they are dedicated test accounts"))

    # 8. test data isolation
    prefix = td.get("marker_prefix")
    if not td:
        checks.append(Check("test_data_isolation", "NOT VERIFIED", "no `test_data` section declared; records are tagged AUTOTEST_<runid> by the runner but isolation is not declared"))
    elif prefix != "AUTOTEST_":
        checks.append(Check("test_data_isolation", "FAIL", "test_data.marker_prefix must be 'AUTOTEST_' (the runner's synthetic-data marker)"))
    elif td.get("dedicated_accounts") is True:
        checks.append(Check("test_data_isolation", "NOT VERIFIED", "declared: AUTOTEST_ marker + dedicated accounts. The agent cannot prove the backend keeps them separate"))
    else:
        checks.append(Check("test_data_isolation", "NOT VERIFIED", "marker prefix is correct but test_data.dedicated_accounts is not true"))

    # 9. source provenance
    if sources is None:
        checks.append(Check("source_commits", "NOT VERIFIED", "no --source / QA_SOURCE_* given: tested commit SHAs will not be recorded"))
    else:
        for n, rec in sources.items():
            if not rec.get("available"):
                checks.append(Check(f"source_{n}", "BLOCKED", describe(rec)))
            elif rec["dirty"]:
                checks.append(Check(f"source_{n}", "NOT VERIFIED", describe(rec)))
            else:
                checks.append(Check(f"source_{n}", "PASS", describe(rec)))
    return checks


def is_ready(checks) -> bool:
    return not any(c.status in ("FAIL", "BLOCKED") for c in checks)


def format_checks(checks) -> str:
    w = max(len(c.name) for c in checks) if checks else 10
    lines = [f"{c.status:12} {c.name:<{w}}  {c.detail}" for c in checks]
    lines.append("")
    n = {s: sum(c.status == s for c in checks) for s in ("PASS", "FAIL", "BLOCKED", "NOT VERIFIED")}
    lines.append("READY for staging tests" if is_ready(checks) else "NOT READY: resolve FAIL/BLOCKED items before running tests that depend on them")
    lines.append(", ".join(f"{k}={v}" for k, v in n.items()))
    return "\n".join(lines)
