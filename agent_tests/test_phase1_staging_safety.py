"""Phase 1: staging preflight, API/payment/database declarations, commit-SHA provenance. All offline."""
import json
import subprocess
from pathlib import Path

import pytest
import yaml

import agent
from config import Role
from qa_agent import preflight, provenance
from qa_agent.errors import SafetyBlocked
from qa_agent.reporting import write_reports
from qa_agent.safety import CONFIRM_PREFIX, check_write_allowed, load_environments, get_environment

STAGE = "https://stage.example.test"
API = "https://api-stage.example.test"
PROD = "https://prod.example.test"
PROD_API = "https://api.prod.example.test"

ROLES = {"consumer": Role("consumer", "nurseconnect", "T_CONS_EMAIL", "T_CONS_PW"),
         "nurse": Role("nurse", "nurseconnect", "T_NURSE_EMAIL", "T_NURSE_PW")}
GOOD_ENVIRON = {"T_CONS_EMAIL": "autotest+c@example.invalid", "T_CONS_PW": "x", "T_NURSE_EMAIL": "autotest+n@example.invalid", "T_NURSE_PW": "y",
                "PAY_ID": "rzp_test_ABC123", "PAY_SECRET": "whatever-secret-value", "DB_URL": "postgresql://u:p@staging-db.internal/nc_staging"}


def env_file(tmp_path, *, stage_extra=None, apps_extra=None, prod_extra=None, allowed=None):
    stage_app = {"base_url": STAGE, "api_base_url": API}
    stage_app.update(apps_extra or {})
    stage = {"kind": "staging", "allowed_origins": allowed if allowed is not None else [STAGE, API], "apps": {"nurseconnect": stage_app}}
    stage.update(stage_extra or {})
    prod = {"kind": "production", "apps": {"nurseconnect": {"base_url": PROD, "api_base_url": PROD_API}}}
    prod.update(prod_extra or {})
    p = tmp_path / "environments.yml"
    p.write_text(yaml.safe_dump({"version": 1, "environments": {"production": prod, "staging": stage}}), encoding="utf-8")
    return p


FULL = {"payment": {"provider": "razorpay", "mode": "sandbox", "key_id_env": "PAY_ID", "key_secret_env": "PAY_SECRET"},
        "database": {"url_env": "DB_URL", "name_pattern": "staging|test", "forbidden_hosts": ["prod-db.internal"]},
        "test_data": {"marker_prefix": "AUTOTEST_", "dedicated_accounts": True, "account_email_regex": "autotest\\+"},
        "api_verify": {"path": "/qa-environment.json", "json_key": "environment", "equals": "staging"},
        "verify": {"path": "/qa-environment.json", "json_key": "environment", "equals": "staging"}}


def status(checks, name):
    return next(c.status for c in checks if c.name == name)


# ---------------------------------------------------------------- environment parsing
def test_api_url_must_be_allowed_origin(tmp_path):
    with pytest.raises(SafetyBlocked, match="not in allowed_origins"):
        load_environments(env_file(tmp_path, allowed=[STAGE]))


def test_api_url_equal_to_production_is_refused(tmp_path):
    with pytest.raises(SafetyBlocked, match="production origin"):
        load_environments(env_file(tmp_path, apps_extra={"api_base_url": PROD_API}, allowed=[STAGE, PROD_API]))


def test_live_payment_mode_is_refused_at_load(tmp_path):
    with pytest.raises(SafetyBlocked, match="payment.mode"):
        load_environments(env_file(tmp_path, stage_extra={"payment": {"mode": "live", "key_id_env": "PAY_ID"}}))


def test_production_cannot_declare_payment(tmp_path):
    with pytest.raises(SafetyBlocked, match="production environments must not declare a payment"):
        load_environments(env_file(tmp_path, prod_extra={"payment": {"mode": "sandbox"}}))


def test_new_sections_must_be_mappings(tmp_path):
    with pytest.raises(SafetyBlocked, match="must be a mapping"):
        load_environments(env_file(tmp_path, stage_extra={"database": ["x"]}))


