"""GitHub App token script (mocked GitHub API) and static checks of the CI workflow. No real credentials are used:
a throw-away RSA key is generated per test run."""
import json
import re
import sys
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_tests.conftest import ROOT

sys.path.insert(0, str(ROOT / "tools"))
import github_app_token as T                                           # noqa: E402

REPOS = ["acme/nurseconnect-app", "acme/hospital-app"]


@pytest.fixture(scope="module")
def keys():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return pem, pub


class FakeGitHub:
    def __init__(self, status=201, permissions=None, repositories=None, selection="selected"):
        self.calls, self.status = [], status
        self.permissions = permissions if permissions is not None else {"contents": "read", "metadata": "read"}
        self.repositories = repositories if repositories is not None else [{"full_name": r} for r in REPOS]
        self.selection = selection

    def __call__(self, method, url, headers, body=None, timeout=20):
        self.calls.append((method, url, headers, body))
        if method == "POST":
            if self.status != 201:
                return self.status, {"message": "x"}
            return 201, {"token": "ghs_FAKEINSTALLATIONTOKEN0123456789abcdef", "expires_at": "2099-01-01T00:00:00Z", "permissions": self.permissions,
                         "repository_selection": self.selection, "repositories": self.repositories}
        return 204, None


def test_token_request_is_least_privilege_and_jwt_is_valid(keys):
    pem, pub = keys
    gh = FakeGitHub()
    out = T.mint("12345", "67890", pem, REPOS, http_fn=gh, now=1_700_000_000)
    assert out["token"].startswith("ghs_") and out["repositories"] == sorted(r.lower() for r in REPOS)
    method, url, headers, body = gh.calls[0]
    assert method == "POST" and url == "https://api.github.com/app/installations/67890/access_tokens"
    assert body == {"repositories": ["nurseconnect-app", "hospital-app"], "permissions": {"contents": "read", "metadata": "read"}}
    import jwt
    claims = jwt.decode(headers["Authorization"].split()[1], pub, algorithms=["RS256"], options={"verify_exp": False, "verify_iat": False})
    assert claims["iss"] == "12345" and claims["exp"] - claims["iat"] <= 600 + 60


def test_private_key_with_escaped_newlines_is_accepted(keys):
    pem, _ = keys
    assert T.mint("1", "2", pem.replace("\n", "\\n"), REPOS, http_fn=FakeGitHub())["token"]


@pytest.mark.parametrize("a,i,k,missing", [("", "2", "k", "GH_APP_ID"), ("1", "", "k", "GH_APP_INSTALLATION_ID"), ("1", "2", "", "GH_APP_PRIVATE_KEY")])
def test_absent_secrets_fail_safely_without_leaking(a, i, k, missing):
    with pytest.raises(T.TokenError, match=missing) as e:
        T.mint(a, i, k, REPOS, http_fn=FakeGitHub())
    assert "BEGIN" not in str(e.value)


def test_no_repos_or_bad_repos_or_mixed_owners_rejected(keys):
    pem, _ = keys
    for repos, msg in (([], "At least one"), (["not a repo"], "Invalid repository"), (["a/x", "b/y"], "same owner")):
        with pytest.raises(T.TokenError, match=msg):
            T.mint("1", "2", pem, repos, http_fn=FakeGitHub())
    with pytest.raises(T.TokenError, match="numeric"):
        T.mint("abc", "2", pem, REPOS, http_fn=FakeGitHub())


@pytest.mark.parametrize("status,hint", [(401, "wrong App ID or private key"), (404, "installation not found"), (403, "installation lacks access"), (422, "not available")])
def test_github_errors_have_clear_safe_messages(keys, status, hint):
    with pytest.raises(T.TokenError, match=hint):
        T.mint("1", "2", keys[0], REPOS, http_fn=FakeGitHub(status=status))


def test_over_broad_token_is_revoked_and_rejected(keys):
    gh = FakeGitHub(permissions={"contents": "write", "metadata": "read"})
    with pytest.raises(T.TokenError, match="revoked"):
        T.mint("1", "2", keys[0], REPOS, http_fn=gh)
    assert gh.calls[-1][0] == "DELETE" and gh.calls[-1][1].endswith("/installation/token")
    gh2 = FakeGitHub(permissions={"contents": "read", "metadata": "read", "administration": "read"})
    with pytest.raises(T.TokenError, match="administration"):
        T.mint("1", "2", keys[0], REPOS, http_fn=gh2)
    gh3 = FakeGitHub(repositories=[{"full_name": "acme/other"}])
    with pytest.raises(T.TokenError, match="repositories differ"):
        T.mint("1", "2", keys[0], REPOS, http_fn=gh3)


def test_revoke_calls_delete_and_tolerates_already_expired():
    gh = FakeGitHub()
    T.revoke("ghs_x", gh)
    assert gh.calls[0][0] == "DELETE" and gh.calls[0][2]["Authorization"] == "token ghs_x"
    T.revoke("ghs_x", lambda *a, **k: (401, None))
    with pytest.raises(T.TokenError):
        T.revoke("ghs_x", lambda *a, **k: (500, None))
    with pytest.raises(T.TokenError, match="No token"):
        T.revoke("", gh)


