"""The original pytest phases (tests/) still import, collect and run against a local server with the new safeguards active."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from agent_tests.conftest import ROOT, make_env_file
from qa_agent.legacy import load_legacy_workflows
from qa_agent.safety import CONFIRM_PREFIX


def run_py(args, env_extra, tmp_path, timeout=300):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("NURSE", "CONSUMER", "RECEPTION", "DOCTOR", "TEST_ENV", "ALLOW_WRITE", "STAGING_", "APP_ONLY", "QA_ENV"))}
    env.update({"ARTIFACTS_DIR": str(tmp_path / "artifacts"), "PYTHONDONTWRITEBYTECODE": "1", "HEADLESS": "true"})
    env.update(env_extra)
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)


def test_legacy_workflow_definitions_moved_to_yaml_unchanged():
    wfs = {w["id"]: w for w in load_legacy_workflows()}
    assert list(wfs) == ["reception_patient_registration", "reception_appointment", "doctor_open_record", "nurse_assignments_visits_docs", "consumer_booking"]
    assert wfs["reception_patient_registration"]["nav"] == ["OPD", "patient|registration|register"]
    assert wfs["reception_patient_registration"]["create"] == r"register|new patient|add patient|create|\+"
    assert wfs["doctor_open_record"]["open_first_row"] is True and wfs["doctor_open_record"]["create"] is None
    assert wfs["nurse_assignments_visits_docs"]["each_nav_is_page"] is True
    assert wfs["consumer_booking"]["payment"] is True
    src = (ROOT / "tests" / "test_07_workflows.py").read_text()
    assert "dict(id=" not in src, "workflow definitions must no longer be hard-coded in Python"


def test_invalid_legacy_yaml_is_rejected_with_clear_errors(tmp_path):
    from qa_agent.errors import SchemaError
    p = tmp_path / "l.yml"
    p.write_text("version: 1\nlegacy_workflows:\n  - {id: a, role: nurse, title: T, nav: ['(['], bogus: 1}\n")
    with pytest.raises(SchemaError) as e:
        load_legacy_workflows(p)
    assert any("invalid regex" in x for x in e.value.problems) and any("unknown key" in x for x in e.value.problems)


def test_all_legacy_test_files_still_collect(tmp_path):
    r = run_py(["-m", "pytest", "tests", "--collect-only", "-q", "-p", "no:cacheprovider"], {}, tmp_path)
    assert r.returncode == 0, r.stdout[-1500:] + r.stderr[-1500:]
    assert "test_workflow[" in r.stdout, "test_07 must still parametrize one test per workflow from the YAML data"
    assert re.search(r"\b31 tests collected", r.stdout), r.stdout[-300:]


def test_legacy_phases_run_against_local_server_with_guards_active(tmp_path, server):
    env = {"NURSECONNECT_URL": server, "NURSECONNECT_LOGIN_PATH": "/login", "APP_ONLY": "nurseconnect", "NAV_TIMEOUT_MS": "8000", "LOGIN_TIMEOUT_S": "5"}
    r = run_py(["-m", "pytest", "tests/test_01_health.py", "tests/test_07_workflows.py", "-p", "no:cacheprovider", "-q"], env, tmp_path)
    out = r.stdout + r.stderr
    assert "Could not attach the write guard" not in out
    reports = list((tmp_path / "artifacts" / "reports").glob("report_*.json"))
    assert reports, out[-2000:]
    data = json.loads(reports[0].read_text())
    tests = {s["test"] for s in data["steps"]}
    assert any(t.startswith("01_health") for t in tests)
    assert any(t.startswith("07_workflows") for t in tests), "workflow tests (now YAML-driven) must still record steps"
    statuses = {s["status"] for s in data["steps"]}
    assert statuses <= {"PASS", "FAIL", "BLOCKED", "NOT VERIFIED", "NOT TESTED"}
    assert any(s["status"] == "BLOCKED" and "NURSE_EMAIL" in s["actual"] for s in data["steps"]), "missing credentials stay BLOCKED, as before"


def cfg_allow(tmp_path, env):
    r = run_py(["-c", "import config; print('ALLOW_WRITE=' + str(config.ALLOW_WRITE))"], env, tmp_path)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip().split("=")[-1] == "True", r.stderr


def test_config_allow_write_is_only_a_request_until_staging_is_verified(tmp_path, server):
    envfile = make_env_file(tmp_path, server)
    base = {"NURSECONNECT_URL": server, "HOSPITAL_URL": server, "QA_ENV_FILE": str(envfile)}
    ok, err = cfg_allow(tmp_path, {**base, "ALLOW_WRITE_TESTS": "true"})
    assert not ok and "TEST_ENV is not set" in err
    ok, err = cfg_allow(tmp_path, {**base, "ALLOW_WRITE_TESTS": "true", "TEST_ENV": "localtest"})
    assert not ok and "STAGING_WRITE_CONFIRMATION" in err
    ok, err = cfg_allow(tmp_path, {**base, "ALLOW_WRITE_TESTS": "true", "TEST_ENV": "production", "STAGING_WRITE_CONFIRMATION": CONFIRM_PREFIX + "production"})
    assert not ok and "production is read-only" in err
    ok, err = cfg_allow(tmp_path, {**base, "ALLOW_WRITE_TESTS": "true", "TEST_ENV": "nope"})
    assert not ok and "Unknown environment" in err
    ok, err = cfg_allow(tmp_path, {**base, "NURSECONNECT_URL": "http://localhost:" + server.rsplit(":", 1)[1], "ALLOW_WRITE_TESTS": "true", "TEST_ENV": "localtest",
                                    "STAGING_WRITE_CONFIRMATION": CONFIRM_PREFIX + "localtest"})
    assert not ok and "not in the staging allowlist" in err
    ok, err = cfg_allow(tmp_path, {**base, "ALLOW_WRITE_TESTS": "false", "TEST_ENV": "localtest", "STAGING_WRITE_CONFIRMATION": CONFIRM_PREFIX + "localtest"})
    assert not ok
    ok, err = cfg_allow(tmp_path, {**base, "ALLOW_WRITE_TESTS": "true", "TEST_ENV": "localtest", "STAGING_WRITE_CONFIRMATION": CONFIRM_PREFIX + "localtest"})
    assert ok, err


def test_cli_validate_run_and_gates_end_to_end(tmp_path, server):
    envfile = make_env_file(tmp_path, server)
    r = run_py(["agent.py", "validate", "--environments", str(envfile)], {}, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    r = run_py(["agent.py", "run", "--dry-run", "--env", "production", "--env-file", str(envfile)], {}, tmp_path)
    assert "BLOCKED" in r.stdout and "NOT APPROVED" in r.stdout and r.returncode == 0
    r = run_py(["agent.py", "run", "--dry-run", "--fail-on-blocked", "--env", "production", "--env-file", str(envfile)], {}, tmp_path)
    assert r.returncode == 4
    r = run_py(["agent.py", "run", "--env", "does-not-exist", "--env-file", str(envfile)], {}, tmp_path)
    assert r.returncode == 2 and "fail closed" in r.stdout
    bad = tmp_path / "bad.yml"
    bad.write_text("version: 1\nworkflows:\n  - {id: bad_one_x, app: nurseconnect, title: t, description: d, status: approved, risk: low, writes: false, steps: [{action: evaluate, code: '1'}]}\n")
    r = run_py(["agent.py", "run", "--file", str(bad), "--dry-run"], {}, tmp_path)
    assert r.returncode == 2 and "unknown action 'evaluate'" in r.stdout and "nothing was run" in r.stdout


def test_cli_discover_draft_approve_flow_in_a_sandbox(tmp_path):
    """discover -> draft -> (drafts are inert) -> approve -> approved copy exists. Uses a throw-away copy of the approvals dir."""
    import shutil
    work = tmp_path / "proj"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "artifacts", "__pycache__", "workflow_drafts", "agent_tests", ".pytest_cache"))
    (work / "workflow_drafts").mkdir()
    repo = work / "apps" / "nurseconnect" / "src"
    repo.mkdir(parents=True)
    (repo / "App.jsx").write_text('<Routes><Route path="/partner/visits" element={<Visits />} /></Routes>\n')
    (repo / "Visits.jsx").write_text('export default function Visits(){ return <form><TextField label="Search" name="q" /><Button type="submit">Find</Button></form> }\n')

    def cli(*a, env=None):
        e = {k: v for k, v in os.environ.items() if k != "CI"}
        e["ARTIFACTS_DIR"] = str(work / "artifacts")
        return subprocess.run([sys.executable, "agent.py", *a], cwd=work, env=e, capture_output=True, text=True, timeout=120)
    r = cli("discover", "--app", "nurseconnect=apps/nurseconnect", "--strict")
    assert r.returncode == 0 and "1 routes" in r.stdout, r.stdout + r.stderr
    r = cli("draft")
    assert r.returncode == 0 and "CANNOT run until approved" in r.stdout
    drafts = sorted((work / "workflow_drafts" / "nurseconnect").glob("*.yml"))
    assert len(drafts) >= 2
    assert (work / "workflow_drafts" / "nurseconnect" / "INDEX.md").is_file()
    r = cli("run", "--dry-run")
    assert not any(d.stem in r.stdout for d in drafts), "draft workflows must never be picked up by the runner"
    nav = next(d for d in drafts if d.name.startswith("nav_"))
    r = cli("approve", "--draft", str(nav), "--by", "Tester", "--reason", "reviewed in the sandbox")
    assert r.returncode == 0 and "Approved:" in r.stdout, r.stdout + r.stderr
    assert (work / "workflows_approved" / nav.name).is_file()
    r = cli("approve", "--draft", str(nav), "--by", "Tester", "--reason", "again reviewed")
    assert r.returncode == 2 and "already exists" in r.stdout
    r = cli("validate")
    assert r.returncode == 0, r.stdout
    r = cli("inventory")
    assert nav.stem in r.stdout and "approved" in r.stdout
    other = next(d for d in drafts if d is not nav)
    assert other.stem in cli("inventory").stdout and "draft" in cli("inventory").stdout
