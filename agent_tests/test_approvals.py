"""Explicit approval, re-approval on change, audit chain, refusal of unapproved workflows."""
import json

import pytest
import yaml

from agent_tests.conftest import wf, write_wf_file
from qa_agent.approvals import ApprovalStore, promote_draft
from qa_agent.errors import ApprovalError, SchemaError


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    return ApprovalStore(tmp_path / "approvals")


def test_unapproved_workflow_cannot_run(store):
    w = wf()
    assert store.state(w)[0] == "unapproved"
    with pytest.raises(ApprovalError, match="no approval record"):
        store.require_approved(w)


def test_draft_status_cannot_run_even_with_a_record(store):
    w = wf(status="draft")
    store.approve(dict(w, status="approved"), by="Dakshi", reason="reviewed the steps")
    assert store.state(w)[0] == "draft"
    with pytest.raises(ApprovalError):
        store.require_approved(w)


def test_approve_then_run_gate_passes(store):
    w = wf()
    rec = store.approve(w, by="Dakshi", reason="reviewed the steps")
    assert rec["approved_by"] == "Dakshi" and len(rec["hash"]) == 64
    store.require_approved(w)
    assert store.state(w)[0] == "approved"


def test_material_change_requires_reapproval(store):
    w = wf()
    store.approve(w, by="Dakshi", reason="reviewed the steps")
    changed = wf(steps=[{"action": "goto", "path": "/somewhere-else"}])
    assert store.state(changed)[0] == "changed"
    with pytest.raises(ApprovalError, match="re-approval required"):
        store.require_approved(changed)
    store.approve(changed, by="Dakshi", reason="re-reviewed the new path")
    store.require_approved(changed)
    events = [json.loads(l)["event"] for l in store.audit.read_text().splitlines()]
    assert events == ["approve", "re-approve"]


def test_approver_and_reason_are_mandatory(store):
    with pytest.raises(ApprovalError, match="--by"):
        store.approve(wf(), by="", reason="because reasons")
    with pytest.raises(ApprovalError, match="--reason"):
        store.approve(wf(), by="Dakshi", reason="")


def test_write_workflows_need_explicit_acknowledgement(store):
    w = wf(writes=True, steps=[{"action": "submit", "target": {"role": "button", "name": "Save"}}])
    with pytest.raises(ApprovalError, match="writes test records"):
        store.approve(w, by="Dakshi", reason="reviewed the steps")
    store.approve(w, by="Dakshi", reason="reviewed the steps", confirm_writes=True)


def test_approval_is_refused_in_ci(store, monkeypatch):
    monkeypatch.setenv("CI", "true")
    with pytest.raises(ApprovalError, match="refused in CI"):
        store.approve(wf(), by="bot", reason="auto approve please")


def test_revoke(store):
    w = wf()
    store.approve(w, by="Dakshi", reason="reviewed the steps")
    store.revoke(w["id"], by="Dakshi", reason="no longer needed")
    assert store.state(w)[0] == "unapproved"
    with pytest.raises(ApprovalError):
        store.revoke(w["id"], by="Dakshi", reason="again")


def test_audit_chain_detects_tampering(store):
    store.approve(wf("wf_first_id"), by="A", reason="reviewed one")
    store.approve(wf("wf_second_id"), by="A", reason="reviewed two")
    assert store.verify_audit() == []
    lines = store.audit.read_text().splitlines()
    store.audit.write_text("\n".join(lines[1:]) + "\n")             # remove the first line
    assert any("chain broken" in p for p in store.verify_audit())


def test_corrupt_approvals_file_fails_closed(store):
    store.dir.mkdir(parents=True, exist_ok=True)
    store.file.write_text("{ not json")
    with pytest.raises(ApprovalError, match="unreadable"):
        store.state(wf())


def _draft(tmp_path, **kw):
    d = wf("draft_demo_one", status="draft", sources=[{"file": "src/App.jsx", "line": 4}], preconditions=["p"], expected_outcome=["o"],
           generated={"tool": "test"}, **kw)
    return write_wf_file(tmp_path, d, name="draft.yml")


def test_promote_draft_copies_to_approved_dir_and_records_approval(tmp_path, store):
    f = _draft(tmp_path)
    ids = promote_draft(f, store=store, by="Dakshi", reason="reviewed the draft", approved_dir=tmp_path / "approved")
    assert ids == ["draft_demo_one"]
    out = yaml.safe_load((tmp_path / "approved" / "draft_demo_one.yml").read_text())
    assert out["workflows"][0]["status"] == "approved"
    assert yaml.safe_load(f.read_text())["workflows"][0]["status"] == "draft", "the draft file itself is left as a draft"
    store.require_approved(out["workflows"][0])


def test_promote_refuses_overwrite_and_non_drafts_and_invalid_drafts(tmp_path, store):
    f = _draft(tmp_path)
    promote_draft(f, store=store, by="D", reason="reviewed the draft", approved_dir=tmp_path / "approved")
    with pytest.raises(ApprovalError, match="already exists"):
        promote_draft(f, store=store, by="D", reason="reviewed the draft", approved_dir=tmp_path / "approved")
    notdraft = write_wf_file(tmp_path, wf("not_a_draft_x"), name="n.yml")
    with pytest.raises(ApprovalError, match="not a draft"):
        promote_draft(notdraft, store=store, by="D", reason="reviewed the draft", approved_dir=tmp_path / "approved2")
    bad = write_wf_file(tmp_path, wf("bad_generated_x", status="draft", generated={"t": 1}, writes=True,
                                    steps=[{"action": "submit", "target": {"role": "button", "name": "Save"}}]), name="bad.yml")
    with pytest.raises(SchemaError):
        promote_draft(bad, store=store, by="D", reason="reviewed the draft", approved_dir=tmp_path / "approved3")


def test_hand_edited_approvals_json_is_not_an_approval(store):
    w = wf()
    from qa_agent.workflows import content_hash
    store.dir.mkdir(parents=True, exist_ok=True)
    store.file.write_text(json.dumps({"version": 1, "approvals": {w["id"]: {"hash": content_hash(w), "approved_by": "nobody", "approved_at": "x"}}}))
    state, detail = store.state(w)
    assert state == "unapproved" and "not backed by the audit log" in detail
    with pytest.raises(ApprovalError):
        store.require_approved(w)


def test_edited_audit_log_invalidates_all_approvals(store):
    a, b = wf("wf_first_id"), wf("wf_second_id")
    store.approve(a, by="A", reason="reviewed one")
    store.approve(b, by="A", reason="reviewed two")
    assert store.state(a)[0] == "approved"
    lines = store.audit.read_text().splitlines()
    store.audit.write_text("\n".join(lines[1:]) + "\n")
    assert store.state(b)[0] == "unapproved" and "audit.jsonl is broken" in store.state(b)[1]


def test_revoked_then_restored_record_is_not_valid(store):
    w = wf()
    store.approve(w, by="A", reason="reviewed one")
    saved = store.file.read_text()
    store.revoke(w["id"], by="A", reason="no longer needed here")
    store.file.write_text(saved)                       # someone puts the old record back by hand
    assert store.state(w)[0] == "unapproved"
