"""CI gating: honest verdicts, block classification, critical NOT VERIFIED, workflow YAML wiring."""
import re
from types import SimpleNamespace as R
from pathlib import Path

import pytest
import yaml

from qa_agent import verdict
from qa_agent.errors import StepBlocked

CRIT = (("high",), frozenset({"booking"}))
ROOT = Path(__file__).resolve().parent.parent


def res(i, status, reason=""):
    return R(id=i, status=status, reason=reason)


WFS = [{"id": i, "app": "hospital", "risk": r} for i, r in (("a", "low"), ("hi", "high"), ("booking", "medium"), ("lo", "low"))]


def analyse(results):
    return verdict.analyse(results, WFS, critical=CRIT)


# ---- classification (24 credential blocks must be separable from the 1 unapproved draft)
def test_block_kinds():
    assert verdict.block_kind("step 0 (login): Credentials missing: set HIMS_LAB_EMAIL and HIMS_LAB_PASSWORD") == "credentials"
    assert verdict.block_kind("NOT APPROVED (draft): status is not 'approved'") == "unapproved"
    assert verdict.block_kind("WRITE BLOCKED: read-only") == "policy"
    assert verdict.block_kind("Browser execution could not start or finish: X") == "other"


def test_runner_reason_strings_map_to_expected_kinds(monkeypatch):
    """Guards against the runner changing its wording without the classifier following."""
    import config
    from qa_agent.runner import default_login
    role = config.Role("x", "hospital", "NOPE_EMAIL_X", "NOPE_PASSWORD_X")
    monkeypatch.setitem(config.ROLES, "x", role)
    with pytest.raises(StepBlocked) as e:
        default_login(None, "x", "http://localhost")
    assert verdict.block_kind(str(e.value)) == "credentials"
    with pytest.raises(StepBlocked) as e:
        default_login(None, "no_such_role_zz", "http://localhost")
    assert verdict.block_kind(str(e.value)) == "credentials"


def test_unapproved_draft_does_not_fail_fail_on_blocked_but_credentials_do():
    only_draft = analyse([res("a", "PASS"), res("lo", "BLOCKED", "NOT APPROVED (draft): x")])
    assert verdict.decide_exit(only_draft, fail_on_blocked=True) == 4                              # legacy default: strict
    assert verdict.decide_exit(only_draft, fail_on_blocked=True, ignore_blocked=("unapproved",)) == 0  # CI opt-in
    creds = analyse([res("a", "PASS"), res("lo", "BLOCKED", "step 0 (login): Credentials missing: set A and B")])
    assert verdict.decide_exit(creds, fail_on_blocked=True) == 4
    assert verdict.decide_exit(creds, fail_on_blocked=True, ignore_blocked=("unapproved",)) == 4  # credentials never ignored
    assert verdict.decide_exit(creds) == 0                      # flag off keeps old behaviour


def test_unknown_block_reason_fails_closed():
    a = analyse([res("a", "BLOCKED", "something new")])
    assert verdict.decide_exit(a, fail_on_blocked=True) == 4


# ---- critical NOT VERIFIED
def test_critical_not_verified_by_risk_and_by_id_fail():
    for crit_id in ("hi", "booking"):
        a = analyse([res("a", "PASS"), res(crit_id, "NOT VERIFIED", "x")])
        assert verdict.decide_exit(a, fail_on_critical_unverified=True) == 6


def test_noncritical_not_verified_passes_unless_incomplete_flag():
    a = analyse([res("a", "PASS"), res("lo", "NOT VERIFIED", "x")])
    assert verdict.decide_exit(a, fail_on_critical_unverified=True) == 0
    assert verdict.decide_exit(a, fail_on_incomplete=True) == 5


def test_fail_beats_everything_and_all_pass_is_zero():
    assert verdict.decide_exit(analyse([res("a", "FAIL"), res("hi", "NOT VERIFIED")]), fail_on_blocked=True) == 1
    assert verdict.decide_exit(analyse([res("a", "PASS"), res("hi", "PASS")]), fail_on_blocked=True,
                               fail_on_incomplete=True, fail_on_critical_unverified=True) == 0