def test_old_environment_files_without_new_sections_still_load(tmp_path):
    env = get_environment("staging", env_file(tmp_path))
    assert env.payment == {} and env.database == {} and env.test_data == {}
    assert env.api_url("nurseconnect") == API


# ---------------------------------------------------------------- payment key classification + write gate
@pytest.mark.parametrize("key,expected", [("rzp_test_abc", "test"), ("rzp_live_abc", "live"), ("sk_live_x", "live"), ("sk_test_x", "test"),
                                          ("pk_live_x", "live"), ("something-else", "unknown"), ("", "unknown"), ("MY_LIVE_KEY", "live")])
def test_classify_payment_key(key, expected):
    assert preflight.classify_payment_key(key) == expected


def test_write_gate_refuses_live_looking_payment_key(tmp_path, monkeypatch):
    env = get_environment("staging", env_file(tmp_path, stage_extra=FULL))
    monkeypatch.setenv("PAY_ID", "rzp_live_SECRETSHOULDNOTPRINT")
    with pytest.raises(SafetyBlocked, match="not a sandbox") as ei:
        check_write_allowed(env, "nurseconnect", "wf_1", allow_write=True, confirm_phrase=CONFIRM_PREFIX + "staging",
                            confirmed_workflows=["wf_1"], fetch=lambda u: '{"environment": "staging"}')
    assert "SECRETSHOULDNOTPRINT" not in str(ei.value)          # the key value is never echoed


def test_write_gate_still_allows_a_clean_sandbox_setup(tmp_path, monkeypatch):
    env = get_environment("staging", env_file(tmp_path, stage_extra=FULL))
    monkeypatch.setenv("PAY_ID", "rzp_test_ok")
    check_write_allowed(env, "nurseconnect", "wf_1", allow_write=True, confirm_phrase=CONFIRM_PREFIX + "staging",
                        confirmed_workflows=["wf_1"], fetch=lambda u: '{"environment": "staging"}')


# ---------------------------------------------------------------- preflight
def run_pf(tmp_path, extra=FULL, environ=None, **kw):
    env = get_environment("staging", env_file(tmp_path, stage_extra=extra))
    return preflight.run_preflight(env, "nurseconnect", environ=GOOD_ENVIRON if environ is None else environ, roles=ROLES, **kw)


def test_preflight_ready_when_everything_is_configured(tmp_path):
    checks = run_pf(tmp_path, online=True, fetch=lambda u: '{"environment": "staging"}')
    assert preflight.is_ready(checks), preflight.format_checks(checks)
    assert status(checks, "frontend_probe") == "PASS" and status(checks, "api_probe") == "PASS"
    assert status(checks, "payment_sandbox") == "PASS" and status(checks, "database") == "PASS"
    assert status(checks, "test_credentials") == "PASS"
    assert status(checks, "test_data_isolation") == "NOT VERIFIED"          # declared, not provable: never reported as PASS


def test_preflight_offline_does_not_touch_network_and_marks_probes_not_verified(tmp_path):
    def boom(url):
        raise AssertionError("network used offline")
    checks = run_pf(tmp_path, fetch=boom)
    assert status(checks, "frontend_probe") == "NOT VERIFIED" and status(checks, "api_probe") == "NOT VERIFIED"


def test_preflight_probe_mismatch_and_error_fail(tmp_path):
    assert status(run_pf(tmp_path, online=True, fetch=lambda u: '{"environment": "production"}'), "frontend_probe") == "FAIL"
    def down(u):
        raise OSError("down")
    assert status(run_pf(tmp_path, online=True, fetch=down), "api_probe") == "FAIL"


def test_missing_api_url_and_payment_are_blocked_not_passed(tmp_path):
    env = get_environment("staging", env_file(tmp_path))
    object.__setattr__(env, "api_urls", {})
    checks = preflight.run_preflight(env, "nurseconnect", environ=GOOD_ENVIRON, roles=ROLES)
    assert status(checks, "api_base_url") == "BLOCKED" and status(checks, "payment_sandbox") == "BLOCKED"
    assert not preflight.is_ready(checks)


