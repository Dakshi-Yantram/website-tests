"""End-to-end: real Chromium against a LOCAL synthetic server. Verifies gates, guards, statuses and reports."""
import json
import os
from pathlib import Path

import pytest

from agent_tests.conftest import make_env_file, posts, wf
from qa_agent import reporting
from qa_agent.approvals import ApprovalStore
from qa_agent.redaction import clear_registered_secrets
from qa_agent.runner import RunConfig, Runner
from qa_agent.safety import CONFIRM_PREFIX, get_environment

SAVE = {"role": "button", "name": "Save Patient"}


@pytest.fixture
def ctx(tmp_path, server, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("SCREENSHOT_MODE", "masked")
    store = ApprovalStore(tmp_path / "approvals")
    envfile = make_env_file(tmp_path, server)
    class C: pass
    c = C()
    c.tmp, c.server, c.store, c.envfile = tmp_path, server, store, envfile
    c.env = get_environment("localtest", envfile)
    c.prod = get_environment("production", envfile)

    def run(workflows, approve=True, env="local", **kw):
        if approve:
            for w in workflows:
                store.approve(w, by="pytest", reason="synthetic test approval", confirm_writes=True)
        cfg = dict(env=c.env if env == "local" else c.prod if env == "prod" else None, base_urls={"nurseconnect": server, "hospital": server},
                   artifacts_dir=tmp_path / "artifacts", run_id="t1", store=store)
        cfg.update(kw)
        r = Runner(RunConfig(**cfg))
        return r, r.run(workflows)
    c.run = run
    yield c
    clear_registered_secrets()


def by_id(results):
    return {r.id: r for r in results}


# ----------------------------------------------------------------- approval enforcement
def test_unapproved_workflow_is_blocked_and_no_browser_is_launched(ctx, monkeypatch):
    monkeypatch.setattr(Runner, "_run_browser", lambda self, r: (_ for _ in ()).throw(AssertionError("browser must not launch")))
    r, res = ctx.run([wf("not_approved_yet")], approve=False)
    assert res[0].status == "BLOCKED" and "NOT APPROVED" in res[0].reason


def test_draft_status_workflow_is_blocked(ctx):
    w = wf("draft_one_wf", status="draft")
    r, res = ctx.run([w], approve=False)
    assert res[0].status == "BLOCKED" and "draft" in res[0].reason


def test_workflow_changed_after_approval_is_blocked(ctx):
    w = wf("changed_wf_one")
    ctx.store.approve(w, by="pytest", reason="synthetic test approval")
    changed = wf("changed_wf_one", steps=[{"action": "goto", "path": "/login"}])
    r, res = ctx.run([changed], approve=False)
    assert res[0].status == "BLOCKED" and "changed since approval" in res[0].reason


# ----------------------------------------------------------------- reads, assertions, statuses
def test_login_form_workflow_passes_and_secrets_never_reach_reports(ctx, monkeypatch, requests_log):
    monkeypatch.setenv("NURSE_EMAIL", "nurse.test@example.invalid")
    monkeypatch.setenv("NURSE_PASSWORD", "Zx9-very-secret-pw")
    w = wf("login_form_flow", steps=[
        {"action": "goto", "path": "/login"},
        {"action": "fill", "target": {"label": "Email"}, "value_env": "NURSE_EMAIL"},
        {"action": "fill", "target": {"label": "Password"}, "value_env": "NURSE_PASSWORD"},
        {"action": "login_submit", "target": {"role": "button", "name": "Log in"}},
        {"action": "expect_url", "contains": "/dashboard"},
        {"action": "expect_authenticated"},
        {"action": "expect_text", "contains": "welcome"},
    ])
    r, res = ctx.run([w])
    assert res[0].status == "PASS", [(s.n, s.status, s.error) for s in res[0].steps]
    inv = reporting.build_inventory([w], res, ctx.store, ctx.tmp / "nodrafts")
    paths = reporting.write_reports(res, r.meta, ctx.tmp / "rep", inv)
    for p in paths.values():
        assert "Zx9-very-secret-pw" not in Path(p).read_text()
    assert posts(requests_log) == [("POST", "/api/login")], "only the login/session POST may reach the server"


def test_read_only_navigation_and_assertions_pass(ctx, requests_log):
    w = wf("nav_readonly_one", steps=[
        {"action": "goto", "path": "/"}, {"action": "expect_title", "contains": "home"},
        {"action": "expect_visible", "target": {"role": "heading", "name": "Synthetic home"}},
        {"action": "expect_count", "target": {"role": "link"}, "min": 2},
        {"action": "click", "target": {"role": "link", "name": "Form"}}, {"action": "expect_url", "matches": "/form$"},
        {"action": "expect_no_writes_sent"}])
    r, res = ctx.run([w])
    assert res[0].status == "PASS", [(s.n, s.status, s.error) for s in res[0].steps]
    assert not posts(requests_log)


def test_failed_assertion_is_FAIL_with_clear_message_and_remaining_steps_not_tested(ctx):
    w = wf("failing_assert_one", steps=[{"action": "goto", "path": "/"}, {"action": "expect_text", "contains": "THIS TEXT IS NOT THERE", "timeout_ms": 800},
                                         {"action": "goto", "path": "/visits"}])
    r, res = ctx.run([w])
    assert res[0].status == "FAIL"
    assert "did not contain/match 'THIS TEXT IS NOT THERE'" in res[0].steps[1].error
    assert res[0].steps[1].screenshot and Path(res[0].steps[1].screenshot).is_file()
    assert res[0].steps[2].status == "NOT TESTED" and "step 2 failed" in res[0].steps[2].actual


def test_missing_selector_is_FAIL_not_a_crash(ctx):
    w = wf("missing_selector_one", steps=[{"action": "goto", "path": "/"}, {"action": "click", "target": {"role": "button", "name": "Nonexistent"}, "timeout_ms": 700}])
    r, res = ctx.run([w])
    assert res[0].status == "FAIL" and "Element not found within 700 ms" in res[0].steps[1].error


def test_http_error_and_404_are_FAIL(ctx):
    r, res = ctx.run([wf("http404_flow_one", steps=[{"action": "goto", "path": "/does-not-exist"}]),
                      wf("http500_flow_one", steps=[{"action": "goto", "path": "/boom"}])])
    assert [x.status for x in res] == ["FAIL", "FAIL"]
    assert "HTTP 404" in res[0].steps[0].error and "HTTP 500" in res[1].steps[0].error


def test_navigation_error_is_FAIL(ctx):
    r, res = ctx.run([wf("nav_error_one", steps=[{"action": "goto", "path": "/"}])], base_urls={"nurseconnect": "http://127.0.0.1:9", "hospital": "http://127.0.0.1:9"},
                     env=None)
    assert res[0].status == "FAIL" and "Navigation error" in res[0].steps[0].error


def test_optional_step_failure_is_NOT_VERIFIED_not_FAIL(ctx):
    w = wf("optional_step_one", steps=[{"action": "goto", "path": "/"}, {"action": "expect_visible", "target": {"text": "nothing like this"}, "optional": True, "timeout_ms": 600}])
    r, res = ctx.run([w])
    assert res[0].status == "NOT VERIFIED" and res[0].steps[1].status == "NOT VERIFIED"


def test_missing_credentials_env_var_is_BLOCKED(ctx, monkeypatch):
    monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)
    w = wf("missing_cred_one", steps=[{"action": "goto", "path": "/login"}, {"action": "fill", "target": {"label": "Email"}, "value_env": "DEFINITELY_UNSET_VAR"}])
    r, res = ctx.run([w])
    assert res[0].status == "BLOCKED" and "DEFINITELY_UNSET_VAR is not set" in res[0].reason