def test_verdict_never_says_passed_when_something_is_unverified():
    out = []
    code = verdict.finish([res("a", "PASS"), res("lo", "NOT VERIFIED", "x")], WFS, say=out.append)
    text = "\n".join(out)
    assert code == 0 and "NOT FULLY TESTED" in text and "PASSED" not in text.split("VERDICT:")[1].split("-")[0]
    out = []
    verdict.finish([res("a", "PASS"), res("hi", "PASS")], WFS, say=out.append)
    assert "VERDICT: PASSED" in out[0]


def test_step_summary_written(tmp_path, monkeypatch):
    f = tmp_path / "s.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(f))
    verdict.finish([res("a", "PASS"), res("hi", "NOT VERIFIED")], WFS, say=lambda s: None)
    assert "QA agent verdict" in f.read_text()


# ---- shipped config + workflow YAML wiring
def test_shipped_critical_config_loads_and_includes_booking():
    levels, ids = verdict.load_critical()
    assert "high" in levels and "consumer_booking_flow_readonly" in ids


def _wf():
    return yaml.safe_load((ROOT / ".github/workflows/website-tests.yml").read_text())


def _step(name_part):
    return [s for s in _wf()["jobs"]["agent-tests"]["steps"] if name_part in s.get("name", "")][0]


def _hims_names():
    import config
    names = set()
    for r in config.AGENT_ROLES.values():
        for n in (r.email_env, r.password_env):
            if n.startswith("HIMS_"):
                names.add(n)
    return names


def test_workflow_applies_fail_flags_without_workflow_spec():
    run = _step("Run approved workflows")["run"]
    first = run.split("if [[ -n \"$WORKFLOW_SPEC\" ]]")[0]
    assert "--fail-on-blocked" in first and "--fail-on-critical-unverified" in first
    assert "--ignore-blocked unapproved" in first and "credentials" not in first.split("--ignore-blocked")[1].split()[0]


def test_fail_on_incomplete_is_controlled_only_by_the_repository_variable():
    run = _step("Run approved workflows")["run"]
    assert run.count("--fail-on-incomplete") == 1, "must be added in exactly one place"
    guard = run.split("--fail-on-incomplete")[0].rstrip().splitlines()[-1]
    assert 'FAIL_ON_INCOMPLETE" == "true"' in guard
    env = _wf()["jobs"]["agent-tests"]["env"]
    assert env["FAIL_ON_INCOMPLETE"] == "${{ vars.AGENT_FAIL_ON_INCOMPLETE }}"


def test_hims_credentials_use_the_hospital_only_expression_and_are_all_present():
    names = _hims_names()
    assert len(names) == 16
    pattern = re.compile(r"^\$\{\{ matrix\.app == 'hospital' && secrets\.(HIMS_[A-Z_]+) \|\| '' \}\}$")
    for step_name in ("Run approved workflows", "Redact and scan reports"):
        env = _step(step_name)["env"]
        for n in names:
            m = pattern.match(env[n])
            assert m and m.group(1) == n, f"{step_name}: {n} must be exactly the hospital-only expression"


def _eval_hims_expr(expr, matrix_app, secrets):
    """Tiny evaluator for the single expression shape we allow: `matrix.app == 'hospital' && secrets.X || ''` (GitHub && / || semantics)."""
    m = re.match(r"^\$\{\{ matrix\.app == 'hospital' && secrets\.(\w+) \|\| '' \}\}$", expr)
    assert m
    cond = matrix_app == "hospital"
    val = secrets.get(m.group(1), "")
    return (val if val else "") if cond else ""


def test_hims_values_are_empty_for_the_nurseconnect_leg_even_if_secrets_exist():
    secrets = {n: "SENTINEL-" + n for n in _hims_names()}
    env = _step("Run approved workflows")["env"]
    for n in _hims_names():
        assert _eval_hims_expr(env[n], "nurseconnect", secrets) == ""
        assert _eval_hims_expr(env[n], "hospital", secrets) == "SENTINEL-" + n


