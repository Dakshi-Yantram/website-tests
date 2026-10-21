"""Valid / invalid workflows.yml, schema validation, unsupported actions, clear error messages."""
import re
from pathlib import Path

import pytest
import yaml

from agent_tests.conftest import ROOT, wf, write_wf_file
from qa_agent.errors import SchemaError, WorkflowFileError
from qa_agent.schema import ACTIONS, validate_document, validate_workflow
from qa_agent.workflows import content_hash, load_all, load_workflows


def problems(w, **kw):
    return validate_workflow(w, **kw)


def test_shipped_workflows_yml_is_valid_and_has_required_examples():
    wfs = load_workflows(ROOT / "workflows.yml")
    ids = {w["id"] for w in wfs}
    assert len(wfs) >= 6
    assert any("login" in i for i in ids), "login example missing"
    assert any("navigation" in i for i in ids), "page navigation example missing"
    assert any("validation" in i for i in ids), "form validation example missing"
    assert any("booking" in i or "create" in i for i in ids), "booking flow example missing"


def test_valid_minimal_workflow(tmp_path):
    f = write_wf_file(tmp_path, wf())
    assert [w["id"] for w in load_workflows(f)] == ["demo_workflow"]


def test_missing_file_message_names_the_file(tmp_path):
    with pytest.raises(WorkflowFileError, match="not found"):
        load_workflows(tmp_path / "nope.yml")


def test_malformed_yaml_reports_line(tmp_path):
    p = tmp_path / "bad.yml"
    p.write_text("version: 1\nworkflows:\n  - id: [unclosed\n", encoding="utf-8")
    with pytest.raises(WorkflowFileError, match=r"not valid YAML at line \d+"):
        load_workflows(p)


def test_empty_file(tmp_path):
    p = tmp_path / "e.yml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(WorkflowFileError, match="empty"):
        load_workflows(p)


def test_python_object_tags_are_rejected_not_executed(tmp_path):
    p = tmp_path / "evil.yml"
    p.write_text("version: 1\nworkflows:\n  - !!python/object/apply:os.system ['echo pwned']\n", encoding="utf-8")
    with pytest.raises(WorkflowFileError):
        load_workflows(p)


def test_unknown_action_is_rejected_with_suggestion():
    ps = problems(wf(steps=[{"action": "clik", "target": {"role": "button", "name": "Save"}}]))
    assert any("unknown action 'clik'" in p and "did you mean 'click'" in p for p in ps), ps


@pytest.mark.parametrize("bad_action", ["eval", "exec", "shell", "run", "python", "script", "evaluate", "bash", "javascript"])
def test_no_code_execution_actions_exist(bad_action):
    assert bad_action not in ACTIONS
    assert problems(wf(steps=[{"action": bad_action, "code": "1+1"}]))


def test_no_eval_exec_or_shell_in_engine_source():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "qa_agent").glob("*.py"))
    for needle in (r"\beval\(", r"\bexec\(", r"subprocess", r"os\.system", r"\.evaluate\(\s*st\b", r"yaml\.load\("):
        # page.evaluate with a FIXED script is allowed in discovery; workflow data must never reach it.
        if needle.startswith(r"\.evaluate"):
            continue
        assert not re.search(needle, src), f"forbidden construct {needle} in qa_agent/"


def test_missing_required_key_for_action():
    assert any("missing required key" in p and "target" in p for p in problems(wf(steps=[{"action": "click"}])))


def test_unexpected_key_in_step():
    assert any("unexpected key" in p for p in problems(wf(steps=[{"action": "goto", "path": "/", "shell": "rm -rf /"}])))


def test_unknown_workflow_key_and_bad_fields():
    ps = problems(wf(wat=1, risk="extreme", status="maybe", app="other"))
    joined = " | ".join(ps)
    assert "unknown key" in joined and "risk must be" in joined and "status must be" in joined and "app must be" in joined


@pytest.mark.parametrize("path", ["https://evil.example/x", "//evil.example", "relative", "/a/../b", "/a b", "\\\\server\\share"])
def test_goto_path_must_be_site_relative(path):
    assert any("site-relative" in p for p in problems(wf(steps=[{"action": "goto", "path": path}])))


def test_target_needs_exactly_one_selector_kind():
    assert any("exactly one" in p for p in problems(wf(steps=[{"action": "click", "target": {"css": "a", "label": "b"}}])))
    assert any("unknown target key" in p for p in problems(wf(steps=[{"action": "click", "target": {"xpath": "//a"}}])))
    assert any("unsupported ARIA role" in p for p in problems(wf(steps=[{"action": "click", "target": {"role": "buton"}}])))


