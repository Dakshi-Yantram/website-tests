"""The login agent against a LOCAL mock of the login page (agent_tests/login_mock.py), in real Chromium.
No network, no real credentials. These tests prove the agent's logic, gates and reports - not the live site."""
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

from agent_tests.login_mock import MockSite
from login_agent import cli
from login_agent.errors import SafetyBlocked
from login_agent.redaction import clear_registered_secrets
from login_agent.guard import NetworkGuard, PostBudget
from login_agent.plan import DEFAULT_FAKE_ID, PlanError, load_plan
from login_agent.report import write_reports
from login_agent.runner import resolve_target, run_suite

CONSUMER_PW, WORKER_PW = "Zq9-Consumer-Secret-A1", "Zq9-Worker-Secret-B2"
USERS = {"consumer@test.local": (CONSUMER_PW, "consumer"), "worker@test.local": (WORKER_PW, "worker")}
ALL_OK_EXCEPT = {"LOGIN-019"}          # the real markup has no <label for> / aria-label: the a11y test is EXPECTED to fail


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("LOGIN_TEST_BASE_URL", "LOGIN_TEST_API_ORIGIN", "CI", "NC_TEST_INVALID_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NC_TEST_CONSUMER_EMAIL", "consumer@test.local")
    monkeypatch.setenv("NC_TEST_CONSUMER_PASSWORD", CONSUMER_PW)
    monkeypatch.setenv("NC_TEST_WORKER_EMAIL", "worker@test.local")
    monkeypatch.setenv("NC_TEST_WORKER_PASSWORD", WORKER_PW)
    yield
    clear_registered_secrets()


def _fake_id(tmp_path, **limits):
    d = tmp_path / "fake-id"
    shutil.copytree(DEFAULT_FAKE_ID, d)
    if limits:
        f = d / "test-data.yaml"
        data = yaml.safe_load(f.read_text(encoding="utf-8"))
        data["limits"].update(limits)
        f.write_text(yaml.safe_dump(data), encoding="utf-8")
    return d


class Rig:
    def __init__(self, tmp_path, **flags):
        self.tmp = tmp_path
        self.prod = MockSite(host="localhost").start()                 # pretends to be production
        self.loc = MockSite(users=USERS, **flags).start()              # pretends to be staging/local
        self.env_file = tmp_path / "env.yml"
        self.env_file.write_text(yaml.safe_dump({"version": 1, "environments": {
            "production": {"kind": "production", "apps": {"nurseconnect": {"base_url": self.prod.url}}},
            "local": {"kind": "local", "allowed_origins": [self.loc.url], "apps": {"nurseconnect": {"base_url": self.loc.url}}}}}))

    def run(self, env, plan=None, **kw):
        plan = plan or load_plan(fake_id_dir=_fake_id(self.tmp, login_wait_ms=3000))
        tgt = resolve_target(env, self.env_file)
        out = self.tmp / f"out-{env}"
        return run_suite(plan, tgt, out_dir=out, log=lambda m: None, **kw), tgt, out

    def stop(self):
        for s in (self.prod, self.loc):
            try:
                s.stop()
            except Exception:
                pass


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.stop()


def by_id(results):
    return {r.id: r for r in results}


# ------------------------------------------------------------------------------------------------ behaviour
def test_production_is_read_only_and_credential_tests_are_not_tested(rig):
    results, tgt, _ = rig.run("production")
    r = by_id(results)
    assert tgt.env_kind == "production"
    for tid in ("LOGIN-005", "LOGIN-006", "LOGIN-014", "LOGIN-016", "LOGIN-017", "LOGIN-018"):
        assert r[tid].status == "NOT TESTED" and "never run on production" in r[tid].reason, tid
    for tid, t in r.items():
        if t.requires == "anonymous" and tid not in ALL_OK_EXCEPT:
            assert t.status == "PASS", (tid, t.reason)
    assert rig.prod.posts() == [], "no POST of any kind may reach a production server"
    assert any("blocked by the agent" in n for n in r["LOGIN-004"].notes), "the blocked empty-form login POST must be reported"