def test_hims_secrets_appear_nowhere_else_in_the_workflow_file():
    text = (ROOT / ".github/workflows/website-tests.yml").read_text()
    wf = _wf()
    # only the two named steps of agent-tests may reference them; not job env, not other jobs/steps
    assert not any(k.startswith("HIMS_") for k in wf["jobs"]["agent-tests"]["env"])
    for job, body in wf["jobs"].items():
        for s in body.get("steps", []):
            has = any(k.startswith("HIMS_") for k in s.get("env", {}))
            allowed = job == "agent-tests" and s.get("name", "").startswith(("Run approved workflows", "Redact and scan reports"))
            assert has == allowed, f"{job}: {s.get('name') or s.get('uses')}"
        assert not any(k.startswith("HIMS_") for k in body.get("env", {})), job
    assert text.count("secrets.HIMS_") == 32          # 16 names x 2 steps, nothing more
    assert "HIMS_" not in "".join(l for l in text.splitlines() if "run:" in l and "echo" in l)   # never echoed


def test_hims_values_are_redacted_by_the_scanner(monkeypatch, tmp_path):
    from qa_agent import redaction
    for n in _hims_names():
        monkeypatch.setenv(n, "Zq9-" + n.lower() + "-value")
    f = tmp_path / "report.txt"
    f.write_text("login ok " + "Zq9-hims_lab_password-value", encoding="utf-8")
    assert "Zq9-hims_lab_password-value" not in redaction.redact(f.read_text())


def test_artifact_steps_keep_reports_and_split_evidence():
    steps = [s for s in _wf()["jobs"]["agent-tests"]["steps"] if s.get("uses", "").startswith("actions/upload-artifact")]
    rep = [s for s in steps if s["with"]["name"].startswith("agent-report-")][0]["with"]
    ev = [s for s in steps if s["with"]["name"].startswith("agent-evidence-")][0]["with"]
    for ext in ("html", "json"):
        assert f"artifacts/reports/*.{ext}" in rep["path"]
        assert f"!artifacts/reports/*.{ext}" in ev["path"]
    assert rep["retention-days"] > ev["retention-days"] and rep["if-no-files-found"] == "warn"


def test_existing_nurseconnect_env_untouched():
    env = _wf()["jobs"]["agent-tests"]["env"]
    for k in ("NURSE_EMAIL", "CONSUMER_EMAIL", "RECEPTION_EMAIL", "DOCTOR_EMAIL", "NURSECONNECT_LOGIN_PATH"):
        assert k in env


# ---- item 1: critical list must fail safely
@pytest.mark.parametrize("content", [None, "", "- a\n- b\n", "risk_levels: []\n", "risk_levels: high\n", "risk_levels: [high]\nids: [1]\n",
                                     "risk_levels: [high]\nextra: 1\n", "risk_levels: [high\n", "ids: [a]\n",
                                     "risk_levels: [critical]\n", "risk_levels: [high, critical]\n", "risk_levels: [HIGH]\n",
                                     "risk_levels: [urgent, medium]\n", "risk_levels: [low, 5]\n", "risk_levels: ['']\n"])
def test_invalid_or_missing_critical_config_fails_closed(tmp_path, content):
    p = tmp_path / "critical.yml"
    if content is not None:
        p.write_text(content, encoding="utf-8")
    with pytest.raises(verdict.CriticalConfigError):
        verdict.load_critical(p)
    out = []
    code = verdict.finish([res("a", "PASS")], WFS, say=out.append, fail_on_critical_unverified=True, critical_path=p)
    assert code == 2 and "FAILING CLOSED" in "\n".join(out).upper()
    out = []     # flag off: warn and keep the default risk levels instead of crashing
    assert verdict.finish([res("a", "PASS"), res("hi", "NOT VERIFIED")], WFS, say=out.append, critical_path=p) == 0
    assert any("WARNING" in x for x in out)