def test_runtime_refuses_to_click_destructive_elements_even_if_the_yaml_was_innocent(ctx, requests_log):
    # validation rejects the word "Delete" in the YAML, so reach the element by a neutral selector (css) instead
    w = wf("sneaky_delete_one", steps=[{"action": "goto", "path": "/form"}, {"action": "click", "target": {"css": "button >> nth=1"}}])
    r, res = ctx.run([w])
    assert res[0].status == "BLOCKED" and "destructive" in res[0].reason
    assert ("POST", "/api/delete") not in requests_log


def test_dry_run_evaluates_gates_without_a_browser(ctx, monkeypatch):
    monkeypatch.setattr(Runner, "_run_browser", lambda self, r: (_ for _ in ()).throw(AssertionError("no browser in dry-run")))
    ok, bad = wf("dry_ok_one"), wf("dry_bad_one")
    ctx.store.approve(ok, by="pytest", reason="synthetic test approval")
    r, res = ctx.run([ok, bad], approve=False, mode="dry-run")
    st = {x.id: x for x in res}
    assert st["dry_ok_one"].status == "NOT TESTED" and "dry-run" in st["dry_ok_one"].reason
    assert st["dry_bad_one"].status == "BLOCKED"


def test_app_and_workflow_filters(ctx):
    a, b = wf("app_a_wf", app="nurseconnect"), wf("app_b_wf", app="hospital")
    r, res = ctx.run([a, b], apps=("hospital",))
    assert [x.id for x in res] == ["app_b_wf"]
    r, res = ctx.run([a, b], only=("app_a_wf",))
    assert [x.id for x in res] == ["app_a_wf"]


