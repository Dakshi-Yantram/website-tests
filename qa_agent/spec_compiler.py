"""Turn app-specific semantic specification YAML into executable *drafts* for the existing QA runner.

There are two modes:
- deterministic: navigation is executable; unresolved business assertions are explicitly NOT VERIFIED.
- AI: a configured model can propose supported Playwright actions using source and optional
  staging-only browser metadata. All outputs are validated and require human approval.

AI is never allowed to approve/run workflow changes or bypass write gates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile

import yaml

from .errors import SchemaError, WorkflowFileError
from .redaction import safe_text, find_secrets, find_personal
from .schema import ACTIONS, APPS, validate_document, DANGEROUS_RX
from .workflows import DRAFTS_DIR

MAX_SPEC_FILE = 1_000_000
MAX_SPEC_FILES = 80
MAX_SNIPPET = 14000
MODULE_RX = re.compile(r"^[A-Za-z0-9_-]{1,60}$")
DYNAMIC_RX = re.compile(r"[:*\[\](){}]")
SAFE_ROUTE_RX = re.compile(r"^/(?!/)[A-Za-z0-9_./~-]*$")
SPEC_SCHEMAS = {"hospital": {"hims.agent.workflow.v1", "qa.agent.workflow.v1"},
                "nurseconnect": {"nurseconnect.agent.workflow.v1", "qa.agent.workflow.v1"}}
APP_ROLES = {"hospital": {"doctor", "reception", "lab", "radiology", "pharmacy", "finance", "hr", "nurse_hospital", "hospital_admin", "super_admin"},
             "nurseconnect": {"consumer", "nurse", "operations", "reviewer", "admin"}}

def app_for(spec):
    app = spec.get("app", "hospital")
    if app not in APPS:
        raise WorkflowFileError(f"Unsupported application {app!r}")
    return app

def role_for(spec):
    role = spec.get("role") or ""
    app = app_for(spec)
    if role == "anonymous":
        return ""
    if app == "hospital" and role == "admin":
        role = "hospital_admin"
    if role and role not in APP_ROLES[app]:
        raise WorkflowFileError(f"{spec.get('id')}: role {role!r} does not belong to {app}")
    return role



def _read_source_files(path):
    """Read a directory, single YAML file, or zip without extracting untrusted archive paths."""
    src = Path(path)
    if src.is_dir():
        files = [p for p in sorted(src.rglob('*')) if p.suffix.lower() in ('.yaml', '.yml') and p.is_file()]
        files = [p for p in files if p.name != 'agent_config.yaml']
        if len(files) > MAX_SPEC_FILES:
            raise WorkflowFileError('Too many specification YAML files')
        for f in files:
            if f.stat().st_size > MAX_SPEC_FILE:
                raise WorkflowFileError(f'Specification too large: {f.name}')
            yield f.name, f.read_text(encoding='utf-8')
    elif src.suffix.lower() == '.zip':
        with ZipFile(src) as z:
            names = [n for n in z.namelist() if n.endswith(('.yml', '.yaml')) and not n.endswith('agent_config.yaml')]
            if len(names) > MAX_SPEC_FILES:
                raise WorkflowFileError('Too many YAML files in specification archive')
            for n in sorted(names):
                if n.startswith('/') or '..' in Path(n).parts or z.getinfo(n).file_size > MAX_SPEC_FILE:
                    raise WorkflowFileError(f'Unsafe archive member: {n[:80]}')
                yield Path(n).name, z.read(n).decode('utf-8')
    elif src.is_file() and src.suffix.lower() in ('.yaml', '.yml'):
        if src.stat().st_size > MAX_SPEC_FILE:
            raise WorkflowFileError(f'Specification too large: {src}')
        yield src.name, src.read_text(encoding='utf-8')
    else:
        raise WorkflowFileError(f'Cannot find specification directory, YAML, or ZIP: {src}')


def read_specs(path):
    """Returns source-linked scenarios; accepts the original HIMS package directly."""
    out, ids = [], set()
    for name, contents in _read_source_files(path):
        try:
            doc = yaml.safe_load(contents)
        except yaml.YAMLError as e:
            raise WorkflowFileError(f'{name}: invalid YAML: {str(e)[:140]}') from e
        if not isinstance(doc, dict) or not isinstance(doc.get('workflows'), list):
            continue  # agent_config.yaml, not a scenario file
        app = doc.get('application')
        if app not in APPS:
            raise WorkflowFileError(f'{name}: application must be nurseconnect or hospital')
        if doc.get('schema_version') not in SPEC_SCHEMAS[app]:
            raise WorkflowFileError(f'{name}: unsupported schema for {app}: {doc.get("schema_version")!r}')
        for x in doc['workflows']:
            if not isinstance(x, dict) or not isinstance(x.get('id'), str):
                raise WorkflowFileError(f'{name}: workflow must have id')
            if x['id'] in ids:
                raise WorkflowFileError(f'Duplicate specification id: {x["id"]}')
            if x.get('app', app) != app:
                raise WorkflowFileError(f'{name}: {x["id"]} app conflicts with document application {app}')
            ids.add(x['id'])
            # Preserve hashes of previously published HIMS specs; nurseconnect specs
            # carry the app identity in the hashed content to prevent cross-app reuse.
            if app == 'nurseconnect':
                x = {**x, 'app': app}
            role_for(x)
            out.append((name, x))
    if not out:
        raise WorkflowFileError('No semantic specification workflows found')
    return out


def route_for(spec):
    """Only allow concrete site-relative paths. Dynamic parameters require browser navigation."""
    for anchor in spec.get('source_anchors') or []:
        if not isinstance(anchor, str):
            continue
        parsed = urlparse(anchor)
        path = parsed.path
        if SAFE_ROUTE_RX.fullmatch(path) and not DYNAMIC_RX.search(path) and not parsed.query and not parsed.fragment:
            return path
    return ''


def _safestr(s, limit=240):
    # limit untrusted text and remove accidental clinical identifiers/credentials
    return safe_text(str(s)[:limit], what='spec compiler text')


def _provenance(name, spec):
    return [{'file': f'spec:{name}#{spec["id"]}'}]


def _make_id(spec):
    s = re.sub(r'[^a-z0-9_-]', '_', spec['id'].lower()).strip('_')
    return ('spec_' + s)[:64]


def _fallback(spec, *, reason):
    path = route_for(spec)
    steps = []
    if path:
        steps.extend([
            {'action': 'goto', 'path': path, 'expected': 'Route responds'},
            {'action': 'expect_visible', 'target': {'css': 'body'}, 'expected': 'Page renders'},
        ])
    steps.append({'action': 'require_verified', 'reason': reason,
                  'expected': 'Business assertions need a real UI or API verification'})
    steps.append({'action': 'expect_no_writes_sent', 'expected': 'No business write request was sent'})
    return steps


def _model_response(prompt, model):
    """AI compiler adapter. Never passes secrets or DOM input values to the model."""
    from . import llm
    instructions = (
            'You compile untrusted test specifications into safe, verifiable Playwright YAML steps. '
            'Return ONLY a JSON object {"steps": [..], "covered_assertions": [zero_based_indexes], "covered_negative_tests": [zero_based_indexes]}. '
            'Use only these allowed actions: goto, click, fill, select_option, check, press, wait_for, '
            'open_section, login_submit, guarded_submit, submit, expect_visible, expect_hidden, '
            'expect_text, expect_url, expect_title, expect_count, expect_authenticated, '
            'expect_no_writes_sent, reload. Never use JS/eval/shell or arbitrary HTTP calls. '
            'Targets must use exactly one: role (+ optional name), label, placeholder, text, test_id, css. '
            'Do not claim assertion coverage unless a specific matching expect step actually checks it. '
            'For persistence, the workflow MUST submit synthetic data, reload or navigate away and back, '
            'and verify the same synthetic marker; otherwise leave that assertion uncovered. '
            'Do not fabricate selectors or values. Use only shown static source/DOM hints. '
            'For clinical writes use only synthetic templates {{SYNTH_NAME}}, {{SYNTH_PHONE}}, '
            '{{SYNTH_EMAIL}}, {{MARKER}} or {{RUN_ID}}. Never suggest a payment, discharge, '
            'notification, deletion or real patient change. If evidence is insufficient, return empty steps.'
    )
    txt = llm.complete(instructions, prompt, model=model, max_tokens=4000, timeout=120, json_mode=True)
    if txt.startswith('```'):
        txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', txt).strip()
    obj = json.loads(txt)
    if not isinstance(obj, dict) or not isinstance(obj.get('steps'), list):
        raise ValueError('AI compiler did not return steps array')
    return obj


def _is_meaningful_assertion(st):
    if st.get('action') not in ('expect_visible', 'expect_hidden', 'expect_text', 'expect_url',
                                'expect_title', 'expect_count'):
        return False
    return st.get('target') != {'css': 'body'}


def _safe_steps(candidate, spec):
    """Strict schema + coverage audit. Fail closed on risky or invented actions."""
    if not isinstance(candidate, dict):
        raise ValueError('Missing compiler output')
    steps = candidate.get('steps', [])
    if not isinstance(steps, list) or len(steps) > 55 or not steps:
        raise ValueError('No executable steps or too many steps')
    for step in steps:
        if not isinstance(step, dict) or step.get('action') not in ACTIONS:
            raise ValueError('Unsupported action in compiled steps')
        if step['action'] in ('submit', 'login_submit') and not spec.get('write_test'):
            raise ValueError('Compiler attempted an unapproved write-related action')
        # A model must not disguise submitting a form as a harmless 'click' or 'press Enter'.
        if step['action'] == 'click':
            label = json.dumps(step.get('target', {}), ensure_ascii=True)
            if re.search(r'(?i)save|submit|create|register|confirm|update|finish', label):
                raise ValueError('Potential form submission must use submit and staging write gates')
        if step['action'] == 'press' and step.get('key', '').lower() in ('enter', 'return'):
            raise ValueError('Form submission by Enter is not permitted in auto-compiled workflows')
        # Untrusted target labels: destructive/messaging flows must not be operated.
        if step['action'] in ('click', 'submit', 'guarded_submit', 'open_section'):
            t = step.get('target', {})
            if DANGEROUS_RX.search(json.dumps(t, ensure_ascii=True) + ' ' + str(step.get('pattern', ''))):
                raise ValueError('Dangerous operation proposed by compiler')
    assert_ids = spec.get('assertions') or []
    covered = candidate.get('covered_assertions', [])
    if not isinstance(covered, list) or any(type(x) is not int or not 0 <= x < len(assert_ids) for x in covered):
        raise ValueError('Invalid assertion indexes in compiler output')
    if covered and not any(_is_meaningful_assertion(s) for s in steps):
        covered = []
    # Covers cannot be fully trusted: require a concrete assertion action per coverage claim.
    if sum(_is_meaningful_assertion(s) for s in steps) < len(set(covered)):
        covered = []
    # Do not allow a merely rendered page to stand in for a business assertion.
    # Persistence needs an actual write, reload and a synthetic marker assertion.
    covered = list(set(covered))
    for i in list(covered):
        descr = str(assert_ids[i].get('description', '')).lower()
        if re.search(r'persist|saved after|after reload|reopen|refresh stable|save and retrieve', descr):
            has_submit = any(s['action'] == 'submit' for s in steps)
            has_reload = any(s['action'] == 'reload' for s in steps)
            has_marker = any(s['action'] == 'expect_text' and
                             any('{{' in str(s.get(k,'')) for k in ('contains','matches')) for s in steps)
            if not (has_submit and has_reload and has_marker):
                covered.remove(i)
        if re.search(r'cross.patient|cross.tenant|patient.mix|isolation|wrong doctor', descr):
            covered.remove(i) if i in covered else None
    missing = [i for i in range(len(assert_ids)) if i not in set(covered)]
    for i in missing:
        steps.append({'action': 'require_verified', 'reason': f'Assertion {i + 1} not compiled into a verified browser check: {_safestr(assert_ids[i].get("description", "unknown"), 130)}'})
    negative = spec.get('negative_and_boundary_tests') or []
    neg_cov = candidate.get('covered_negative_tests', [])
    if not isinstance(neg_cov, list) or any(type(x) is not int or not 0 <= x < len(negative) for x in neg_cov):
        neg_cov = []
    if len(set(covered)) + len(set(neg_cov)) > sum(_is_meaningful_assertion(x) for x in steps):
        neg_cov = []
    # A generic expect cannot demonstrate security/negative conditions on its own.
    # Until dedicated synthetic setup and a negative assertion are generated, report NOT VERIFIED.
    for i, desc in enumerate(negative):
        if i not in neg_cov:
            steps.append({'action': 'require_verified', 'reason': f'Negative/boundary case {i + 1} untested: {_safestr(desc, 130)}'})
    if not any(s['action'] == 'expect_no_writes_sent' for s in steps) and not any(s['action'] == 'submit' for s in steps):
        steps.append({'action': 'expect_no_writes_sent'})
    return steps


def create_workflow(name, spec, *, model=None, source=None, browser=None):
    sid = spec['id']
    original_spec_sha = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=True).encode('utf-8')).hexdigest()
    objective = _safestr(spec.get('objective') or sid, 300)
    assertions = [_safestr(x.get('description', ''), 160) for x in spec.get('assertions', []) if isinstance(x, dict)]
    route = route_for(spec)
    proposed = None
    issue = ''
    if model:
        context = {
            'source_spec_id': sid, 'application': app_for(spec), 'objective': objective,
            'source_anchors': spec.get('source_anchors', [])[:10],
            'preconditions': spec.get('preconditions', [])[:10],
            'steps': spec.get('steps', [])[:20],
            'assertions': assertions,
            'negative_tests': spec.get('negative_and_boundary_tests', [])[:10],
            'allowed_route': route or None,
            'source_evidence': source or {},
            'browser_evidence': browser or {},
            'writes_allowed_in_staging_only': bool(spec.get('write_test')),
        }
        # Redact string fields before sending source-derived materials to a third-party model.
        payload = safe_text(json.dumps(context, ensure_ascii=False)[:MAX_SNIPPET], what='AI compiler prompt')
        try:
            proposed = _model_response(payload, model)
        except Exception as e:
            issue = f'AI compilation unavailable: {type(e).__name__}: {str(e)[:110]}'
    if proposed is not None:
        try:
            steps = _safe_steps(proposed, spec)
        except (ValueError, TypeError) as e:
            issue = f'AI steps invalid: {str(e)[:130]}'
            steps = _fallback(spec, reason=issue)
    else:
        steps = _fallback(spec, reason=issue or 'Source specification provides no executable assertion selectors; configure AI compilation and staging UI evidence')
    has_writes = any(s['action'] == 'submit' for s in steps)
    role = role_for(spec)
    wf = {
        'id': _make_id(spec), 'app': app_for(spec), 'title': objective[:110],
        'description': f'Auto-compiled from {name} / {sid}. Business assertions: ' + '; '.join(map(str, assertions))[:350],
        'status': 'draft',
        'risk': 'medium' if has_writes else ('high' if spec.get('priority') == 'P0' else 'low'),
        'writes': has_writes,
        'preconditions': [_safestr(x) for x in (spec.get('preconditions') or [])[:8]],
        'expected_outcome': [_safestr(x) for x in assertions[:12]] or ['Expected behavior is verified'],
        'sources': _provenance(name, spec),
        'steps': steps,
        'generated': {'method': 'multiapp-spec-compiler', 'original_id': sid,
                      'model': model or 'deterministic-fallback',
                      'source_sha256': original_spec_sha,
                      'compiled': proposed is not None and not issue,
                      'requires_human_approval': True},
    }
    if role:
        wf['role'] = role
    if has_writes:
        wf['staging_only'] = True
    return wf, issue


def compile_package(spec_path, out_dir, *, model=None, only=(), source_lookup=None, browser_lookup=None, refresh=False, provenance=None):
    """Produce one YAML draft per specification; return a structured report."""
    target = Path(out_dir)
    specs = read_specs(spec_path)
    chosen = [x for x in specs if not only or x[1]['id'] in only]
    if not chosen:
        raise WorkflowFileError(f'No matching workflow IDs for {list(only)}')
    target.mkdir(parents=True, exist_ok=True)
    records = []
    for name, spec in chosen:
        sid = spec['id']
        wf, issue = create_workflow(name, spec, model=model,
                                    source=(source_lookup or {}).get(sid), browser=(browser_lookup or {}).get(sid))
        doc = {'version': 1, 'workflows': [wf]}
        # Generated fallback remains draft: never auto-approved.
        problems = validate_document(doc, draft_rules=not wf['writes'])
        if problems and model and not issue:
            # One bounded self-repair pass: tell the compiler why the proposed schema failed.
            # No human selector mapping, no unbounded API retries or credit drain.
            repair_source = dict((source_lookup or {}).get(sid) or {})
            repair_source['previous_draft_validation_errors'] = problems[:8]
            wf2, issue2 = create_workflow(name, spec, model=model,
                                          source=repair_source, browser=(browser_lookup or {}).get(sid))
            doc2 = {'version': 1, 'workflows': [wf2]}
            problems2 = validate_document(doc2, draft_rules=not wf2['writes'])
            if not problems2:
                wf, issue, doc, problems = wf2, issue2, doc2, []
            else:
                problems = problems2
        if problems:
            records.append({'id': sid, 'state': 'INVALID', 'problems': problems[:8]})
            continue
        dump = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
        if find_secrets(dump) or find_personal(dump):
            records.append({'id': sid, 'state': 'BLOCKED', 'problems': ['Sensitive data detected in compiled workflow']})
            continue
        path = target / (wf['id'] + '.yml')
        # Never silently overwrite an existing reviewed/edited draft.
        if path.exists() and path.read_text(encoding='utf-8') != dump:
            if not refresh:
                records.append({'id': sid, 'state': 'CONFLICT', 'problems': ['Draft exists with different content; use --refresh to archive and recompile']})
                continue
            archive = target / '_archive'
            archive.mkdir(exist_ok=True)
            # Content-addressed backup avoids clobbering a reviewer-edited draft.
            old = path.read_bytes()
            backup = archive / f'{wf["id"]}_{hashlib.sha256(old).hexdigest()[:16]}.yml'
            if not backup.exists():
                backup.write_bytes(old)
        path.write_text(dump, encoding='utf-8')
        unresolved = sum(s['action'] == 'require_verified' for s in wf['steps'])
        records.append({'id': sid, 'state': 'DRAFT', 'path': str(path), 'needs_verification': unresolved,
                        'contains_staging_write': wf['writes'], 'compile_issue': issue})
    # No sensitive user data belongs in the summary either.
    report = {'total_source_workflows': len(specs), 'selected': len(chosen), 'results': records}
    if provenance:
        report['source_provenance'] = provenance   # git SHA/branch/dirty of the checkout the compiler read
    (target / 'compilation_report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report