def test_valid_config_roundtrip_and_unknown_ids_are_reported(tmp_path):
    p = tmp_path / "critical.yml"
    p.write_text("risk_levels: [high]\nids: [booking, ghost]\n", encoding="utf-8")
    assert verdict.load_critical(p) == (("high",), frozenset({"booking", "ghost"}))
    assert verdict.check_known_ids(WFS, p) == ["critical workflow id 'ghost' matches no loaded workflow"]


def test_shipped_critical_ids_all_exist_in_the_repo():
    from qa_agent.workflows import load_all
    assert verdict.check_known_ids(load_all()) == []


# ---- item 2: critical workflows that never produced a result
def test_missing_critical_workflow_is_detected_and_fails():
    a = verdict.analyse([res("a", "PASS")], WFS, critical=CRIT, apps=("nurseconnect", "hospital"))
    assert a["missing_critical"] == ["booking", "hi"]
    assert verdict.decide_exit(a, fail_on_critical_unverified=True) == 6
    assert verdict.decide_exit(a) == 0
    lines = verdict.verdict_lines(a, 6)
    assert "NOT FULLY TESTED" in lines[0] and any("NOT EXECUTED" in x for x in lines)


def test_missing_critical_only_counts_apps_that_were_asked_for():
    wfs = [{"id": "n1", "app": "nurseconnect", "risk": "high"}, {"id": "h1", "app": "hospital", "risk": "high"}]
    a = verdict.analyse([res("n1", "PASS")], wfs, critical=CRIT, apps=("nurseconnect",))
    assert a["missing_critical"] == [] and verdict.decide_exit(a, fail_on_critical_unverified=True) == 0
    a = verdict.analyse([], wfs, critical=CRIT, apps=("nurseconnect",), selection=("h1",))     # explicit --workflow filter opts out
    assert a["missing_critical"] == []


# ---- item 5: CLI regression matrix (real argument parsing + cmd_run + exit codes; Runner is faked, no browser)
import itertools
import agent


class FakeRunner:
    results = []

    def __init__(self, cfg):
        self.run_id, self.meta, self.store = "r1", {"environment": "(test)", "sources": []}, None

    def run(self, wfs):
        return list(FakeRunner.results)


@pytest.fixture
def cli(monkeypatch, tmp_path):
    monkeypatch.setattr(agent, "Runner", FakeRunner)
    monkeypatch.setattr(agent.reporting, "build_inventory", lambda *a, **k: [])
    monkeypatch.setattr(agent.reporting, "write_reports", lambda *a, **k: {"html": str(tmp_path / "r.html")})
    monkeypatch.setattr(agent, "_collect_sources", lambda a: [])
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.delenv("TEST_ENV", raising=False)

    def go(results, wfs, *flags):
        FakeRunner.results = results
        monkeypatch.setattr(agent, "load_all", lambda *a, **k: wfs)
        return agent.main(["run", "--read-only", "--app", "all", *flags])
    return go


CLI_WFS = [{"id": "p1", "app": "nurseconnect", "risk": "low", "title": "t", "writes": False, "steps": []},
           {"id": "f1", "app": "nurseconnect", "risk": "low", "title": "t", "writes": False, "steps": []},
           {"id": "b1", "app": "hospital", "risk": "low", "title": "t", "writes": False, "steps": []},
           {"id": "v1", "app": "hospital", "risk": "high", "title": "t", "writes": False, "steps": []},
           {"id": "t1", "app": "hospital", "risk": "high", "title": "t", "writes": False, "steps": []}]
CRED = "step 0 (login): Credentials missing: set HIMS_LAB_EMAIL and HIMS_LAB_PASSWORD"
ST = {"FAIL": res("f1", "FAIL", "boom"), "BLOCKED": res("b1", "BLOCKED", CRED),
      "NOT VERIFIED": res("v1", "NOT VERIFIED", "x"), "NOT TESTED": res("t1", "NOT TESTED", "x")}


def oracle(present, fb, fi, fc, crit_missing=False):
    if "FAIL" in present:
        return 1
    if fb and "BLOCKED" in present:
        return 4
    if fc and ("NOT VERIFIED" in present or "NOT TESTED" in present or crit_missing):
        return 6
    if fi and ("NOT VERIFIED" in present or "NOT TESTED" in present):
        return 5
    return 0


