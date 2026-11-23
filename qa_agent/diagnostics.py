"""Evidence-based issue triage. No external API calls by default.

Known error patterns produce *investigation hints*, never alleged verified root causes.
AI analysis is an explicit opt-in and sends only redacted step metadata; it never reads
screenshots, browser DOM, clinical records, cookies, trace files or source files.
"""
from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
import os
import re

from .redaction import redact, assert_clean_obj

BAD = ('FAIL', 'NOT VERIFIED', 'BLOCKED')


def _category(step):
    error = (step.get('error') or '').lower()
    action = step.get('action') or ''
    if action == 'login':
        return 'Authentication / credentials', 'Inspect staging credentials, login response and session creation.'
    if 'not approved' in error or 'write blocked' in error or 'staging-only' in error:
        return 'Safety or approval gate', 'Verify approval hash and explicit staging-only authorization; do not bypass safeguards.'
    if any(x in error for x in ('element not found', 'timeout', 'not visible', 'locator')):
        return 'Selector, navigation or rendering', 'Check the page URL, accessible label, selector stability, load timing and user permissions.'
    if 'http 5' in error or any(' 5' in x for x in step.get('failed_requests', [])):
        return 'Server or API response', 'Inspect matching staging API logs and correlation identifiers; verify backend validation and dependencies.'
    if 'http 4' in error or any(' 4' in x for x in step.get('failed_requests', [])):
        return 'Request rejected or missing route', 'Check route, authorization, request payload and staging data setup.'
    if 'navigation error' in error or 'net::err' in error:
        return 'Network or availability', 'Check staging host, TLS, DNS, network allowlist and application health.'
    if action == 'require_verified':
        return 'Uncompiled business assertion', 'AI compiler or tester must add a real persistence assertion; do not treat page navigation as coverage.'
    if action.startswith('expect_') or 'assert' in error:
        return 'Business assertion', 'Compare observed response and persisted state against the acceptance criterion.'
    return 'Unclassified execution issue', 'Inspect failure screenshot, browser errors, action parameters and corresponding app logs.'


def _signature(w, step, category):
    # Group repeated symptoms, not allegedly identical root causes.
    msg = (step.get('error') or '').lower()
    msg = re.sub(r'https?://[^\s]+', '<url>', msg)
    msg = re.sub(r'\d+', '#', msg)
    msg = msg[:120]
    key = '|'.join((w.get('app', ''), category, step.get('action', ''), msg))
    return sha256(key.encode()).hexdigest()[:12]


def generate_findings(workflows):
    """Return sanitized findings with repeat-symptom grouping and explicit uncertainty."""
    groups = defaultdict(list)
    for w in workflows:
        for step in w.get('steps', []):
            if step.get('status') not in BAD:
                continue
            cat, hint = _category(step)
            sid = _signature(w, step, cat)
            groups[sid].append((w, step, cat, hint))
        if w.get('status') in BAD and not any(s.get('status') in BAD for s in w.get('steps', [])):
            # A blocked approval/environment gate happens before a browser page exists.
            fake = {'n': -1, 'action': 'preflight', 'status': w['status'], 'error': w.get('reason', ''),
                    'expected': 'All execution gates pass', 'actual': 'No browser execution', 'screenshot': '',
                    'url': '', 'failed_requests': [], 'console_errors': []}
            cat, hint = _category(fake)
            groups[_signature(w, fake, cat)].append((w, fake, cat, hint))
    out = []
    for sig, entries in groups.items():
        w, st, category, hint = entries[0]
        severity = st.get('severity') or ('High' if w.get('risk') == 'high' and st.get('status') == 'FAIL' else 'Medium' if st.get('status') == 'FAIL' else 'Informational')
        out.append({'id': 'ISSUE-' + sig, 'type': category, 'status': st['status'], 'severity': severity,
                    'confidence': 'Unconfirmed - investigation suggestion only',
                    'summary': redact(st.get('error') or w.get('reason') or category),
                    'expected': redact(st.get('expected', '')), 'actual': redact(st.get('actual', '')),
                    'screenshot': st.get('screenshot', ''), 'url': redact(st.get('url', '')),
                    'console_errors': [redact(x) for x in st.get('console_errors', [])[:8]],
                    'failed_requests': [redact(x) for x in st.get('failed_requests', [])[:8]],
                    'investigate': hint,
                    'occurrences': [{'workflow': row[0]['id'], 'step': row[1]['n'], 'status': row[1]['status']}
                                    for row in entries], 'count': len(entries)})
    out.sort(key=lambda x: ({'FAIL': 0, 'NOT VERIFIED': 1, 'BLOCKED': 2}.get(x['status'], 3), -x['count'], x['id']))
    assert_clean_obj(out, what='issue findings')
    return out


def enrich_with_ai(findings, *, env_kind: str = ''):
    """Optional metadata-only diagnosis. Never request AI in production or without explicit consent.

    This intentionally excludes customer/patient values, screenshots, DOM snapshots, logs and source.
    """
    if os.getenv('QA_AI_DIAGNOSIS', '').lower() not in ('true', '1'):
        return findings
    if env_kind not in ('local', 'staging'):
        return findings
    from . import llm
    if not llm.is_configured():
        return findings
    model = (os.getenv('QA_DIAGNOSIS_MODEL', '') or llm.default_model()).strip()
    if not model:
        return findings
    for finding in findings[:8]:
        evidence = {k: finding[k] for k in ('type', 'status', 'summary', 'expected', 'investigate')}
        try:
            assert_clean_obj(evidence, what='AI diagnostic prompt')
            answer = llm.complete('You analyze synthetic software test failures. Give one short hypothesis and a developer verification step. '
                                  'Never claim a root cause is proven, never suggest disabling security gates, never ask for patient data.',
                                  json.dumps(evidence), model=model, max_tokens=180, timeout=25, retries=0)
            hypothesis = redact(answer[:750])
            if hypothesis:
                assert_clean_obj(hypothesis, what='AI diagnosis response')
                finding['ai_hypothesis_unverified'] = hypothesis
        except Exception:
            finding['ai_diagnosis_status'] = 'Unavailable; deterministic investigation guidance retained'
    return findings