def test_token_is_masked_and_never_printed(tmp_path, capsys, monkeypatch, keys):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    for k, v in (("GH_APP_ID", "1"), ("GH_APP_INSTALLATION_ID", "2"), ("GH_APP_PRIVATE_KEY", keys[0])):
        monkeypatch.setenv(k, v)
    real = T.mint
    monkeypatch.setattr(T, "mint", lambda a, i, k, r: real(a, i, k, r, http_fn=FakeGitHub()))
    assert T.main(["mint", "--repo", REPOS[0], "--repo", REPOS[1]]) == 0
    printed = capsys.readouterr().out
    token = "ghs_FAKEINSTALLATIONTOKEN0123456789abcdef"
    assert f"::add-mask::{token}" in printed
    assert printed.count(token) == 1, "the token may appear ONLY inside the ::add-mask:: command"
    assert "(value hidden)" in printed
    assert out.read_text().strip() == f"token={token}"


def test_main_refuses_to_print_token_without_github_output(monkeypatch, capsys, keys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    for k, v in (("GH_APP_ID", "1"), ("GH_APP_INSTALLATION_ID", "2"), ("GH_APP_PRIVATE_KEY", keys[0])):
        monkeypatch.setenv(k, v)
    real = T.mint
    monkeypatch.setattr(T, "mint", lambda a, i, k, r: real(a, i, k, r, http_fn=FakeGitHub()))
    assert T.main(["mint", "--repo", REPOS[0]]) == 2
    assert "ghs_FAKE" not in capsys.readouterr().out


def test_main_missing_secrets_is_an_error_not_a_crash(monkeypatch, capsys):
    for k in ("GH_APP_ID", "GH_APP_INSTALLATION_ID", "GH_APP_PRIVATE_KEY"):
        monkeypatch.delenv(k, raising=False)
    assert T.main(["mint", "--repo", REPOS[0]]) == 2
    assert "Missing required secret" in capsys.readouterr().out


# --------------------------------------------------------------------------- CI workflow static checks
@pytest.fixture(scope="module")
def ci():
    text = (ROOT / ".github" / "workflows" / "website-tests.yml").read_text()
    return text, yaml.safe_load(text)


def test_ci_permissions_are_minimal(ci):
    text, doc = ci
    assert doc["permissions"] == {"contents": "read"}
    for name, job in doc["jobs"].items():
        for k, v in (job.get("permissions") or {}).items():
            assert v == "read", f"job {name} requests {k}: {v}"
    assert not re.search(r"(contents|pull-requests|actions|packages|id-token):\s*write", text)


def test_ci_actions_are_pinned_to_exact_versions_or_shas(ci):
    text, _ = ci
    uses = re.findall(r"uses:\s*([^\s#]+)", text)
    assert uses
    for u in uses:
        assert re.search(r"@(v\d+\.\d+\.\d+|[0-9a-f]{40})$", u), f"{u} is not pinned to an exact version/SHA"


def test_ci_token_is_never_echoed_and_used_only_for_checkout_and_revoke(ci):
    text, doc = ci
    for line in text.splitlines():
        if "steps.app.outputs.token" in line:
            assert re.search(r"token:\s*\$\{\{ steps\.app\.outputs\.token \}\}|GH_APP_TOKEN:|steps\.app\.outputs\.token != ''", line), line
    assert not re.search(r"echo[^\n]*(token|GH_APP_PRIVATE_KEY)", text, re.I)
    steps = doc["jobs"]["discover"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout") and s.get("with", {}).get("repository")]
    assert len(checkouts) == 2
    assert {s["with"]["path"] for s in checkouts} == {"apps/nurseconnect", "apps/hospital"}
    assert all(s["with"]["persist-credentials"] is False for s in steps if str(s.get("uses", "")).startswith("actions/checkout"))
    assert any("revoke" in str(s.get("run", "")) and s.get("if") for s in steps), "token must be revoked"
    order = [i for i, s in enumerate(steps) if "github_app_token.py mint" in str(s.get("run", ""))]
    assert order and order[0] < steps.index(checkouts[0])


def test_ci_fails_safely_when_secrets_missing_and_gates_exist(ci):
    text, doc = ci
    assert "Missing required secret/variable" in text
    d = doc["jobs"]["discover"]["steps"]
    assert "exit 1" in d[0]["run"]
    assert any("check-approvals" in str(s.get("run", "")) for s in doc["jobs"]["agent-tests"]["steps"])
    w = doc["jobs"]["agent-write"]
    assert w["environment"] == "staging-write" and "allow_write" in w["if"] and "workflow_dispatch" in w["if"]
    assert any("verify-env" in str(s.get("run", "")) for s in w["steps"])
    assert "--read-only" in " ".join(str(s.get("run", "")) for s in doc["jobs"]["agent-tests"]["steps"])


def test_ci_scans_before_every_upload(ci):
    _, doc = ci
    for name, job in doc["jobs"].items():
        steps = job["steps"]
        for i, s in enumerate(steps):
            if str(s.get("uses", "")).startswith("actions/upload-artifact"):
                assert any("scan-artifacts" in str(p.get("run", "")) for p in steps[:i]), f"{name}: upload without scan"


def test_no_credentials_in_repo_files():
    from qa_agent.discovery import has_hard_secret
    skip = {".git", "artifacts", "__pycache__", ".pytest_cache"}
    bad = []
    for p in ROOT.rglob("*"):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & skip) and p.suffix in {".py", ".yml", ".yaml", ".md", ".txt", ".json", ".bat", ".example", ".ini"} \
                and "agent_tests" not in p.parts:
            cat = has_hard_secret(p.read_text(encoding="utf-8", errors="replace"))
            if cat:
                bad.append((str(p.relative_to(ROOT)), cat))
    assert not bad, bad
