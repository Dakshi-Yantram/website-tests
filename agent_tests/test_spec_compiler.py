"""Regression tests for semantic HIMS specification compilation."""
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
import yaml

from qa_agent.spec_compiler import (read_specs, compile_package, route_for, _safe_steps,
                                    create_workflow)
from qa_agent.schema import validate_document
from qa_agent.errors import WorkflowFileError, StepNotVerified
from qa_agent.runner import RunConfig, Runner

SPEC = {
    'id': 'OPD-001', 'role': 'doctor', 'priority': 'P0',
    'objective': 'Test OPD patient workflow',
    'source_anchors': ['/hospital-dashboard/opd'],
    'preconditions': ['Use synthetic staging patient only'],
    'assertions': [{'description': 'Persisted record appears after reload'}],
    'write_test': False,
}


def make_src(tmp_path, spec=None):
    p = tmp_path / 'opd.yaml'
    p.write_text(yaml.safe_dump({'schema_version': 'hims.agent.workflow.v1',
                                 'application': 'hospital', 'workflows': [spec or SPEC]}), encoding='utf-8')
    return p


def test_read_and_compile_minimal_spec(tmp_path):
    src = make_src(tmp_path)
    inputs = read_specs(src)
    assert len(inputs) == 1
    report = compile_package(src, tmp_path / 'drafts')
    assert report['selected'] == 1 and report['results'][0]['state'] == 'DRAFT'
    path = Path(report['results'][0]['path'])
    doc = yaml.safe_load(path.read_text())
    assert not validate_document(doc, draft_rules=True)
    wf = doc['workflows'][0]
    assert wf['status'] == 'draft'
    assert wf['steps'][0] == {'action': 'goto', 'path': '/hospital-dashboard/opd', 'expected': 'Route responds'}
    assert any(s['action'] == 'require_verified' for s in wf['steps'])
    assert wf['writes'] is False
    assert report['results'][0]['needs_verification'] == 1


def test_dynamic_route_is_not_fabricated():
    spec = {**SPEC, 'source_anchors': ['/hospital-dashboard/opd/:patientTimelineID']}
    assert route_for(spec) == ''
    wf, _ = create_workflow('test.yml', spec)
    assert not any(s['action'] == 'goto' for s in wf['steps'])
    assert wf['steps'][0]['action'] == 'require_verified'


def test_no_api_write_in_read_only_spec():
    bad = {'steps': [{'action': 'goto', 'path': '/x'}, {'action': 'submit', 'target': {'role': 'button', 'name': 'Save'}}],
           'covered_assertions': [0]}
    with pytest.raises(ValueError, match='unapproved write'):
        _safe_steps(bad, SPEC)


def test_unsupported_action_rejected():
    bad = {'steps': [{'action': 'python', 'code': 'print(1)'}], 'covered_assertions': []}
    with pytest.raises(ValueError, match='Unsupported'):
        _safe_steps(bad, SPEC)


def test_claimed_business_assertion_needs_real_expect_step():
    proposal = {'steps': [{'action': 'goto', 'path': '/hospital-dashboard/opd'}], 'covered_assertions': [0]}
    steps = _safe_steps(proposal, SPEC)
    assert any(s['action'] == 'require_verified' for s in steps)


def test_model_generated_write_requires_staging_and_approval():
    source = {**SPEC, 'write_test': True}
    proposed = {'steps': [{'action': 'goto', 'path': '/hospital-dashboard/opd'},
                          {'action': 'fill', 'target': {'label': 'Patient Name'}, 'value': '{{SYNTH_NAME}}'},
                          {'action': 'submit', 'target': {'role': 'button', 'name': 'Save'}},
                          {'action': 'reload'},
                          {'action': 'expect_text', 'contains': '{{SYNTH_NAME}}'}],
                'covered_assertions': [0]}
    import qa_agent.spec_compiler as c
    old = c._model_response
    c._model_response = lambda *_: proposed
    try:
        wf, issue = c.create_workflow('opd.yml', source, model='test-model')
    finally:
        c._model_response = old
    assert not issue
    assert wf['writes'] and wf['staging_only'] and wf['status'] == 'draft'
    assert wf['generated']['requires_human_approval'] is True
    assert not validate_document({'version': 1, 'workflows': [wf]})


def test_unverified_action_never_passes():
    runner = Runner(RunConfig())
    with pytest.raises(StepNotVerified):
        runner._do(None, None, None, {'action': 'require_verified', 'reason': 'No persistence check'},
                   '', '', {}, 1000, None)


def test_unsafe_zip_member_rejected(tmp_path):
    p = tmp_path / 'bad.zip'
    with ZipFile(p, 'w') as z:
        z.writestr('../escape.yaml', 'x')
    with pytest.raises(WorkflowFileError):
        read_specs(p)


def test_compiler_does_not_overwrite_edited_workflow(tmp_path):
    src = make_src(tmp_path)
    out = tmp_path / 'drafts'
    report = compile_package(src, out)
    f = Path(report['results'][0]['path'])
    f.write_text('changed by reviewer')
    second = compile_package(src, out)
    assert second['results'][0]['state'] == 'CONFLICT'
    assert f.read_text() == 'changed by reviewer'