def test_staging_full_run(rig):
    results, tgt, _ = rig.run("local")
    r = by_id(results)
    for tid, t in r.items():
        if tid in ALL_OK_EXCEPT:
            assert t.status == "FAIL" and "password_toggle" in t.reason
        else:
            assert t.status == "PASS", (tid, t.reason)
    assert set(rig.loc.posts()) == {"/api/auth/login"}, "the login request is the only write that may be sent"
    assert len(rig.loc.posts()) <= 14
    assert len(r["LOGIN-016"].items) == 3 and all(i["status"] == "PASS" for i in r["LOGIN-016"].items)


def test_open_redirect_bug_is_detected_with_screenshot(tmp_path):
    rig = Rig(tmp_path, vulnerable_redirect=True)
    try:
        results, _, out = rig.run("local", only=["LOGIN-009", "LOGIN-016"])
        r = by_id(results)
        assert r["LOGIN-009"].status == "PASS"               # the bug only shows after login
        assert r["LOGIN-016"].status == "FAIL"
        # Screenshots are checked through the real CLI in test_login_agent_selfcontained.py: the project's root conftest.py
        # installs a process-wide Page.screenshot guard that suppresses captures inside pytest.
    finally:
        rig.stop()


def test_account_enumeration_is_detected(tmp_path):
    rig = Rig(tmp_path, leaky_errors=True)
    try:
        results, _, _ = rig.run("local", only=["LOGIN-005"])
        assert results[0].status == "FAIL" and "must not appear" in results[0].reason
    finally:
        rig.stop()


def test_missing_credentials_block_but_never_fail(rig, monkeypatch):
    for k in ("NC_TEST_CONSUMER_PASSWORD", "NC_TEST_WORKER_PASSWORD"):
        monkeypatch.delenv(k)
    results, _, _ = rig.run("local")
    r = by_id(results)
    for tid in ("LOGIN-006", "LOGIN-014", "LOGIN-016", "LOGIN-017", "LOGIN-018"):
        assert r[tid].status == "BLOCKED" and "NC_TEST_" in r[tid].reason and CONSUMER_PW not in r[tid].reason
    assert r["LOGIN-005"].status == "PASS"                    # the wrong-password fixture needs no secret


def test_unreachable_target_is_blocked_fast(rig):
    rig.loc.stop()
    results, _, _ = rig.run("local")
    assert {t.status for t in results} == {"BLOCKED"}
    assert "not reachable" in results[0].reason


def test_login_budget_is_enforced(tmp_path):
    rig = Rig(tmp_path)
    try:
        plan = load_plan(fake_id_dir=_fake_id(tmp_path, login_wait_ms=2000, max_login_posts_per_run=2))
        results, _, _ = rig.run("local", plan=plan, only=["LOGIN-006", "LOGIN-014", "LOGIN-017"])
        assert len(rig.loc.posts()) == 2
        assert any("budget" in (n or "") for t in results for n in t.notes)
    finally:
        rig.stop()


# ------------------------------------------------------------------------------------------------ reports
def test_reports_are_complete_and_contain_no_secrets(rig):
    results, tgt, out = rig.run("local")
    meta = {"suite": "nurseconnect-login", "environment": "local", "env_kind": "local", "base_origin": tgt.base_origin,
            "run_id": "t", "started": "now", "agent_version": "test"}
    paths = write_reports(results, meta, out)
    blob = ""
    for p in paths.values():
        blob += Path(p).read_text(encoding="utf-8")
    for secret in (CONSUMER_PW, WORKER_PW, "consumer@test.local", "worker@test.local"):
        assert secret not in blob, f"{secret!r} leaked into a report"
    data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert data["summary"]["PASS"] >= 17 and data["summary"]["FAIL"] == 1
    suite = ET.parse(paths["junit"]).getroot()
    assert suite.get("failures") == "1" and int(suite.get("tests")) == len(results)
    assert "LOGIN-019" in Path(paths["md"]).read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ guard / safety / plan