@pytest.mark.parametrize("fb,fi,fc", list(itertools.product([0, 1], repeat=3)))
@pytest.mark.parametrize("subset", [c for n in range(5) for c in itertools.combinations(ST, n)], ids=lambda c: "+".join(c) or "all-pass")
def test_cli_exit_code_matrix(cli, subset, fb, fi, fc):
    # every workflow always has a result: statuses not in `subset` are PASS, so no critical workflow is "missing"
    results = [ST[s] if s in subset else res({"FAIL": "f1", "BLOCKED": "b1", "NOT VERIFIED": "v1", "NOT TESTED": "t1"}[s], "PASS") for s in ST]
    results.append(res("p1", "PASS"))
    flags = [f for f, on in (("--fail-on-blocked", fb), ("--fail-on-incomplete", fi), ("--fail-on-critical-unverified", fc)) if on]
    assert cli(results, CLI_WFS, *flags) == oracle(set(subset), fb, fi, fc)


def test_cli_missing_critical_result_fails_only_with_the_flag(cli):
    results = [res("p1", "PASS"), res("f1", "PASS"), res("b1", "PASS")]          # v1 and t1 (critical) never ran
    assert cli(results, CLI_WFS) == 0
    assert cli(results, CLI_WFS, "--fail-on-critical-unverified") == 6
    assert cli(results, CLI_WFS, "--fail-on-critical-unverified", "--workflow", "p1") == 0     # explicit selection opts out


def test_cli_ignore_blocked_unapproved_but_never_credentials(cli):
    ok = [res(i, "PASS") for i in ("p1", "f1", "v1", "t1")] + [res("b1", "BLOCKED", "NOT APPROVED (draft): x")]
    assert cli(ok, CLI_WFS, "--fail-on-blocked") == 4
    assert cli(ok, CLI_WFS, "--fail-on-blocked", "--ignore-blocked", "unapproved") == 0
    bad = [res(i, "PASS") for i in ("p1", "f1", "v1", "t1")] + [res("b1", "BLOCKED", CRED)]
    assert cli(bad, CLI_WFS, "--fail-on-blocked", "--ignore-blocked", "unapproved") == 4


def test_cli_invalid_critical_config_exits_2(cli, monkeypatch, tmp_path):
    monkeypatch.setattr(verdict, "CRITICAL_FILE", tmp_path / "missing.yml")
    results = [res(i, "PASS") for i in ("p1", "f1", "b1", "v1", "t1")]
    assert cli(results, CLI_WFS, "--fail-on-critical-unverified") == 2
    assert cli(results, CLI_WFS) == 0


# ---- real Runner statuses (local server, real Chromium) feed the same verdict
from agent_tests.test_runner_e2e import ctx  # noqa: E402,F401  (fixture)
from agent_tests.conftest import wf as make_wf  # noqa: E402


def test_real_runner_statuses_map_to_expected_exit_codes(ctx):
    w_pass = make_wf("real_pass")
    w_fail = make_wf("real_fail", steps=[{"action": "goto", "path": "/"}, {"action": "expect_visible", "target": {"css": "#does-not-exist"}, "timeout_ms": 500}])
    w_nv = make_wf("real_nv", risk="high", steps=[{"action": "goto", "path": "/"}, {"action": "require_verified", "reason": "no selectors"}])
    w_draft = make_wf("real_draft", status="draft")
    wfs = [w_pass, w_fail, w_nv, w_draft]
    for w in (w_pass, w_fail, w_nv):
        ctx.store.approve(w, by="pytest", reason="synthetic test approval", confirm_writes=True)
    _, results = ctx.run(wfs, approve=False)
    st = {r.id: r.status for r in results}
    assert st == {"real_pass": "PASS", "real_fail": "FAIL", "real_nv": "NOT VERIFIED", "real_draft": "BLOCKED"}
    out = []
    only_nv = [r for r in results if r.id in ("real_pass", "real_nv")]
    assert verdict.finish(only_nv, wfs, say=out.append, fail_on_critical_unverified=True, critical_path=ROOT / "config" / "critical_workflows.yml") == 6
    drafted = [r for r in results if r.id in ("real_pass", "real_draft")]
    assert verdict.finish(drafted, wfs, say=out.append, fail_on_blocked=True) == 4
    assert verdict.finish(drafted, wfs, say=out.append, fail_on_blocked=True, ignore_blocked=("unapproved",)) == 0
    _, dry = ctx.run([w_pass], approve=True, mode="dry-run")
    assert dry[0].status == "NOT TESTED" and verdict.finish(dry, [w_pass], say=out.append, fail_on_incomplete=True) == 5


