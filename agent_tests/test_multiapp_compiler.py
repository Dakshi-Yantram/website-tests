"""Prevent a Nurse Connect spec from targeting Hospital source or execution."""
import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

from qa_agent.errors import WorkflowFileError
from qa_agent.spec_compiler import read_specs, create_workflow, compile_package, app_for
from qa_agent.spec_evidence import source_hints
from qa_agent.schema import validate_document

NURSE = {
    'schema_version': 'nurseconnect.agent.workflow.v1',
    'application': 'nurseconnect',
    'workflows': [{
        'id': 'NC-BOOK-001', 'role': 'consumer', 'priority': 'P1',
        'objective': 'Verify nearby nursing booking appears after refresh',
        'source_anchors': ['/consumer'], 'write_test': False,
        'preconditions': ['Use the staging consumer account'],
        'steps': [{'action': 'navigate_or_call', 'description': 'Open bookings'}],
        'assertions': [{'description': 'Booking is visible after refresh'}],
        'negative_and_boundary_tests': ['Unavailable dates are not offered'],
    }]
}


def write_spec(tmp_path, doc=NURSE):
    dest = tmp_path / 'nurse.yml'
    dest.write_text(yaml.safe_dump(doc, sort_keys=False), encoding='utf-8')
    return dest


def test_nurse_document_routes_to_nurseconnect(tmp_path):
    source = write_spec(tmp_path)
    entries = read_specs(source)
    assert len(entries) == 1
    assert entries[0][1]['app'] == 'nurseconnect'
    wf, warning = create_workflow(*entries[0])
    assert wf['app'] == 'nurseconnect'
    assert wf['role'] == 'consumer'
    assert not warning
    assert wf['generated']['source_sha256'] == hashlib.sha256(json.dumps(entries[0][1], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    assert not validate_document({'version': 1, 'workflows': [wf]}, draft_rules=True)
    assert any(s['action'] == 'require_verified' for s in wf['steps'])


def test_app_mismatch_rejected(tmp_path):
    doc = {**NURSE, 'workflows': [{**NURSE['workflows'][0], 'app': 'hospital'}]}
    with pytest.raises(WorkflowFileError, match='conflicts'):
        read_specs(write_spec(tmp_path, doc))


def test_role_mismatch_rejected(tmp_path):
    doc = {**NURSE, 'workflows': [{**NURSE['workflows'][0], 'role': 'doctor'}]}
    with pytest.raises(WorkflowFileError, match='does not belong'):
        read_specs(write_spec(tmp_path, doc))


def test_nurse_schema_not_accepted_as_hims(tmp_path):
    doc = {**NURSE, 'application': 'hospital'}
    with pytest.raises(WorkflowFileError, match='unsupported schema'):
        read_specs(write_spec(tmp_path, doc))


def test_updated_spec_requires_refresh_then_generates_new_hash(tmp_path):
    doc = write_spec(tmp_path)
    out = tmp_path / 'drafts'
    assert compile_package(doc, out)['results'][0]['state'] == 'DRAFT'
    first = yaml.safe_load((out/'spec_nc-book-001.yml').read_text())['workflows'][0]
    updated = {**NURSE, 'workflows': [{**NURSE['workflows'][0], 'objective': 'Updated customer booking check'}]}
    write_spec(tmp_path, updated)
    assert compile_package(doc, out)['results'][0]['state'] == 'CONFLICT'
    assert compile_package(doc, out, refresh=True)['results'][0]['state'] == 'DRAFT'
    second = yaml.safe_load((out/'spec_nc-book-001.yml').read_text())['workflows'][0]
    assert first['generated']['source_sha256'] != second['generated']['source_sha256']
    assert list((out/'_archive').glob('spec_nc-book-001_*.yml'))


def test_source_analysis_receives_correct_app(monkeypatch, tmp_path):
    seen = []
    from qa_agent import discovery
    def fake_repo(path, app):
        seen.append((path, app))
        return {'routes': [], 'forms': [], 'controls': []}
    monkeypatch.setattr(discovery, 'analyze_repo', fake_repo)
    specs = read_specs(write_spec(tmp_path))
    hints = source_hints(tmp_path, specs, app='nurseconnect')
    assert seen == [(tmp_path, 'nurseconnect')]
    assert 'NC-BOOK-001' in hints


def test_cli_refuses_wrong_source_repo_binding(tmp_path, capsys):
    from agent import build_parser, cmd_compile_spec
    path = write_spec(tmp_path)
    args = build_parser().parse_args(['compile-spec', '--spec', str(path), '--app', 'nurseconnect',
                                      '--repo', 'hospital=/some/hospital/source'])
    assert cmd_compile_spec(args) == 2
    assert 'does not match' in capsys.readouterr().out


def test_cli_refuses_wrong_app(tmp_path, capsys):
    from agent import build_parser, cmd_compile_spec
    path = write_spec(tmp_path)
    args = build_parser().parse_args(['compile-spec', '--spec', str(path), '--app', 'hospital',
                                      '--repo', 'nurseconnect=/tmp/nurse'])
    assert cmd_compile_spec(args) == 2
    assert 'no source was inspected' in capsys.readouterr().out


def test_cli_autobinds_checked_out_repo(tmp_path, capsys, monkeypatch):
    import agent
    spec = write_spec(tmp_path)
    monkeypatch.setattr(agent, 'ROOT', tmp_path)
    repo = tmp_path / 'apps' / 'nurseconnect'
    repo.mkdir(parents=True)
    (repo / 'src.jsx').write_text('<button>Search</button>')
    args = agent.build_parser().parse_args(['compile-spec', '--spec', str(spec), '--out', str(tmp_path / 'out')])
    assert agent.cmd_compile_spec(args) == 0
    assert (tmp_path / 'out' / 'spec_nc-book-001.yml').exists()
    assert 'Compiled nurseconnect' in capsys.readouterr().out