def test_preflight_live_key_fails_and_value_is_not_printed(tmp_path):
    environ = dict(GOOD_ENVIRON, PAY_ID="rzp_live_TOPSECRETVALUE")
    checks = run_pf(tmp_path, environ=environ)
    assert status(checks, "payment_sandbox") == "FAIL"
    assert "TOPSECRETVALUE" not in preflight.format_checks(checks)


def test_payment_key_not_set_is_blocked(tmp_path):
    environ = {k: v for k, v in GOOD_ENVIRON.items() if k != "PAY_ID"}
    assert status(run_pf(tmp_path, environ=environ), "payment_sandbox") == "BLOCKED"


def test_unrecognised_key_format_is_not_verified(tmp_path):
    assert status(run_pf(tmp_path, environ=dict(GOOD_ENVIRON, PAY_ID="abc123")), "payment_sandbox") == "NOT VERIFIED"


def test_database_checks(tmp_path):
    prod_db = dict(GOOD_ENVIRON, DB_URL="postgresql://u:p@prod-db.internal/nc_staging")
    assert status(run_pf(tmp_path, environ=prod_db), "database") == "FAIL"
    wrong_name = dict(GOOD_ENVIRON, DB_URL="postgresql://u:p@db2.internal/nc_main")
    assert status(run_pf(tmp_path, environ=wrong_name), "database") == "FAIL"
    assert status(run_pf(tmp_path, environ={k: v for k, v in GOOD_ENVIRON.items() if k != "DB_URL"}), "database") == "BLOCKED"
    assert status(run_pf(tmp_path, extra={}), "database") == "NOT VERIFIED"


def test_credentials_reported_by_role_name_only(tmp_path):
    environ = dict(GOOD_ENVIRON)
    environ.pop("T_NURSE_PW")
    checks = run_pf(tmp_path, environ=environ)
    c = next(c for c in checks if c.name == "test_credentials")
    assert c.status == "BLOCKED" and "nurse" in c.detail and "consumer" not in c.detail
    assert "autotest+" not in preflight.format_checks(checks)


def test_non_test_account_email_fails_the_pattern(tmp_path):
    environ = dict(GOOD_ENVIRON, T_CONS_EMAIL="real.person@gmail.com")
    c = next(c for c in run_pf(tmp_path, environ=environ) if c.name == "test_credentials")
    assert c.status == "FAIL" and "consumer" in c.detail and "real.person" not in c.detail


def test_production_environment_can_never_be_ready(tmp_path):
    env = get_environment("production", env_file(tmp_path))
    checks = preflight.run_preflight(env, "nurseconnect", environ=GOOD_ENVIRON, roles=ROLES)
    assert status(checks, "environment_kind") == "FAIL" and not preflight.is_ready(checks)


def test_quick_tunnel_host_is_flagged(tmp_path):
    t = "https://abc-def.trycloudflare.com"
    p = env_file(tmp_path, apps_extra={"base_url": t}, allowed=[t, API])
    checks = preflight.run_preflight(get_environment("staging", p), "nurseconnect", environ=GOOD_ENVIRON, roles=ROLES)
    assert status(checks, "frontend_url_stability") == "NOT VERIFIED"


def test_shipped_environments_file_still_loads_and_reports_missing_api_as_blocked():
    env = get_environment("staging")
    checks = preflight.run_preflight(env, "nurseconnect", environ={}, roles=ROLES)
    assert status(checks, "api_base_url") == "BLOCKED"          # not invented: the shipped file has no API URL yet
    assert not preflight.is_ready(checks)