# ---- critical workflows can never be bypassed (v3)
UNAPPROVED = "NOT APPROVED (draft): status is not 'approved'"


def test_shipped_risk_levels_are_all_supported_by_the_schema():
    from qa_agent.schema import RISKS
    levels, _ = verdict.load_critical()
    assert levels and set(levels) <= set(RISKS)
    assert set(verdict.DEFAULT_CRITICAL_RISK) <= set(RISKS)


@pytest.mark.parametrize("levels", ["[critical]", "[high, critical]", "[HIGH]", "[urgent]", "[]", "high", "[low, 5]"])
def test_unsupported_risk_levels_fail_safely(tmp_path, levels):
    p = tmp_path / "c.yml"
    p.write_text(f"risk_levels: {levels}\nids: []\n", encoding="utf-8")
    with pytest.raises(verdict.CriticalConfigError):
        verdict.load_critical(p)
    out = []
    assert verdict.finish([res("a", "PASS")], WFS, say=out.append, fail_on_critical_unverified=True, critical_path=p) == 2


def test_supported_risk_levels_are_accepted(tmp_path):
    p = tmp_path / "c.yml"
    p.write_text("risk_levels: [medium, high]\n", encoding="utf-8")       # `ids` is optional
    assert verdict.load_critical(p) == (("medium", "high"), frozenset())


@pytest.mark.parametrize("cid", ["hi", "booking"], ids=["critical-by-risk", "critical-by-id"])
class TestCriticalNeverBypassed:
    def test_critical_blocked_unapproved_is_not_ignorable(self, cid):
        a = analyse([res("a", "PASS"), res(cid, "BLOCKED", UNAPPROVED)])
        assert a["critical_blocked"] == [cid]
        assert verdict.decide_exit(a, fail_on_blocked=True, ignore_blocked=("unapproved",)) == 4
        assert verdict.decide_exit(a, fail_on_critical_unverified=True, ignore_blocked=("unapproved",)) == 6   # critical flag alone is enough
        assert verdict.decide_exit(a, fail_on_blocked=True, fail_on_critical_unverified=True, ignore_blocked=("unapproved",)) == 4

    def test_critical_blocked_missing_credentials(self, cid):
        a = analyse([res("a", "PASS"), res(cid, "BLOCKED", CRED)])
        assert a["critical_blocked"] == [cid]
        assert verdict.decide_exit(a, fail_on_blocked=True, ignore_blocked=("unapproved",)) == 4
        assert verdict.decide_exit(a, fail_on_critical_unverified=True) == 6

    def test_critical_not_tested(self, cid):
        a = analyse([res("a", "PASS"), res(cid, "NOT TESTED", "dry-run")])
        assert a["critical_unverified"] == [cid]
        assert verdict.decide_exit(a, fail_on_critical_unverified=True) == 6
        assert verdict.decide_exit(a) == 0

    def test_critical_with_no_result_at_all(self, cid):
        a = verdict.analyse([res("a", "PASS")], WFS, critical=CRIT, apps=("hospital",))
        assert cid in a["missing_critical"]
        assert verdict.decide_exit(a, fail_on_critical_unverified=True, ignore_blocked=("unapproved",)) == 6

    @pytest.mark.parametrize("reason", [UNAPPROVED, CRED, "WRITE BLOCKED: read-only", "Browser execution could not start or finish: X"])
    def test_every_block_kind_fails_for_critical(self, cid, reason):
        a = analyse([res(cid, "BLOCKED", reason)])
        assert verdict.decide_exit(a, fail_on_critical_unverified=True, ignore_blocked=tuple(verdict.BLOCK_KINDS)) == 6


