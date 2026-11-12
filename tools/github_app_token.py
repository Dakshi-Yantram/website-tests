#!/usr/bin/env python3
"""Mint / revoke a SHORT-LIVED, READ-ONLY GitHub App installation token for the two private application repos.

    python tools/github_app_token.py mint --repo OWNER/nurseconnect-app --repo OWNER/hospital-app
    python tools/github_app_token.py revoke            # token read from env GH_APP_TOKEN

Inputs (all from GitHub Actions secrets; never from files in the repo):
    GH_APP_ID               numeric App ID
    GH_APP_INSTALLATION_ID  numeric installation ID
    GH_APP_PRIVATE_KEY      PEM private key of the App

Safety properties (each one is covered by agent_tests/test_github_app_token.py):
  * the token request asks for ONLY contents:read + metadata:read and ONLY the listed repositories;
  * the response is checked - if GitHub returns anything broader the token is revoked and the script fails;
  * the token is never printed. It is registered with ::add-mask:: and written to $GITHUB_OUTPUT only;
  * missing inputs fail with a clear message that never includes secret values.
Installation tokens expire after 1 hour at most; `revoke` ends them earlier (call it in an `if: always()` step).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO_RX = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
WANTED_PERMISSIONS = {"contents": "read", "metadata": "read"}


class TokenError(Exception):
    """Message is always safe to print."""


def http(method: str, url: str, headers: dict, body: dict | None = None, timeout: int = 20):
    """-> (status, parsed_json_or_None). Replaced by a fake in tests."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**headers, "Content-Type": "application/json"} if data else headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:        # noqa: S310 (fixed https://api.github.com base)
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except Exception:
            return e.code, None


def make_jwt(app_id: str, private_key_pem: str, now: int | None = None) -> str:
    try:
        import jwt                                       # PyJWT (requirements-ci.txt)
    except ImportError:
        raise TokenError("PyJWT is not installed: pip install -r requirements-ci.txt")
    now = int(now or time.time())
    try:
        return jwt.encode({"iat": now - 60, "exp": now + 9 * 60, "iss": str(app_id)}, private_key_pem, algorithm="RS256")
    except Exception as e:
        raise TokenError(f"Could not sign the App JWT ({type(e).__name__}); check GH_APP_PRIVATE_KEY is a PEM RSA key")


def _headers(auth_scheme: str, credential: str) -> dict:
    return {"Authorization": f"{auth_scheme} {credential}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "nurseconnect-qa-agent"}


def mint(app_id: str, installation_id: str, private_key: str, repos: list, http_fn=http, now: int | None = None) -> dict:
    """Return {"token", "expires_at", "repositories"}; raises TokenError (safe message) on any problem."""
    if not (app_id and installation_id and private_key):
        missing = [n for n, v in (("GH_APP_ID", app_id), ("GH_APP_INSTALLATION_ID", installation_id), ("GH_APP_PRIVATE_KEY", private_key)) if not v]
        raise TokenError(f"Missing required secret(s): {', '.join(missing)}. See docs/GITHUB_APP_SETUP.md")
    if not str(installation_id).isdigit() or not str(app_id).isdigit():
        raise TokenError("GH_APP_ID and GH_APP_INSTALLATION_ID must be numeric")
    if not repos:
        raise TokenError("At least one --repo OWNER/NAME is required (tokens are never issued for 'all repositories')")
    bad = [r for r in repos if not REPO_RX.match(r)]
    if bad:
        raise TokenError(f"Invalid repository name(s): {bad}")
    if len({r.split('/')[0].lower() for r in repos}) != 1:
        raise TokenError("All repositories must belong to the same owner")
    pem = private_key.replace("\\n", "\n")
    app_jwt = make_jwt(app_id, pem, now)
    status, body = http_fn("POST", f"{API}/app/installations/{installation_id}/access_tokens", _headers("Bearer", app_jwt),
                           {"repositories": [r.split("/", 1)[1] for r in repos], "permissions": WANTED_PERMISSIONS})
    if status != 201 or not isinstance(body, dict) or not body.get("token"):
        hint = {401: "JWT rejected (wrong App ID or private key)", 403: "installation lacks access or the App is suspended",
                404: "installation not found (wrong GH_APP_INSTALLATION_ID or App not installed on those repositories)",
                422: "requested repositories/permissions are not available to this installation"}.get(status, "unexpected response")
        raise TokenError(f"GitHub refused to issue a token (HTTP {status}): {hint}")
    token = body["token"]
    perms = body.get("permissions") or {}
    granted_repos = sorted(r.get("full_name", "").lower() for r in body.get("repositories") or [])
    problems = []
    extra = {k: v for k, v in perms.items() if WANTED_PERMISSIONS.get(k) != v}
    if extra:
        problems.append(f"broader permissions than allowed: {sorted(extra)}")
    if body.get("repository_selection") == "all" and not granted_repos:
        problems.append("token is scoped to all repositories")
    if granted_repos and granted_repos != sorted(r.lower() for r in repos):
        problems.append("token repositories differ from the requested list")
    if problems:
        try:
            revoke(token, http_fn)
        except TokenError:
            pass
        raise TokenError("Issued token violated the least-privilege policy and was revoked: " + "; ".join(problems))
    return {"token": token, "expires_at": body.get("expires_at", ""), "repositories": granted_repos or [r.lower() for r in repos]}


def revoke(token: str, http_fn=http) -> None:
    if not token:
        raise TokenError("No token to revoke")
    status, _ = http_fn("DELETE", f"{API}/installation/token", _headers("token", token))
    if status not in (204, 401):                         # 401 = already expired / revoked: fine
        raise TokenError(f"Token revocation returned HTTP {status}")


def emit_output(name: str, value: str, out=None) -> None:
    """Write name=value to $GITHUB_OUTPUT after masking the value in the log. Never prints the value itself."""
    out = out or os.getenv("GITHUB_OUTPUT")
    if not out:
        raise TokenError("GITHUB_OUTPUT is not set: refusing to print a token to the console")
    print(f"::add-mask::{value}")
    with open(out, "a", encoding="utf-8") as fh:
        fh.write(f"{name}={value}\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("mint")
    m.add_argument("--repo", action="append", default=[], help="OWNER/NAME (repeat for each private app repo)")
    sub.add_parser("revoke")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "mint":
            res = mint(os.getenv("GH_APP_ID", ""), os.getenv("GH_APP_INSTALLATION_ID", ""), os.getenv("GH_APP_PRIVATE_KEY", ""), a.repo)
            emit_output("token", res["token"])
            print(f"Issued read-only token for {', '.join(res['repositories'])}; expires {res['expires_at']} (value hidden).")
        else:
            revoke(os.getenv("GH_APP_TOKEN", ""))
            print("Token revoked.")
        return 0
    except TokenError as e:
        print(f"::error::{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