# ----------------------------------------------------------------- guarded submit: nothing reaches the server
def test_guarded_submit_prevents_the_write_at_the_network_layer(ctx, requests_log):
    w = wf("guarded_submit_one", steps=[{"action": "goto", "path": "/form"}, {"action": "guarded_submit", "target": SAVE},
                                         {"action": "expect_text", "contains": "required"}, {"action": "expect_no_writes_sent"}])
    r, res = ctx.run([w])
    assert res[0].status == "PASS", [(s.n, s.status, s.error) for s in res[0].steps]
    assert not posts(requests_log), "the page tried to POST but the guard must have stopped it before the server"
    assert any("/api/save" in b for b in res[0].blocked_requests)


# ----------------------------------------------------------------- production / unknown origin / staging write gates
def write_wf(wid="staging_write_one"):
    return wf(wid, writes=True, risk="medium", steps=[{"action": "goto", "path": "/form"}, {"action": "submit", "target": SAVE},
                                                      {"action": "expect_text", "contains": "saved"}])


def test_write_workflow_is_blocked_on_production_and_nothing_is_sent(ctx, requests_log):
    r, res = ctx.run([write_wf()], env="prod", allow_write=True, confirm_phrase=CONFIRM_PREFIX + "production", confirmed_workflows=("staging_write_one",))
    assert res[0].status == "BLOCKED" and "production is read-only" in res[0].reason
    assert not requests_log, "not even a GET: the gate fires before any browser work"


def test_write_workflow_is_blocked_when_environment_is_unknown(ctx, requests_log):
    r, res = ctx.run([write_wf()], env=None, allow_write=True, confirm_phrase="x", confirmed_workflows=("staging_write_one",))
    assert res[0].status == "BLOCKED" and "No verified environment" in res[0].reason
    assert not posts(requests_log)


def test_write_blocked_for_origin_outside_the_allowlist(ctx, requests_log):
    r, res = ctx.run([write_wf()], allow_write=True, confirm_phrase=CONFIRM_PREFIX + "localtest", confirmed_workflows=("staging_write_one",),
                     base_urls={"nurseconnect": "http://localhost:%s" % ctx.server.rsplit(":", 1)[1], "hospital": ctx.server})
    assert res[0].status == "BLOCKED" and "not in the allowlist" in res[0].reason
    assert not requests_log