def test_guard_decisions():
    ok_origin = "http://127.0.0.1:1"
    g = NetworkGuard("local", {ok_origin}, PostBudget(1))
    assert g.decide("GET", "https://anything.example/x")[0]
    assert not g.decide("POST", ok_origin + "/api/auth/login")[0]               # test did not opt in
    g.login_post_allowed = True
    assert g.decide("POST", ok_origin + "/api/auth/login")[0]
    assert not g.decide("POST", "http://127.0.0.1:2/api/auth/login")[0]         # wrong origin
    assert not g.decide("POST", ok_origin + "/api/auth/register")[0]            # not the login endpoint
    assert not g.decide("DELETE", ok_origin + "/api/auth/login")[0]
    g.budget.used = 1
    assert "budget" in g.decide("POST", ok_origin + "/api/auth/login")[1]
    prod = NetworkGuard("production", {ok_origin}, PostBudget(5))
    prod.login_post_allowed = True
    assert prod.decide("POST", ok_origin + "/api/auth/login") == (False, "production is read-only")


def test_target_resolution_fails_closed(rig, tmp_path):
    with pytest.raises(SafetyBlocked):
        resolve_target("production", rig.env_file, "https://not-production.example")        # other origin under production
    with pytest.raises(SafetyBlocked):
        resolve_target("local", rig.env_file, rig.prod.url)                                  # outside the staging allowlist
    with pytest.raises(SafetyBlocked):
        resolve_target("nope", rig.env_file)
    bad = tmp_path / "bad.yml"
    bad.write_text(yaml.safe_dump({"version": 1, "environments": {
        "production": {"kind": "production", "apps": {"nurseconnect": {"base_url": rig.prod.url}}},
        "local": {"kind": "local", "allowed_origins": [rig.loc.url], "auth_origins": [rig.prod.url],
                  "apps": {"nurseconnect": {"base_url": rig.loc.url}}}}}))
    with pytest.raises(SafetyBlocked, match="production"):
        resolve_target("local", bad)                                                         # login POST allowlist names production


def _plan_with(tmp_path, mutate):
    raw = yaml.safe_load((Path(__file__).resolve().parent.parent / "login_tests" / "login-test-plan.yaml").read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "plan.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r["tests"][0]["steps"].append({"action": "teleport"}), "unknown action"),
    (lambda r: r["tests"][0]["steps"].append({"action": "click", "target": "no_such_selector"}), "unknown selector"),
    (lambda r: r["tests"][0]["steps"].append({"action": "click", "target": "login_button", "tpyo": 1}), "does not accept"),
    (lambda r: r["tests"][0].update(identity="ghost"), "unknown identity"),
    (lambda r: r["tests"][0].update(severity="Huge"), "severity"),
    (lambda r: r["tests"].append(dict(r["tests"][0])), "duplicate"),
])
def test_plan_validation(tmp_path, mutate, msg):
    with pytest.raises(PlanError, match=msg):
        load_plan(_plan_with(tmp_path, mutate))


# ------------------------------------------------------------------------------------------------ CLI
def test_cli_exit_codes_and_dry_run(rig, tmp_path, capsys):
    base = ["--env-file", str(rig.env_file), "--fake-id", str(_fake_id(tmp_path, login_wait_ms=3000)), "--out", str(tmp_path / "cli-out")]
    assert cli.main(base + ["--env", "local", "--dry-run"]) == 0
    assert "WILL RUN" in capsys.readouterr().out
    assert cli.main(base + ["--env", "local", "--only", "LOGIN-001,LOGIN-019"]) == 1                  # a FAIL -> exit 1
    assert cli.main(base + ["--env", "local", "--only", "LOGIN-001,LOGIN-019", "--fail-on", "medium"]) == 0   # Low severity ignored
    assert (tmp_path / "cli-out" / "login_report.html").is_file()
    assert cli.main(base + ["--env", "does-not-exist"]) == 2
    out = capsys.readouterr().out
    assert "SUMMARY" in out