def test_timeout_bounds():
    assert any("timeout_ms" in p for p in problems(wf(timeout_ms=10)))
    assert any("timeout_ms" in p for p in problems(wf(timeout_ms=10_000_000)))
    assert not problems(wf(timeout_ms=45_000))


def test_bad_regex_and_catastrophic_pattern_rejected():
    assert any("invalid regular expression" in p for p in problems(wf(steps=[{"action": "expect_url", "matches": "(["}])))
    assert any("nested quantifier" in p for p in problems(wf(steps=[{"action": "expect_url", "matches": "(a+)+$"}])))


def test_templates_are_whitelisted():
    assert not problems(wf(steps=[{"action": "fill", "target": {"label": "x"}, "value": "{{MARKER}}"}]))
    assert any("unknown template variable" in p for p in problems(wf(steps=[{"action": "fill", "target": {"label": "x"}, "value": "{{HOME}}"}])))


def test_literal_personal_data_and_secrets_are_rejected_in_values():
    for bad in ("9876543210", "jane.doe@gmail.com", "1234 5678 9012", "ghp_" + "a" * 30):
        ps = problems(wf(steps=[{"action": "fill", "target": {"label": "x"}, "value": bad}]))
        assert any("secret or personal" in p for p in ps), (bad, ps)


def test_value_env_must_be_a_name_not_a_secret():
    assert any("NAME" in p for p in problems(wf(steps=[{"action": "fill", "target": {"label": "x"}, "value_env": "my secret!"}])))
    assert not problems(wf(steps=[{"action": "fill", "target": {"label": "x"}, "value_env": "NURSE_PASSWORD"}]))


@pytest.mark.parametrize("name", ["Delete record", "Pay now", "Logout", "Send message", "Cancel booking", "Refund"])
def test_destructive_payment_messaging_targets_are_never_allowed(name):
    for action in ("click", "submit", "guarded_submit"):
        ps = problems(wf(writes=(action == "submit"), steps=[{"action": action, "target": {"role": "button", "name": name}}]))
        assert any("destructive" in p for p in ps), (action, name, ps)


def test_writes_flag_must_match_steps():
    sub = {"action": "submit", "target": {"role": "button", "name": "Save"}}
    assert any("write action but writes is false" in p for p in problems(wf(writes=False, steps=[sub])))
    assert any("no write action exists" in p for p in problems(wf(writes=True)))
    assert not problems(wf(writes=True, steps=[sub]))
    assert any("high-risk" in p for p in problems(wf(writes=True, risk="high", steps=[sub])))


def test_duplicate_ids_in_one_file_and_across_files(tmp_path):
    doc = {"version": 1, "workflows": [wf("same_id_x"), wf("same_id_x")]}
    assert any("duplicate workflow id" in p for p in validate_document(doc))
    a = write_wf_file(tmp_path, wf("same_id_x"), name="a.yml")
    b = write_wf_file(tmp_path, wf("same_id_x"), name="b.yml")
    with pytest.raises(SchemaError, match="duplicate workflow id"):
        load_all([a, b])


def test_wrong_version_and_top_level_shape():
    assert any("version must be 1" in p for p in validate_document({"version": 2, "workflows": [wf()]}))
    assert validate_document([1, 2])
    assert any("non-empty list" in p for p in validate_document({"version": 1, "workflows": []}))


def test_draft_rules_forbid_writes_and_require_sources_and_draft_status():
    ok = wf(status="draft", sources=[{"file": "src/a.jsx", "line": 3}], preconditions=["p"], expected_outcome=["o"])
    assert not problems(ok, draft_rules=True)
    assert any("status: draft" in p for p in problems(dict(ok, status="approved"), draft_rules=True))
    assert any("source files" in p for p in problems({k: v for k, v in ok.items() if k != "sources"}, draft_rules=True))
    w = dict(ok, writes=True, steps=[{"action": "submit", "target": {"role": "button", "name": "Save"}}])
    assert any("may not contain write actions" in p for p in problems(w, draft_rules=True))


def test_hash_changes_on_material_change_but_not_on_status_or_approval_metadata():
    a = wf()
    assert content_hash(a) == content_hash(dict(a, status="draft"))
    assert content_hash(a) == content_hash(dict(a, approval={"x": 1}))
    assert content_hash(a) != content_hash(wf(steps=[{"action": "goto", "path": "/other"}]))
    assert content_hash(a) != content_hash(dict(a, risk="medium"))
    assert content_hash(a) != content_hash(dict(a, app="hospital"))


def test_error_message_names_workflow_and_step():
    ps = problems(wf("my_flow_id", steps=[{"action": "goto", "path": "/"}, {"action": "click", "target": {}}]))
    assert any("my_flow_id" in p and "step 2" in p for p in ps), ps