@pytest.mark.parametrize("kw,needle", [
    (dict(allow_write=False, confirm_phrase=CONFIRM_PREFIX + "localtest", confirmed_workflows=("staging_write_one",)), "not enabled"),
    (dict(allow_write=True, confirm_phrase="", confirmed_workflows=("staging_write_one",)), "confirmation phrase"),
    (dict(allow_write=True, confirm_phrase=CONFIRM_PREFIX + "localtest", confirmed_workflows=()), "not explicitly confirmed"),
])
def test_write_blocked_without_each_confirmation(ctx, requests_log, kw, needle):
    r, res = ctx.run([write_wf()], **kw)
    assert res[0].status == "BLOCKED" and needle in res[0].reason
    assert not posts(requests_log)


def test_read_only_mode_marks_write_workflows_NOT_TESTED(ctx, requests_log):
    r, res = ctx.run([write_wf()], mode="read-only", allow_write=True, confirm_phrase=CONFIRM_PREFIX + "localtest", confirmed_workflows=("staging_write_one",))
    assert res[0].status == "NOT TESTED" and "read-only mode" in res[0].reason
    assert not posts(requests_log)


def test_write_runs_on_verified_staging_with_all_confirmations_and_creates_exactly_one_record(ctx, requests_log):
    r, res = ctx.run([write_wf()], allow_write=True, confirm_phrase=CONFIRM_PREFIX + "localtest", confirmed_workflows=("staging_write_one",))
    assert res[0].status == "PASS", [(s.n, s.status, s.error) for s in res[0].steps]
    assert posts(requests_log) == [("POST", "/api/save")]


def test_environment_probe_failure_blocks_writes(ctx, requests_log):
    f = make_env_file(ctx.tmp, ctx.server, verify={"path": "/__env", "json_key": "environment", "equals": "something-else"})
    env = get_environment("localtest", f)
    store = ctx.store
    w = write_wf()
    store.approve(w, by="pytest", reason="synthetic test approval", confirm_writes=True)
    rr = Runner(RunConfig(env=env, base_urls={"nurseconnect": ctx.server, "hospital": ctx.server}, artifacts_dir=ctx.tmp / "a", store=store, allow_write=True,
                          confirm_phrase=CONFIRM_PREFIX + "localtest", confirmed_workflows=("staging_write_one",)))
    res = rr.run([w])
    assert res[0].status == "BLOCKED" and "verification mismatch" in res[0].reason
    assert not posts(requests_log)


def test_concurrent_write_lock_prevents_sharing_mutable_data(ctx, requests_log):
    lock_dir = ctx.tmp / "locks"
    lock_dir.mkdir()
    (lock_dir / "localtest-nurseconnect.lock").write_text("held by another run")
    from qa_agent import runner as rmod
    orig = rmod._Lock.__init__
    rmod._Lock.__init__ = lambda self, path, wait_s=60, stale_s=900: orig(self, path, wait_s=1, stale_s=3600)
    try:
        r, res = ctx.run([write_wf()], allow_write=True, confirm_phrase=CONFIRM_PREFIX + "localtest", confirmed_workflows=("staging_write_one",), lock_dir=lock_dir)
    finally:
        rmod._Lock.__init__ = orig
    assert res[0].status == "BLOCKED" and "write lock" in res[0].reason
    assert not posts(requests_log)


def test_synthetic_data_is_unique_per_run_and_workflow():
    from qa_agent.workflows import synth_values
    a, b, c = synth_values("r1", "wf_a"), synth_values("r1", "wf_b"), synth_values("r2", "wf_a")
    assert len({a["SYNTH_PHONE"], b["SYNTH_PHONE"], c["SYNTH_PHONE"]}) == 3
    assert a["MARKER"] == "AUTOTEST_r1" and a["SYNTH_EMAIL"].endswith("@example.invalid")