# ---------------------------------------------------------------- CLI
def test_cli_preflight_exit_codes_and_json(tmp_path, capsys, monkeypatch):
    p = env_file(tmp_path, stage_extra=FULL)
    for k, v in GOOD_ENVIRON.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("config.ROLES", ROLES)
    monkeypatch.setattr("config.AGENT_ROLES", {})
    rc = agent.main(["preflight", "--env", "staging", "--app", "nurseconnect", "--env-file", str(p), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["ready"] is True
    monkeypatch.delenv("PAY_ID")
    assert agent.main(["preflight", "--env", "staging", "--app", "nurseconnect", "--env-file", str(p)]) == 2
    assert agent.main(["preflight", "--env", "nope", "--app", "nurseconnect", "--env-file", str(p)]) == 2


# ---------------------------------------------------------------- provenance
def make_repo(path, dirty=False, remote=None):
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (path / "a.txt").write_text("a")
    run("add", ".")
    run("commit", "-q", "-m", "init")
    if remote:
        run("remote", "add", "origin", remote)
    if dirty:
        (path / "b.txt").write_text("uncommitted")
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def test_git_info_clean_and_dirty(tmp_path):
    sha = make_repo(tmp_path / "clean", remote="https://user:tok3n@github.com/org/repo.git")
    rec = provenance.git_info("backend", tmp_path / "clean")
    assert rec["available"] and rec["sha"] == sha and len(sha) == 40 and rec["branch"] == "main" and rec["dirty"] is False
    assert "tok3n" not in rec["remote"] and "user" not in rec["remote"] and rec["remote"].endswith("/org/repo.git")
    make_repo(tmp_path / "dirty", dirty=True)
    d = provenance.git_info("frontend", tmp_path / "dirty")
    assert d["dirty"] is True and d["dirty_files"] == 1


def test_git_info_never_raises_and_never_stores_the_path(tmp_path):
    (tmp_path / "plain").mkdir()
    assert provenance.git_info("backend", tmp_path / "plain")["available"] is False
    assert provenance.git_info("backend", tmp_path / "missing")["reason"].startswith("path does not exist")
    assert provenance.git_info("Bad Name!", tmp_path)["available"] is False
    make_repo(tmp_path / "r")
    assert str(tmp_path) not in json.dumps(provenance.git_info("backend", tmp_path / "r"))


def test_parse_source_args_merges_env_and_flags():
    got = provenance.parse_source_args(["backend=/b"], environ={"QA_SOURCE_FRONTEND": "/f", "QA_SOURCE_BACKEND": "/old", "OTHER": "x"})
    assert got == {"frontend": "/f", "backend": "/b"}
    for bad in ("nopath", "Bad Name=/x"):
        with pytest.raises(ValueError):
            provenance.parse_source_args([bad], environ={})


# ---------------------------------------------------------------- reports carry the SHAs
def meta(**kw):
    m = {"run_id": "p1", "started": "t0", "finished": "t1", "mode": "dry-run", "environment": "staging", "environment_kind": "staging",
         "python": "3", "platform": "x", "browser": "chromium", "browser_version": "-"}
    m.update(kw)
    return m


def test_report_contains_commit_shas_and_flags_dirty_and_missing(tmp_path):
    sha = make_repo(tmp_path / "be")
    make_repo(tmp_path / "fe", dirty=True)
    sources = provenance.collect({"backend": tmp_path / "be", "frontend": tmp_path / "fe", "mobile": tmp_path / "nope"})
    paths = write_reports([], meta(sources=sources), tmp_path / "out")
    md = Path(paths["md"]).read_text(encoding="utf-8")
    html = Path(paths["html"]).read_text(encoding="utf-8")
    data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert sha in md and sha in html and data["meta"]["sources"]["backend"]["sha"] == sha
    assert "UNCOMMITTED CHANGES" in md and "UNAVAILABLE" in md
    assert str(tmp_path) not in md + html + json.dumps(data)


def test_report_without_sources_says_not_recorded(tmp_path):
    paths = write_reports([], meta(), tmp_path / "out")
    assert "NOT RECORDED" in Path(paths["md"]).read_text(encoding="utf-8")
    assert "NOT RECORDED" in Path(paths["html"]).read_text(encoding="utf-8")


def test_compile_report_records_provenance(tmp_path):
    from qa_agent.spec_compiler import compile_package
    spec = Path(__file__).resolve().parent.parent / "specifications" / "nurseconnect_public_smoke.yaml"
    sha = make_repo(tmp_path / "fe")
    prov = {"nurseconnect": provenance.git_info("nurseconnect", tmp_path / "fe")}
    rep = compile_package(spec, tmp_path / "drafts", provenance=prov)
    on_disk = json.loads((tmp_path / "drafts" / "compilation_report.json").read_text(encoding="utf-8"))
    assert rep["source_provenance"]["nurseconnect"]["sha"] == sha == on_disk["source_provenance"]["nurseconnect"]["sha"]
