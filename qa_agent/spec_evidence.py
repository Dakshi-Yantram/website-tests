"""Minimal source and staging UI evidence for semantic-to-Playwright compilation.

Only static form/button metadata is collected; no passwords, field values, table contents,
patient records, HTTP response bodies, screenshots or browser cookies are sent to AI.
"""
from pathlib import Path

from .redaction import safe_text
from .safety import normalize_origin, verify_environment, RequestGuard, SafetyBlocked


def source_hints(repo_path, specs, *, app="hospital"):
    """Use only the repository explicitly selected for this application."""
    """Read source metadata once for all specs; never read .env or database dumps."""
    from .discovery import analyze_repo
    from .spec_compiler import route_for
    source = analyze_repo(Path(repo_path), app)
    routes, forms, controls = source.get('routes', []), source.get('forms', []), source.get('controls', [])
    out = {}
    for name, spec in specs:
        route = route_for(spec)
        linked = [r for r in routes if r.get('route') == route]
        files = {r.get('source_file') for r in linked} | {r.get('component_file') for r in linked}
        # Keep only short, non-patient metadata. No full source is sent externally.
        context = {'matched_routes': linked[:4],
                   'form_controls': [f for f in forms if f.get('source_file') in files][:12],
                   'visible_controls': [c for c in controls if c.get('source_file') in files][:20]}
        out[spec['id']] = context
    return out


_JS = r"""() => {
 const nodes=[...document.querySelectorAll('input,textarea,select,button,a,[role="button"],[data-testid]')].slice(0,90);
 return nodes.filter(e => !e.closest('table,[role="row"],[data-patient-id]')).map(e=>({
  tag:e.tagName.toLowerCase(),role:e.getAttribute('role')||'',
  test_id:(e.getAttribute('data-testid')||'').slice(0,64),
  label:(e.getAttribute('aria-label')||'').slice(0,90),
  placeholder:(e.getAttribute('placeholder')||'').slice(0,90),
  name:(e.getAttribute('name')||'').slice(0,70),
  type:(e.getAttribute('type')||'').slice(0,20),
  id:(e.getAttribute('id')||'').slice(0,70),
  text:(['BUTTON','A'].includes(e.tagName)?(e.textContent||'').trim().slice(0,65):''),
  href:(e.tagName==='A' && /^\/[a-z/_-]*$/i.test(e.getAttribute('href')||'')?(e.getAttribute('href')||'').slice(0,100):'')
 })).filter(x=>x.test_id||x.label||x.placeholder||x.name||x.text||x.href);
}"""


def staging_ui_hints(specs, *, env, app='hospital', limit=12):
    """Never inspect production; never copy patient-visible body text to an AI model."""
    if not env or env.kind not in ('staging', 'local'):
        raise SafetyBlocked('UI compilation requires a declared staging/local environment')
    if app not in env.base_urls:
        raise SafetyBlocked(f'{app} staging base URL missing from environment {env.name}')
    origin = normalize_origin(env.base_url(app))
    if origin not in env.allowed_origins or origin in env.production_origins:
        raise SafetyBlocked('Staging origin is not verified or is production')
    verify_environment(env, app)
    from playwright.sync_api import sync_playwright
    from .runner import default_login
    from .spec_compiler import route_for, role_for
    hints, seen = {}, {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for _, spec in specs:
                if len(seen) >= limit:
                    break
                path = route_for(spec)
                role = role_for(spec)
                if not path:
                    continue
                cache_key = (path, role)
                if cache_key not in seen:
                    context = browser.new_context(accept_downloads=False, service_workers='block')
                    guard = RequestGuard(env, origin, writes_enabled=False, auth_any_origin=False)
                    guard.attach(context)
                    page = context.new_page()
                    try:
                        if role:
                            default_login(page, role, origin)
                        resp = page.goto(origin + path, wait_until='domcontentloaded', timeout=12000)
                        if resp and resp.status >= 400:
                            raise RuntimeError(f'HTTP {resp.status}')
                        raw = page.evaluate(_JS)
                        # No field values or arbitrary patient record text. Safe redaction applied.
                        seen[cache_key] = {'path': path, 'status': 'inspected',
                                           'controls': [{k: safe_text(v, what='UI control') for k,v in rec.items()}
                                                        for rec in raw][:65]}
                    except Exception as e:
                        seen[cache_key] = {'path': path, 'status': 'unavailable', 'reason': type(e).__name__}
                    finally:
                        context.close()
                hints[spec['id']] = seen[cache_key]
        finally:
            browser.close()
    return hints