# ----------------------------------------------------------------- reports
def test_reports_json_md_html_inventory_with_all_statuses(ctx, monkeypatch):
    monkeypatch.setenv("SOME_API_TOKEN", "tok-should-not-appear-123")
    good = wf("report_pass_wf")
    bad = wf("report_fail_wf", steps=[{"action": "goto", "path": "/boom"}])
    blocked = wf("report_blocked_wf")
    ctx.store.approve(good, by="pytest", reason="synthetic test approval")
    ctx.store.approve(bad, by="pytest", reason="synthetic test approval")
    wfs = [good, bad, blocked]
    r, res = ctx.run(wfs, approve=False)
    drafts = ctx.tmp / "drafts" / "nurseconnect"
    drafts.mkdir(parents=True)
    (drafts / "d.yml").write_text("version: 1\nworkflows:\n  - {id: draft_only_wf, app: nurseconnect, title: T, risk: low, writes: false}\n")
    inv = reporting.build_inventory(wfs, res, ctx.store, ctx.tmp / "drafts")
    paths = reporting.write_reports(res, r.meta, ctx.tmp / "reports", inv)
    data = json.loads(Path(paths["json"]).read_text())
    assert data["summary"]["PASS"] == 1 and data["summary"]["FAIL"] == 1 and data["summary"]["BLOCKED"] == 1
    assert data["meta"]["environment"] == "localtest" and "chromium" in data["meta"]["browser_version"] and data["meta"]["started"]
    states = {i["id"]: (i["approval"], i["last_result"]) for i in data["inventory"]}
    assert states["report_pass_wf"] == ("approved", "PASS") and states["report_fail_wf"] == ("approved", "FAIL")
    assert states["report_blocked_wf"][0] == "unapproved" and states["draft_only_wf"] == ("draft", "NOT TESTED")
    md = Path(paths["md"]).read_text()
    assert "## Failures" in md and "report_fail_wf" in md and "HTTP 500" in md and "## Skipped / not run, with reasons" in md and "NOT APPROVED" in md
    html = Path(paths["html"]).read_text()
    for status in ("PASS", "FAIL", "BLOCKED", "NOT VERIFIED", "NOT TESTED"):
        assert status in html
    assert Path(paths["inventory_md"]).is_file()
    for p in paths.values():
        assert "tok-should-not-appear-123" not in Path(p).read_text()
    assert any(s["id"] == "report_blocked_wf" for s in data["skipped"])


def test_report_is_withheld_when_sensitive_data_cannot_be_removed(ctx, monkeypatch):
    r, res = ctx.run([wf("withheld_report_one")])
    monkeypatch.setattr(reporting, "redact_obj", lambda o, **k: o)           # simulate a redaction failure
    res[0].reason = "leaked ghp_" + "Z9" * 20
    from qa_agent.errors import SensitiveDataError
    with pytest.raises(SensitiveDataError):
        reporting.write_reports(res, r.meta, ctx.tmp / "rep2")
    files = list((ctx.tmp / "rep2").iterdir())
    assert [f.name for f in files] == ["agent_report_t1_WITHHELD.txt"], "no report may be written when redaction fails"


def test_screenshots_are_masked_by_default(ctx):
    w = wf("screenshot_mask_one", steps=[{"action": "goto", "path": "/form"}, {"action": "screenshot", "name": "form"}])
    r, res = ctx.run([w])
    assert res[0].status == "PASS" and "masked" in res[0].steps[1].actual
    shot = ctx.tmp / "artifacts" / "screenshots" / "t1_screenshot_mask_one_form.png"
    assert shot.is_file() and shot.stat().st_size > 500
    r2, res2 = ctx.run([w], screenshot_mode="off")
    assert res2[0].steps[1].status == "NOT TESTED" and "SCREENSHOT_MODE=off" in res2[0].steps[1].error
