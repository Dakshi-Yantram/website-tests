"""Regression tests for self-contained failure evidence and conservative result statuses."""
import json
from pathlib import Path

from qa_agent import reporting
from qa_agent.runner import StepResult, WorkflowResult, _roll_up, Runner, RunConfig
from qa_agent.safety import Environment

PNG = bytes.fromhex('89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000b49444154789c636000020000050001a5f645400000000049454e44ae426082')


def _meta():
    return dict(run_id='synth42', environment='local', environment_kind='local', mode='read-only',
                started='2026-10-30T00:00:00Z', finished='2026-10-30T00:00:01Z',
                browser_version='chromium', python='3.11', platform='Linux')


def test_report_packages_actual_png_and_links_it_in_html_json_markdown(tmp_path):
    original = tmp_path / 'artifacts' / 'screenshots' / 'synth42_flow_02_FAIL.png'
    original.parent.mkdir(parents=True)
    original.write_bytes(PNG)
    failed = StepResult(n=2, action='expect_visible', expected='Visible confirmation',
                        actual='No confirmation', status='FAIL', severity='High',
                        error='Element not found within 700 ms', screenshot=str(original),
                        timestamp='2026-10-30T00:00:01Z')
    workflow = WorkflowResult('flow', 'hospital', 'Synthetic visit', 'FAIL', risk='high', steps=[failed])
    paths = reporting.write_reports([workflow], _meta(), tmp_path / 'artifacts' / 'reports')
    dest = Path(paths['html']).parent / 'screenshots' / original.name
    assert dest.read_bytes() == PNG
    markup = Path(paths['html']).read_text()
    assert '<img' in markup and 'screenshots/synth42_flow_02_FAIL.png' in markup
    assert 'Issues requiring investigation' in markup
    report = json.loads(Path(paths['json']).read_text())
    assert len(report['findings']) == 1
    assert report['workflows'][0]['steps'][0]['screenshot_link'] == 'screenshots/synth42_flow_02_FAIL.png'
    assert 'investigate' in report['findings'][0] and report['findings'][0]['confidence'].startswith('Unconfirmed')
    assert 'view screenshot' in Path(paths['md']).read_text()


def test_preflight_blocked_has_issue_without_fake_screenshot(tmp_path):
    blocked = WorkflowResult('no_approval', 'hospital', 'Not approved', 'BLOCKED',
                             reason='NOT APPROVED (draft): human review needed', steps=[])
    paths = reporting.write_reports([blocked], _meta(), tmp_path / 'reports')
    report = json.loads(Path(paths['json']).read_text())
    assert report['findings'][0]['status'] == 'BLOCKED'
    assert not report['findings'][0].get('screenshot_link')


def test_repeat_failures_grouped_by_symptom_not_claimed_root_cause(tmp_path):
    workflows = [WorkflowResult(f'f{i}', 'hospital', 'T', 'FAIL', steps=[StepResult(1, 'click',
                 expected='Button clickable', error='Element not found within 300 ms', status='FAIL')]) for i in (1, 2)]
    doc = json.loads(Path(reporting.write_reports(workflows, _meta(), tmp_path / 'reports')['json']).read_text())
    assert len(doc['findings']) == 1 and doc['findings'][0]['count'] == 2
    assert 'Unconfirmed' in doc['findings'][0]['confidence']


def test_required_untested_step_cannot_be_pass(tmp_path):
    assert _roll_up([StepResult(1, 'goto', 'opens', status='PASS'),
                     StepResult(2, 'require_verified', 'persisted', status='NOT TESTED')]) == 'NOT TESTED'
    assert _roll_up([StepResult(1, 'goto', 'opens', status='PASS')]) == 'PASS'


def test_ci_never_allows_unmasked_runner_screenshots(monkeypatch):
    monkeypatch.setenv('CI', 'true')
    runner = Runner(RunConfig(screenshot_mode='full'))
    assert runner.shot_mode == 'off'  # unknown/production context never records patient screenshots


def test_capture_failure_records_missing_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr('qa_agent.runner.safe_screenshot', lambda *a, **kw: '')
    runner = Runner(RunConfig(artifacts_dir=tmp_path, screenshot_mode='masked', env=Environment(name='test',kind='local')))
    step = StepResult(0, 'login', 'success', status='BLOCKED')
    runner._capture_failure(object(), {'id': 'test'}, step, 0)
    assert not step.screenshot
    assert 'unavailable' in step.screenshot_note.lower()
