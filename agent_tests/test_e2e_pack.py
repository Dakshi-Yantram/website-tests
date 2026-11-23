"""The NurseConnect customer/nurse/operations/reviewer/admin pack: engine features it needs, and checks on the pack itself.
Synthetic data and a local server only. No real credentials."""
from pathlib import Path

import pytest
import yaml

import config
from agent_tests.conftest import ROOT, wf
from agent_tests.test_runner_e2e import ctx, by_id          # noqa: F401  (fixtures)
from qa_agent import schema
from qa_agent.schema import DANGEROUS_RX, validate_workflow

PACK = ROOT / "workflow_drafts" / "nurseconnect" / "e2e_edge_security.yml"


def _pack():
    return yaml.safe_load(PACK.read_text(encoding="utf-8"))["workflows"]


# ------------------------------------------------------------------ engine features used by the pack
def test_new_roles_exist_for_the_agent_but_not_for_the_legacy_suite():
    for r, env in (("operations", "OPS_EMAIL"), ("reviewer", "REVIEWER_EMAIL"), ("admin", "ADMIN_EMAIL")):
        assert config.AGENT_ROLES[r].app == "nurseconnect" and config.AGENT_ROLES[r].email_env == env
        assert r not in config.ROLES, "legacy tests are parametrized over ROLES; adding roles there would change the legacy report"


def test_staging_only_must_be_boolean():
    probs = validate_workflow(wf("so_bad", staging_only="yes"))
    assert any("staging_only must be true or false" in p for p in probs)
    assert not validate_workflow(wf("so_good", staging_only=True))


def test_staging_only_workflow_is_blocked_on_production_and_unknown_env(ctx):
    w = wf("so_prod_block", staging_only=True)
    _, res = ctx.run([w], env="prod")
    assert by_id(res)["so_prod_block"].status == "BLOCKED" and "STAGING-ONLY" in by_id(res)["so_prod_block"].reason
    _, res = ctx.run([wf("so_none_block", staging_only=True)], env=None)
    assert by_id(res)["so_none_block"].status == "BLOCKED"


def test_staging_only_workflow_runs_on_a_declared_local_or_staging_environment(ctx):
    _, res = ctx.run([wf("so_local_ok", staging_only=True)])
    assert by_id(res)["so_local_ok"].status == "PASS"


def test_staging_only_is_part_of_the_approval_hash():
    from qa_agent.workflows import content_hash
    assert content_hash(wf("h1", staging_only=True)) != content_hash(wf("h1", staging_only=False))


def test_template_values_in_targets_are_expanded(ctx):
    w = wf("tpl_target_ok", steps=[{"action": "goto", "path": "/echo?q={{MARKER}}"},
                                   {"action": "expect_visible", "target": {"text": "{{MARKER}}"}},
                                   {"action": "expect_visible", "target": {"role": "heading", "name": "Echo"}}])
    _, res = ctx.run([w])
    assert by_id(res)["tpl_target_ok"].status == "PASS", by_id(res)["tpl_target_ok"].__dict__


def test_unknown_template_variable_in_a_target_is_rejected():
    w = wf("tpl_bad", steps=[{"action": "expect_visible", "target": {"text": "{{SECRET}}"}}])
    assert any("unknown template variable" in p for p in validate_workflow(w))


# ------------------------------------------------------------------ the pack itself
def test_pack_is_valid_and_every_workflow_is_a_draft():
    wfs = _pack()
    assert len(wfs) == 42
    assert all(w["status"] == "draft" for w in wfs), "nothing may ship pre-approved"
    for w in wfs:
        assert not validate_workflow(w), w["id"]
    assert len({w["id"] for w in wfs}) == len(wfs)


def test_pack_covers_the_requested_journeys_and_roles():
    wfs = _pack()
    roles = {w.get("role") for w in wfs}
    assert {"consumer", "nurse", "operations", "reviewer", "admin"} <= roles
    ids = " ".join(w["id"] for w in wfs)
    for part in ("customer_books", "operations_assigns", "nurse_accepts", "reviewer_requests_changes", "reviewer_approves",
                 "admin_sees", "brute_force", "sql_injection", "html_is_escaped", "double_click"):
        assert part in ids, part
    assert sum(w["id"].startswith("edge_") for w in wfs) >= 10 and sum(w["id"].startswith("sec_") for w in wfs) >= 12


def test_brute_force_workflows_are_staging_only_and_never_write_through_submit():
    for w in _pack():
        if "brute-force" in w.get("tags", []):
            assert w.get("staging_only") is True
            assert not any(s["action"] == "submit" for s in w["steps"])


def test_pack_has_no_literal_credentials_and_no_forbidden_clicks():
    text = PACK.read_text(encoding="utf-8")
    from qa_agent.redaction import find_secrets, find_personal
    assert not find_secrets(text) and not find_personal(text)
    for w in _pack():
        for s in w["steps"]:
            if s["action"] in ("click", "submit", "guarded_submit"):
                assert not DANGEROUS_RX.search(str(s.get("target"))), (w["id"], s)


def test_pack_write_workflows_use_submit_and_read_only_ones_do_not():
    for w in _pack():
        has = any(s["action"] == "submit" for s in w["steps"])
        assert has == bool(w["writes"]), w["id"]


def test_pack_is_blocked_until_approved(ctx):
    w = _pack()[1]                       # e2e_02 (read-only)
    _, res = ctx.run([w], approve=False)
    assert by_id(res)[w["id"]].status == "BLOCKED" and "NOT APPROVED" in by_id(res)[w["id"]].reason


def test_marker_and_name_are_shared_by_all_workflows_of_one_run_so_roles_can_hand_over_records():
    from qa_agent.workflows import synth_values
    a, b = synth_values("run42", "e2e_01_customer_books_care_package"), synth_values("run42", "e2e_06_nurse_accepts_assignment")
    assert a["MARKER"] == b["MARKER"] and a["SYNTH_NAME"] == b["SYNTH_NAME"]
    assert a["SYNTH_PHONE"] != b["SYNTH_PHONE"]                       # still isolated where it must be
    assert synth_values("run43", "e2e_01_customer_books_care_package")["MARKER"] != a["MARKER"]   # but not across runs