def test_noncritical_unapproved_is_still_ignorable_next_to_a_passing_critical():
    a = analyse([res("hi", "PASS"), res("lo", "BLOCKED", UNAPPROVED)])
    assert a["critical_blocked"] == []
    assert verdict.decide_exit(a, fail_on_blocked=True, fail_on_critical_unverified=True, ignore_blocked=("unapproved",)) == 0


def test_mixed_critical_and_noncritical_unapproved_still_fails():
    a = analyse([res("lo", "BLOCKED", UNAPPROVED), res("hi", "BLOCKED", UNAPPROVED)])
    assert verdict.decide_exit(a, fail_on_blocked=True, ignore_blocked=("unapproved",)) == 4


def test_verdict_text_names_critical_blocked_workflows():
    a = analyse([res("hi", "BLOCKED", UNAPPROVED)])
    text = "\n".join(verdict.verdict_lines(a, 6))
    assert "CRITICAL BLOCKED" in text and "hi" in text and "VERDICT: NOT FULLY TESTED" in text


# CLI level: real argument parsing + cmd_run + exit codes (CI's exact flag set)
CI_FLAGS = ("--fail-on-blocked", "--ignore-blocked", "unapproved", "--fail-on-critical-unverified")


def _all_pass_except(**over):
    base = {"p1": "PASS", "f1": "PASS", "b1": "PASS", "v1": "PASS", "t1": "PASS"}
    base.update(over)
    return [res(i, s, UNAPPROVED if s == "BLOCKED" else "") for i, s in base.items()]


def test_cli_critical_unapproved_fails_with_ci_flags(cli):
    assert cli(_all_pass_except(), CLI_WFS, *CI_FLAGS) == 0
    assert cli(_all_pass_except(v1="BLOCKED"), CLI_WFS, *CI_FLAGS) == 4                     # v1 is critical (risk high)
    assert cli(_all_pass_except(v1="BLOCKED"), CLI_WFS, "--fail-on-critical-unverified", "--ignore-blocked", "unapproved") == 6
    assert cli(_all_pass_except(b1="BLOCKED"), CLI_WFS, *CI_FLAGS) == 0                     # non-critical draft: tolerated


def test_cli_critical_credentials_missing_fails_with_ci_flags(cli):
    r = _all_pass_except()
    r[3] = res("v1", "BLOCKED", CRED)
    assert cli(r, CLI_WFS, *CI_FLAGS) == 4
    assert cli(r, CLI_WFS, "--fail-on-critical-unverified") == 6


def test_cli_critical_not_tested_and_missing_fail_with_ci_flags(cli):
    assert cli(_all_pass_except(t1="NOT TESTED"), CLI_WFS, *CI_FLAGS) == 6
    missing = [r for r in _all_pass_except() if r.id != "t1"]
    assert cli(missing, CLI_WFS, *CI_FLAGS) == 6


def test_cli_critical_by_id_via_the_shipped_config(cli, monkeypatch):
    wfs = CLI_WFS + [{"id": "consumer_booking_flow_readonly", "app": "nurseconnect", "risk": "medium", "title": "t", "writes": False, "steps": []}]
    ok = _all_pass_except() + [res("consumer_booking_flow_readonly", "PASS")]
    assert cli(ok, wfs, *CI_FLAGS) == 0
    for status in ("NOT VERIFIED", "NOT TESTED", "BLOCKED"):
        bad = _all_pass_except() + [res("consumer_booking_flow_readonly", status, UNAPPROVED if status == "BLOCKED" else "x")]
        assert cli(bad, wfs, *CI_FLAGS) in (4, 6), status
    assert cli(_all_pass_except(), wfs, *CI_FLAGS) == 6                                     # booking flow never ran at all
