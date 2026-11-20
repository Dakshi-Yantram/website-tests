"""Autonomous, READ-ONLY explorer: give it a URL and test-account credentials, it does the rest.

What it does by itself (nobody has to write workflow steps or selectors):
  1. finds the login form, signs in as every role whose credentials are configured (retries a 5xx login, never a 401/403)
  2. discovers the app: sidebar/menu items (links AND click-only menus), in-page links, optional --seed paths
  3. on every page: HTTP status, redirect to login, blank page, error text, console errors, failing API calls, slow load,
     broken images, horizontal overflow, unlabeled inputs/buttons
  4. interacts safely: submits empty required forms, opens "Add/New/Create" dialogs (and validates them empty), clicks tabs,
     runs an unmatched search - and records the validation messages it observed
  5. security smoke: can an anonymous visitor open pages that only a logged-in role could see? can another role?
  6. writes a bug report (HTML + Markdown + JSON) - see qa_agent/explore_report.py

Safety (defence in depth, same RequestGuard as the workflow runner):
  * after login EVERY non-GET request is blocked at the network layer (block_all) - nothing can be created/changed/deleted
  * buttons/links whose text looks destructive, payment or messaging related are never clicked (schema.DANGEROUS_RX + config.DANGEROUS)
  * no synthetic data is typed into forms; only the empty-submit and an unmatched search term
  * screenshots follow the same privacy policy as the runner: off for production/unknown environments unless the operator
    explicitly asks for masked ones
  * every string that reaches a report is redacted and ids in URLs are replaced by ':id'
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .artifacts import safe_name, safe_screenshot, screenshot_mode
from .redaction import redact, register_secret
from .safety import Environment, RequestGuard, normalize_origin
from .schema import DANGEROUS_RX

SEVERITIES = ("High", "Medium", "Low", "Info")
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}

LOGIN_URL_RX = re.compile(r"/(?:auth/)?(?:login|signin|sign-in)\b", re.I)
FALLBACK_LOGIN_PATHS = ("/login", "/auth/login", "/signin", "/sign-in", "/")

ERROR_TEXT_RX = re.compile(
    r"something went wrong|internal server error|unexpected error|an error (?:has )?occurred|failed to (?:load|fetch)|"
    r"cannot read propert(?:y|ies)|is not defined|\bundefined\b|\bNaN\b|\[object Object\]|traceback \(most recent|"
    r"stack ?trace|nullpointer|404\s*[-:]?\s*not found|page not found|service unavailable|bad gateway|"
    r"application error|chunkloaderror|loading chunk \d+ failed", re.I)

SUBMIT_RX = re.compile(r"^\W*(save|submit|register|create|add|update|send|book|confirm|continue|next|proceed|apply|generate|schedule|admit|assign|issue|sign ?up)\b", re.I)
OPEN_RX = r"^\W*(add|new|create|register|invite|upload|schedule|book|generate|assign|issue)\b"

DIALOG_SEL = ('[role="dialog"], [aria-modal="true"], .MuiDialog-root, .MuiDrawer-root, .modal.show, .modal.in, '
              '.ant-modal-wrap, .swal2-popup, [class*="modal" i][class*="open" i]')
FEEDBACK_SEL = ('[role="alert"], [role="status"], [aria-live]:not([aria-live="off"]), [data-sonner-toast], [class*="toast" i], [class*="snack" i], '
                '.Mui-error, .MuiAlert-root, .Toastify__toast, .ant-message, .ant-form-item-explain-error, .invalid-feedback, '
                '.text-danger, .text-red-500, .swal2-popup, [class*="error" i], [class*="invalid" i], [aria-invalid="true"]')
NOISE_RX = re.compile(r"^(loading|please wait|signing in|logging in|log ?in|sign ?in|close|ok|x|×)\W*$", re.I)
_ID_RX = re.compile(r"/(?:\d{2,}|[0-9a-f]{24}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=/|$|\?)", re.I)

# --------------------------------------------------------------------------------------------- browser-side helpers
_VIS = "const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };"

PAGE_FACTS_JS = "() => {" + _VIS + r"""
  const q = s => [...document.querySelectorAll(s)].filter(vis);
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const broken = q('img').filter(i => i.complete && i.naturalWidth === 0 && i.src && !i.src.startsWith('data:')).length;
  const unlabeled = q('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]),select,textarea').filter(i => {
    if (i.getAttribute('aria-label') || i.getAttribute('aria-labelledby') || i.placeholder || i.title) return false;
    if (i.id && document.querySelector('label[for="' + CSS.escape(i.id) + '"]')) return false;
    if (i.closest('label')) return false;
    const p = i.parentElement && i.parentElement.parentElement;
    return !(p && p.querySelector('label'));
  }).length;
  const unnamed = q('button,[role=button]').filter(b => !(b.innerText || '').trim() && !b.getAttribute('aria-label') && !b.title
        && !b.querySelector('img[alt]:not([alt=""])')).length;
  return {
    title: document.title || '',
    headings: q('h1,h2,h3').map(txt).filter(Boolean).slice(0, 12),
    links: q('a[href]').map(a => ({text: txt(a), href: a.href})).slice(0, 250),
    tabs: q('[role=tab]').map(txt).filter(Boolean).slice(0, 12),
    searchBoxes: q('input[type=search],input[placeholder*=search i],input[name*=search i]').length,
    inputs: q('input:not([type=hidden]),textarea,select').length,
    forms: q('form').length, tables: q('table,[role=grid],[role=table]').length,
    buttons: q('button,[role=button],input[type=submit]').length,
    brokenImages: broken, unlabeledInputs: unlabeled, unnamedButtons: unnamed,
    overflowX: document.documentElement.scrollWidth - window.innerWidth,
    nodes: document.getElementsByTagName('*').length,
    textLen: (document.body ? document.body.innerText : '').trim().length
  };
}"""

# The sidebar = the smallest tall, narrow column hugging the left edge (works for <div> menus with no <nav>/<aside>); nav/aside/[class*=sidebar] also count.
SIDE_FN = r"""
  const H = window.innerHeight, W = window.innerWidth;
  let side = null, sideArea = 0;
  for (const e of document.querySelectorAll('aside,nav,[role=navigation],section,div,ul')) {
    const r = e.getBoundingClientRect();
    if (r.left > 60 || r.width < 110 || r.width > W * 0.35 || r.height < H * 0.5 || !vis(e)) continue;
    const area = r.width * r.height;
    if (!side || area < sideArea) { side = e; sideArea = area; }
  }
  const inSide = e => !!(e.closest('nav,aside,[role=navigation],[class*=sidebar i]') || (side && side.contains(e)));
"""

MENU_JS = "(loose) => {" + _VIS + SIDE_FN + r"""
  document.querySelectorAll('[data-qa-menu]').forEach(e => e.removeAttribute('data-qa-menu'));
  const out = [], seen = new Set();
  for (const e of document.querySelectorAll('a,button,[role=button],[role=menuitem],[role=link],[role=treeitem],li,div,span')) {
    if (!vis(e)) continue;
    const r = e.getBoundingClientRect();
    if (!(inSide(e) || e.closest('header') || (loose && r.left < W * 0.4))) continue;
    if (r.width > 420 || r.height > 90 || r.height < 16) continue;
    const text = (e.innerText || '').trim().replace(/\s+/g, ' ');
    if (text.length < 2 || text.length > 40 || seen.has(text)) continue;
    const a = e.tagName === 'A' ? e : e.closest('a');
    if (!(a || getComputedStyle(e).cursor === 'pointer' || e.getAttribute('role'))) continue;
    seen.add(text);
    e.setAttribute('data-qa-menu', String(out.length));
    out.push({idx: out.length, text, href: a ? a.href : ''});
    if (out.length >= 80) break;
  }
  return out;
}"""

OPEN_BUTTONS_JS = "(rx) => {" + _VIS + SIDE_FN + r"""
  const R = new RegExp(rx, 'i'), out = [], seen = new Set();
  for (const b of document.querySelectorAll('button,[role=button],a[role=button],input[type=button]')) {
    if (!vis(b) || inSide(b) || b.closest('header')) continue;
    const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 40 || !R.test(t) || seen.has(t)) continue;
    seen.add(t); out.push(t);
  }
  return out.slice(0, 6);
}"""

# Candidate submit buttons inside a scope (page body or a dialog): marked data-qa-btn so Python can click exactly that element.
SUBMIT_JS = "(scope) => {" + _VIS + SIDE_FN + r"""
  document.querySelectorAll('[data-qa-btn]').forEach(e => e.removeAttribute('data-qa-btn'));
  const out = [];
  for (const b of scope.querySelectorAll('button,input[type=submit],[role=button]')) {
    if (!vis(b) || inSide(b) || b.closest('header')) continue;
    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
    const t = (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().replace(/\s+/g, ' ');
    if (!t || t.length > 40) continue;
    b.setAttribute('data-qa-btn', String(out.length));
    out.push({idx: out.length, label: t});
  }
  return out;
}"""

FORM_FACTS_JS = "(el) => {" + _VIS + r"""
  const inputs = [...el.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]):not([type=search]),select,textarea')].filter(vis);
  const empty = inputs.filter(i => !(i.value || '').trim()).length;
  const req = inputs.filter(i => i.required || i.getAttribute('aria-required') === 'true').length
    + [...el.querySelectorAll('label,legend')].filter(l => vis(l) && /\*/.test(l.innerText || '')).length
    + inputs.filter(i => /\*\s*$/.test(i.placeholder || '')).length;
  return {inputs: inputs.length, empty, required: req};
}"""

FEEDBACK_JS = "(sel) => {" + _VIS + r"""
  const t = [...document.querySelectorAll(sel)].filter(vis).map(e => (e.innerText || '').trim().replace(/\s+/g, ' ')).filter(x => x && x.length < 200);
  return {texts: [...new Set(t)], invalid: document.querySelectorAll('input:invalid,[aria-invalid="true"]').length};
}"""

OBSERVER_JS = """() => {
  window.__qaSeen = [];
  if (window.__qaObs) { try { window.__qaObs.disconnect(); } catch (e) {} }
  const add = t => { t = (t || '').replace(/\\s+/g, ' ').trim(); if (t && t.length < 200) window.__qaSeen.push(t); };
  const grab = n => { if (n.nodeType === 3) add(n.textContent); else if (n.nodeType === 1) add(n.innerText || n.textContent); };
  window.__qaObs = new MutationObserver(ms => ms.forEach(m => { m.addedNodes.forEach(grab); if (m.type === 'characterData') add(m.target.textContent); }));
  window.__qaObs.observe(document.documentElement, {childList: true, subtree: true, characterData: true});
}"""


class ExplorerGuard(RequestGuard):
    """RequestGuard + an OPERATOR-declared allowlist of read-only POST endpoints (many back ends use POST for list/search calls).
    Nothing is allowed unless the operator passes --allow-read-post REGEX, and only on the app's own origin (or declared auth/allowed origins)."""

    def __init__(self, *a, read_post_rx=None, fill_mode=False, **kw):
        super().__init__(*a, **kw)
        self.fill_mode = fill_mode
        self.read_post_rx = read_post_rx
        self.allowed_reads: list = []

    deny_rx = re.compile(r"pay(?:ment|ments|u|tm)?\b|checkout|razorpay|stripe|billdesk|refund|payout|webhook|upi|wallet", re.I)

    def decide(self, method, url):
        if self.fill_mode and method.upper() not in ("GET", "HEAD", "OPTIONS"):
            path = urlsplit(url).path or "/"
            if method.upper() == "DELETE":
                return False, "fill mode: DELETE is never allowed"
            if self.deny_rx.search(path):
                return False, "fill mode: payment-related endpoints are never automated"
        if method.upper() == "POST" and self.read_post_rx is not None:
            try:
                origin, path = normalize_origin(url), urlsplit(url).path or "/"
            except Exception:
                return super().decide(method, url)
            ok_origin = origin == self.app_origin or bool(self.env and origin in (*self.env.auth_origins, *self.env.allowed_origins))
            if ok_origin and self.read_post_rx.search(path):
                self.allowed_reads.append(f"POST {path[:80]}")
                return True, "operator-declared read-only POST"
        return super().decide(method, url)


# --------------------------------------------------------------------------------------------- data model
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_path(url: str) -> str:
    """Path (+ hash route) with ids replaced by ':id' and no query string: safe to print in a report."""
    u = urlsplit(url)
    p = _ID_RX.sub("/:id", (u.path.rstrip("/") or "/"))
    if u.fragment.startswith("/"):
        p += "#" + _ID_RX.sub("/:id", u.fragment.split("?")[0])
    return p


@dataclass
class Finding:
    key: str
    severity: str
    category: str
    title: str
    detail: str
    app: str
    role: str
    pages: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    repro: list = field(default_factory=list)
    screenshot: str = ""
    occurrences: int = 1
    id: str = ""


@dataclass
class PageRecord:
    app: str
    role: str
    path: str
    via: str = ""
    title: str = ""
    status: int | None = None
    load_s: float = 0.0
    text_len: int = 0
    headings: list = field(default_factory=list)
    forms: int = 0
    tables: int = 0
    inputs: int = 0
    buttons: int = 0
    tabs: int = 0
    search: int = 0
    checks: list = field(default_factory=list)       # human-readable list of what was exercised
    findings: int = 0


@dataclass
class RoleOutcome:
    app: str
    role: str
    status: str                  # EXPLORED | LOGIN FAILED | BLOCKED | PARTIAL
    reason: str = ""
    pages: int = 0
    seconds: float = 0.0
    login_seconds: float = 0.0
    login_attempts: int = 0
    video: str = ""               # recording of this role's session (staging/local only)


@dataclass
class ExploreConfig:
    apps: tuple = ("hospital",)
    base_urls: dict = field(default_factory=dict)
    env: Environment | None = None
    roles: tuple = ()                    # empty = every role of the selected apps whose credentials are configured
    seed_paths: tuple = ()
    max_pages: int = 40
    max_minutes: float = 10.0            # per role
    headless: bool = True
    browser: str = "chromium"
    screenshots: str = "auto"            # auto | off | masked
    artifacts_dir: Path = Path("artifacts")
    run_id: str = ""
    anonymous_check: bool = True
    cross_role_check: bool = True
    login_retries: int = 2
    login_retry_wait_s: float = 4.0
    slow_s: float = 8.0
    video: bool = False                  # record a video of each role's session (never for production / unknown environments)
    fill: bool = False                   # STAGING ONLY: fill forms with synthetic AUTOTEST_ data and save them (see _fill_submit)
    confirm_phrase: str = ""
    allow_read_post: tuple = ()          # regexes of POST paths the OPERATOR declares read-only (list/search endpoints)
    login_fn: object = None              # tests: callable(page, role, base_url, login_urls) -> (LoginResult-like, attempts)
    roles_override: dict = field(default_factory=dict)   # tests: name -> config.Role-like


@dataclass
class ExploreResult:
    meta: dict
    findings: list
    pages: list
    roles: list
    validations: list
    dialogs: list
    blocked_writes: dict
    notes: list
    created: list = field(default_factory=list)


class _RoleRun:
    """Everything one (app, role) crawl needs."""

    def __init__(self, app, role, base, origin, ctx, page, guard, mon, deadline):
        self.app, self.role, self.base, self.origin = app, role, base, origin
        self.ctx, self.page, self.guard, self.mon, self.deadline = ctx, page, guard, mon, deadline
        self.seen: set = set()
        self.pattern_count: dict = {}
        self.queue: list = []
        self.menu_seen: set = set()
        self.visited: dict = {}          # normalized path -> headings (for the anonymous / cross-role checks)
        self.relogins = 0
        self.lost = False
        self.landing = ""
        self.login_urls: list = []
        self.menu_noted = False
        self.stats = {"forms": 0, "dialogs": 0, "tabs": 0, "searches": 0, "menu_clicks": 0}


# --------------------------------------------------------------------------------------------- the explorer
class Explorer:
    def __init__(self, cfg: ExploreConfig):
        self.cfg = cfg
        self.run_id = cfg.run_id or datetime.now().strftime("%Y%m%d%H%M%S")
        self.findings: dict = {}
        self.pages: list = []
        self.outcomes: list = []
        self.validations: list = []
        self.dialogs: list = []
        self.blocked_writes: dict = {}
        self.notes: list = []
        self.created: list = []
        self.sessions: dict = {}          # (app, role) -> in-memory storage_state (never written to disk)
        self.owner_pages: dict = {}       # (app, norm path) -> {role: headings}
        self.meta = {"run_id": self.run_id, "started": _now(), "mode": "autonomous read-only exploration",
                     "environment": cfg.env.name if cfg.env else "(none - read-only, unverified)",
                     "environment_kind": cfg.env.kind if cfg.env else "unverified", "apps": list(cfg.apps),
                     "browser": cfg.browser, "browser_version": "(not launched)"}
        kind = cfg.env.kind if cfg.env else None
        want = (cfg.screenshots or "auto").lower()
        self.shot_mode = (screenshot_mode(kind) if want == "auto" else "masked" if want == "masked" else "off")
        self.meta["screenshots"] = self.shot_mode
        self.video_on = bool(cfg.video) and self.shot_mode != "off"
        if cfg.video and not self.video_on:
            self.notes.append("Video NOT recorded: videos show everything on screen, so they are only allowed on declared staging/local environments (and with screenshots not switched off).")
        self.meta["fill"] = bool(cfg.fill)
        self.marker = f"AUTOTEST_{self.run_id}"
        if cfg.allow_read_post:
            self.meta["read_only_post_patterns_allowed_by_operator"] = list(cfg.allow_read_post)
        self._shots = 0
        self._idle_timeouts, self._skip_idle = 0, False
        self._loose_noted = False

    # ------------------------------------------------------------------ findings
    def add(self, rc, key, severity, category, title, detail, path, evidence=(), repro=(), shot=False, page=None, per_role=False):
        """Record a finding. The same problem seen by several roles is ONE finding listing every role (per_role=True keeps them apart)."""
        k = f"{rc.app}|{rc.role if per_role else '*'}|{key}"
        evidence = [redact(str(e))[:300] for e in evidence if e]
        f = self.findings.get(k)
        if f:
            f.occurrences += 1
            if rc.role not in f.role.split(", "):
                f.role += ", " + rc.role
            if path and path not in f.pages:
                f.pages.append(path)
            for e in evidence:
                if e not in f.evidence and len(f.evidence) < 6:
                    f.evidence.append(e)
            if SEV_RANK[severity] < SEV_RANK[f.severity]:
                f.severity = severity
            return f
        f = Finding(k, severity, category, redact(title), redact(detail), rc.app, rc.role, [path] if path else [], evidence[:6],
                    [redact(str(r)) for r in repro], occurrences=1)
        if shot and page is not None and self.shot_mode != "off" and self._shots < 20:
            self._shots += 1
            name = f"auto_{safe_name(self.run_id)}_{self._shots:02d}_{safe_name(category)}.png"
            p = safe_screenshot(page, Path(self.cfg.artifacts_dir) / "reports" / "screenshots" / name, self.shot_mode)
            f.screenshot = "screenshots/" + name if p else ""
        self.findings[k] = f
        return f

    def _repro(self, rc, path, *extra):
        return [f"Sign in as the '{rc.role}' test user ({rc.app})", f"Open {path}", *extra]

    # ------------------------------------------------------------------ entry point
    def run(self) -> ExploreResult:
        from playwright.sync_api import sync_playwright
        import os
        if self.cfg.fill:
            self._check_fill_allowed()
        plan = self._plan()
        self._say("roles that will run: " + (", ".join(f"{a}/{r}" for a, r in plan) if plan else "NONE (no test-account credentials found in .env)"))
        t_all = time.time()
        if not plan:
            self.notes.append("No role with credentials was found - nothing could be tested.")
        with sync_playwright() as p:
            launch = {"headless": self.cfg.headless}
            if self.cfg.browser == "chromium" and os.getenv("QA_CHROMIUM_PATH"):
                launch["executable_path"] = os.environ["QA_CHROMIUM_PATH"]
            browser = getattr(p, self.cfg.browser).launch(**launch)
            self.meta["browser_version"] = f"{self.cfg.browser} {browser.version}"
            try:
                for app in dict.fromkeys(a for a, _ in plan) or self.cfg.apps:
                    self._anonymous_login_page(browser, app)
                    for a, role in plan:
                        if a == app:
                            self._explore_role(browser, app, role)
                if self.cfg.anonymous_check:
                    for app in dict.fromkeys(a for a, _ in plan):
                        self._anonymous_access(browser, app)
                if self.cfg.cross_role_check:
                    for app in dict.fromkeys(a for a, _ in plan):
                        self._cross_role_access(browser, app)
            finally:
                browser.close()
        self.meta["finished"] = _now()
        self.meta["seconds"] = round(time.time() - t_all, 1)
        ordered = sorted(self.findings.values(), key=lambda f: (SEV_RANK[f.severity], f.category, f.title))
        for i, f in enumerate(ordered, 1):
            f.id = f"BUG-{i:03d}" if f.severity in ("High", "Medium") else f"OBS-{i:03d}"
        for rec in self.pages:
            rec.findings = sum(1 for f in ordered if rec.role in f.role.split(", ") and f.app == rec.app and rec.path in f.pages)
        return ExploreResult(self.meta, ordered, self.pages, self.outcomes, self.validations, self.dialogs, self.blocked_writes, self.notes, self.created)

    def _check_fill_allowed(self):
        """Fill mode creates records: every rule of the staging write gate must hold (raises SafetyBlocked otherwise)."""
        from .safety import check_write_allowed, SafetyBlocked
        for app in self.cfg.apps:
            check_write_allowed(self.cfg.env, app, "auto-fill", allow_write=True, confirm_phrase=self.cfg.confirm_phrase,
                                confirmed_workflows=("auto-fill",))

    # ------------------------------------------------------------------ planning: which roles can run
    def _plan(self):
        import config
        plan = []
        pool = dict(config.ROLES)
        pool.update(config.AGENT_ROLES)
        pool.update(self.cfg.roles_override)
        for name, role in pool.items():
            if role.app not in self.cfg.apps or (self.cfg.roles and name not in self.cfg.roles):
                continue
            if not role.email or not role.password:
                self.outcomes.append(RoleOutcome(role.app, name, "BLOCKED",
                                                 f"credentials not configured (set {role.email_env} and {role.password_env}); role skipped"))
                continue
            register_secret(role.password)
            plan.append((role.app, name))
        return plan

    def _role_obj(self, name):
        import config
        return self.cfg.roles_override.get(name) or config.ROLES.get(name) or config.AGENT_ROLES.get(name)

    def _base(self, app):
        import config
        b = (self.cfg.base_urls or {}).get(app)
        if not b and self.cfg.env:
            b = self.cfg.env.base_url(app)
        return (b or config.APPS[app].base).rstrip("/")

    def _new_ctx(self, browser, app, state=None, record=False):
        origin = normalize_origin(self._base(app))
        kw = dict(viewport={"width": 1440, "height": 900}, accept_downloads=False, service_workers="block")
        if state is not None:
            kw["storage_state"] = state
        if record and self.video_on:
            vdir = Path(self.cfg.artifacts_dir) / "videos" / self.run_id
            vdir.mkdir(parents=True, exist_ok=True)
            kw["record_video_dir"] = str(vdir)
            kw["record_video_size"] = {"width": 1280, "height": 720}
        ctx = browser.new_context(**kw)
        rx = re.compile("|".join(f"(?:{p})" for p in self.cfg.allow_read_post), re.I) if self.cfg.allow_read_post else None
        guard = ExplorerGuard(self.cfg.env, origin, writes_enabled=bool(self.cfg.fill), auth_any_origin=True, read_post_rx=rx, fill_mode=bool(self.cfg.fill))
        guard.attach(ctx)
        return ctx, guard, origin

    # ------------------------------------------------------------------ login
    def _login_urls(self, app):
        import config
        base = self._base(app)
        pref = config.APPS[app].login_path if app in config.APPS else "/login"
        out = []
        for p in (pref, *FALLBACK_LOGIN_PATHS):
            if p not in out:
                out.append(p)
        return [base + p for p in out]

    def _do_login(self, rc, role):
        """-> (ok, LoginResult-like, attempts). Retries only server-side failures (5xx / timeout), never a credential rejection."""
        if self.cfg.login_fn:
            return self.cfg.login_fn(rc.page, role, rc.base, rc.login_urls)
        from pages.login_page import LoginPage
        from utils.recorder import Blocked
        attempts, res = 0, None
        for url in rc.login_urls:
            lp = LoginPage(rc.page, url, role.landing_prefix)
            try:
                lp.open()
            except Exception:
                continue
            if not lp.form_visible():
                continue
            while True:
                attempts += 1
                try:
                    res = lp.submit(role.email, role.password)
                except Blocked:
                    break
                if res.ok:
                    return True, res, attempts
                transient = res.api_status >= 500 or (res.api_status == 0 and res.reason == "timeout")
                if not transient or attempts > self.cfg.login_retries:
                    return False, res, attempts
                time.sleep(self.cfg.login_retry_wait_s * attempts)
                try:
                    lp.open()
                except Exception:
                    pass
                if not lp.form_visible():
                    break
            break
        return False, res, attempts

    # ------------------------------------------------------------------ per role
    def _say(self, msg):
        """Live progress for the person watching the terminal (already redacted)."""
        print("  ... " + redact(str(msg)), flush=True)

    def _explore_role(self, browser, app, role_name):
        self._say(f"{app}/{role_name}: opening the login page and signing in")
        role = self._role_obj(role_name)
        base = self._base(app)
        t0 = time.time()
        ctx, guard, origin = self._new_ctx(browser, app, record=True)
        from utils.monitor import PageMonitor
        page = ctx.new_page()
        page.set_default_timeout(15000)
        mon = PageMonitor(page)
        rc = _RoleRun(app, role_name, base, origin, ctx, page, guard, mon, time.time() + self.cfg.max_minutes * 60)
        rc.login_urls = self._login_urls(app)
        rc.net_errors = []
        page.on("console", lambda m: rc.net_errors.append(m.text[:300]) if m.type == "error" and "CORS" in m.text else None)
        page.on("requestfailed", lambda r: rc.net_errors.append(f"{r.method} {norm_path(r.url)} failed: {r.failure}") if r.method != "GET" and len(rc.net_errors) < 6 else None)
        out = RoleOutcome(app, role_name, "EXPLORED")
        try:
            ok, res, attempts = self._do_login(rc, role)
            out.login_attempts = attempts
            out.login_seconds = getattr(res, "seconds", 0.0) or round(time.time() - t0, 1)
            if not ok:
                self._say(f"{app}/{role_name}: LOGIN FAILED ({getattr(res, 'reason', '') or 'no login form'}, API status {getattr(res, 'api_status', 0) or 'none'})")
                self._login_failed(rc, res, attempts, out)
                return
            self._say(f"{app}/{role_name}: logged in, exploring pages")
            if attempts > 1:
                self.add(rc, "login-intermittent", "Medium", "login", f"Login needed {attempts} attempts (intermittent server error)",
                         f"The first login attempt(s) for role '{role_name}' failed with a server error (HTTP {getattr(res, 'api_status', '5xx')}) "
                         "and a later attempt succeeded. Users can hit the same error.", "/login",
                         [f"{attempts} attempts, last result ok"], [f"Open the login page", f"Sign in as '{role_name}' repeatedly"], per_role=True)
            if out.login_seconds > 6:
                self.add(rc, "login-slow", "Low", "performance", f"Login took {out.login_seconds:.1f}s",
                         "Signing in is slow (over 6 seconds from click to landing page).", "/login", [f"{out.login_seconds:.1f}s"],
                         [f"Open the login page", f"Sign in as '{role_name}'"])
            try:
                self.sessions[(app, role_name)] = ctx.storage_state()
            except Exception:
                pass
            guard.block_all = not self.cfg.fill          # read-only: from here on NOTHING that writes can leave the browser (fill mode: staging gate passed)
            mon.clear()
            rc.landing = page.url
            self._crawl(rc, role, out)
        except Exception as e:
            out.status, out.reason = "PARTIAL", redact(f"explorer error: {type(e).__name__}: {(str(e).splitlines() or [''])[0][:200]}")
        finally:
            self._collect_blocked(guard)
            out.seconds = round(time.time() - t0, 1)
            out.pages = sum(1 for r in self.pages if r.app == app and r.role == role_name)
            self.outcomes.append(out)
            vid = None
            try:
                vid = page.video if self.video_on else None
            except Exception:
                vid = None
            try:
                ctx.close()
            except Exception:
                pass
            if vid is not None:
                try:
                    src = Path(vid.path())
                    dst = src.with_name(f"{safe_name(app)}_{safe_name(role_name)}_{self.run_id}.webm")
                    src.replace(dst)
                    out.video = str(dst)
                    self._say(f"{app}/{role_name}: video saved")
                except Exception as e:
                    self.notes.append(f"{role_name}: video could not be saved ({type(e).__name__})")

    def _collect_blocked(self, guard):
        for line in guard.blocked:
            m = re.match(r"(\w+) (\S+) ->", line)
            if m and "/cdn-cgi/" not in m.group(2):          # Cloudflare's own analytics beacon, not the app
                key = f"{m.group(1)} {norm_path(m.group(2))}"
                self.blocked_writes[key] = self.blocked_writes.get(key, 0) + 1

    def _login_failed(self, rc, res, attempts, out):
        status = getattr(res, "api_status", 0) or 0
        fb = redact(" | ".join(getattr(res, "feedback", None) or []) or getattr(res, "error", "") or "")
        reason = getattr(res, "reason", "") or "no login form found"
        if res is None:
            title, sev = f"No login form found for the {rc.app} app", "High"
            detail = "None of the tried addresses (" + ", ".join(norm_path(u) for u in rc.login_urls) + ") showed a password field."
            out.status, out.reason = "LOGIN FAILED", "no login form found"
        elif status >= 500:
            title, sev = f"Login API returns HTTP {status} for role '{rc.role}'", "High"
            detail = (f"Signing in as '{rc.role}' failed with a server error after {attempts} attempt(s) (spaced out). The credentials were accepted "
                      "by the form but the backend crashed - this is a server-side bug or outage, not a wrong password. "
                      "Check the backend logs for the login endpoint at the time of the run.")
            out.status, out.reason = "LOGIN FAILED", f"HTTP {status} from the login API after {attempts} attempt(s)"
        elif status in (401, 403, 422, 400):
            title, sev = f"Login rejected for role '{rc.role}' (HTTP {status})", "High"
            detail = ("The server rejected the configured test credentials. Either the password in your .env / GitHub secret is out of date, "
                      "the account is locked/disabled, or the login endpoint changed. Not retried, to avoid locking the account.")
            out.status, out.reason = "LOGIN FAILED", f"HTTP {status}: credentials rejected"
        elif status == 429:
            title, sev = f"Login rate-limited for role '{rc.role}' (HTTP 429)", "Medium"
            detail = "The server throttled the login. Wait a few minutes and run again; the account was not retried."
            out.status, out.reason = "BLOCKED", "HTTP 429 rate limit"
        else:
            title, sev = f"Login did not complete for role '{rc.role}' ({reason})", "High"
            detail = f"After submitting valid-looking credentials the app did not reach a logged-in page (reason: {reason})."
            blocked = [b for b in rc.guard.blocked[-3:]]
            net = getattr(rc, "net_errors", [])
            if status == 0 and any("CORS" in e for e in net):
                title = f"Login is blocked by the browser's CORS policy for role '{rc.role}'"
                detail = ("The login request was sent but the browser refused the server's reply because the backend does not allow this website's address "
                          "(CORS). Users see a generic 'couldn't reach' message even when the server answered. Fix on the backend: add this site's origin "
                          "to its allowed CORS origins. Browser said: " + redact(next(e for e in net if "CORS" in e))[:260])
                sev = "High"
            elif status == 0 and blocked:
                detail += (" The agent's own safety filter blocked these requests during login, which makes the app say it cannot reach the server: "
                           + "; ".join(blocked) + ". Add that API host to allowed_origins and auth_origins in config/environments.yml.")
            elif status == 0 and fb and re.search(r"reach|network|internet|connection|fetch", fb, re.I):
                detail += (" The page reports a network failure and no login response was seen: the browser could not reach the backend the frontend is "
                           "configured to call (offline, wrong host baked into the frontend build, or CORS).")
            out.status, out.reason = "LOGIN FAILED", ("blocked by CORS policy (backend does not allow this site's origin)" if status == 0 and any("CORS" in e for e in net)
                                                      else reason + (f" (blocked by agent: {blocked[-1]})" if status == 0 and blocked else ""))
        self.add(rc, "login-failed", sev, "login", title, detail, "/login",
                 [f"login API status: {status or 'none seen'}", f"reason: {reason}", f"visible message: {fb}" if fb else "", f"attempts: {attempts}"],
                 [f"Open {norm_path(rc.login_urls[0])}", f"Enter the '{rc.role}' test user's email and password", "Click Log in"],
                 shot=False, per_role=True)

    # ------------------------------------------------------------------ crawl
    def _crawl(self, rc, role, out):
        page = rc.page
        first = rc.landing
        rc.queue.append(("url", first, "landing page after login"))
        for sp in self.cfg.seed_paths:
            rc.queue.append(("url", rc.origin + (sp if sp.startswith("/") else "/" + sp), f"seed {sp}"))
        n = 0
        while rc.queue and n < self.cfg.max_pages and time.time() < rc.deadline and not rc.lost:
            kind, *rest = rc.queue.pop(0)
            if kind == "url":
                url, via = rest
                if norm_path(url) in rc.seen:
                    continue
            else:
                text, start = rest
                if text in rc.menu_seen:
                    continue
                url, via = start, f"menu item '{text}'"
            try:
                rec = self._visit(rc, kind, rest)
            except Exception as e:
                self.notes.append(redact(f"{rc.role}: skipped {norm_path(url)} ({type(e).__name__})"))
                continue
            if rec is not None:
                n += 1
        if rc.queue and n >= self.cfg.max_pages:
            out.status, out.reason = "PARTIAL", f"stopped at the --max-pages limit ({self.cfg.max_pages}); {len(rc.queue)} more page(s) were queued"
        elif time.time() >= rc.deadline:
            out.status, out.reason = "PARTIAL", f"stopped at the {self.cfg.max_minutes:g} minute budget"
        elif rc.lost:
            out.status, out.reason = "PARTIAL", "session was lost and could not be restored"

    def _goto(self, rc, url):
        page = rc.page
        t0 = time.time()
        status, err = None, ""
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            status = resp.status if resp else None
        except Exception as e:
            err = (str(e).splitlines() or [""])[0][:200]
        self._settle(page)
        return status, round(time.time() - t0, 2), err

    def _settle(self, page):
        """Wait until the page is usable. Apps that poll forever never reach 'networkidle'; after 3 such timeouts stop waiting for it."""
        if not self._skip_idle:
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
                self._idle_timeouts = 0
            except Exception:
                self._idle_timeouts += 1
                if self._idle_timeouts >= 3:
                    self._skip_idle = True
                    self.notes.append("The app never became network-idle (it polls); the explorer switched to a fixed short wait per page.")
        try:
            page.wait_for_function("() => document.body && document.body.innerText.trim().length >= 20", timeout=4000)
        except Exception:
            pass
        page.wait_for_timeout(900 if self._skip_idle else 250)

    def _logged_out(self, rc) -> bool:
        try:
            return bool(LOGIN_URL_RX.search(rc.page.url)) or rc.page.locator('input[type="password"]').first.is_visible()
        except Exception:
            return False

    def _relogin(self, rc) -> bool:
        if rc.relogins >= 2:
            rc.lost = True
            return False
        rc.relogins += 1
        role = self._role_obj(rc.role)
        rc.guard.block_all = False
        try:
            ok, res, attempts = self._do_login(rc, role)
        finally:
            rc.guard.block_all = not self.cfg.fill
        if not ok:
            rc.lost = True
        return ok

    def _visit(self, rc, kind, rest):
        page = rc.page
        target = rest[0] if kind == "menu" else rest
        self._say(f"{rc.role}: visiting {norm_path(target) if isinstance(target, str) and target.startswith('http') else target}"[:120])
        if kind == "menu":
            text, start = rest
            rc.menu_seen.add(text)
            status, load_s, err = self._goto(rc, start)
            items = self._menu(page)
            item = next((i for i in items if i["text"] == text), None)
            if item is None:
                return None
            rc.mon.clear()
            b0 = len(rc.guard.blocked)
            t0 = time.time()
            try:
                page.locator(f'[data-qa-menu="{item["idx"]}"]').first.click(timeout=4000)
            except Exception:
                return None
            rc.stats["menu_clicks"] += 1
            self._settle(page)
            load_s, via, status = round(time.time() - t0, 2), f"menu item '{text}'", None
            url = page.url
            key = norm_path(url)
            if key in rc.seen:
                rc.mon.clear()
                return None
        else:
            url, via = rest
            key = norm_path(url)
            rc.mon.clear()
            b0 = len(rc.guard.blocked)
            status, load_s, err = self._goto(rc, url)
            if err and status is None:
                self.add(rc, f"nav-error:{key}", "High", "navigation", f"Page failed to load: {norm_path(url)}", err, key,
                         [err], self._repro(rc, key), shot=False)
                rc.seen.add(key)
                return None
            if self._logged_out(rc) and not LOGIN_URL_RX.search(url):
                if self._relogin(rc):
                    self.add(rc, "session-lost", "Medium", "session", "Session dropped during exploration (re-login was needed)",
                             "While browsing, the app sent the user back to the login page although the session had just been created. "
                             "Users could be logged out unexpectedly.", key, [f"redirected to {norm_path(page.url)}"], self._repro(rc, key))
                    rc.mon.clear()
                    status, load_s, err = self._goto(rc, url)
                else:
                    return None
        final = page.url
        key = norm_path(final)
        if key in rc.seen:
            return None
        rc.seen.add(key)
        facts = page.evaluate(PAGE_FACTS_JS)
        rec = PageRecord(rc.app, rc.role, key, via, redact(facts["title"])[:80], status, load_s, facts["textLen"],
                         [redact(h)[:60] for h in facts["headings"][:6]], facts["forms"], facts["tables"], facts["inputs"], facts["buttons"],
                         len(facts["tabs"]), facts["searchBoxes"])
        self.pages.append(rec)
        rc.visited[key] = facts["headings"]
        self.owner_pages.setdefault((rc.app, key), {})[rc.role] = facts["headings"]
        self._check_page(rc, rec, facts, status, load_s, via, b0)
        self._enqueue(rc, facts)
        if time.time() < rc.deadline:
            self._interact(rc, rec, final, facts)
        return rec

    def _menu(self, page):
        """Menu items; if the strict sidebar detection finds fewer than 3, retry with the loose rule (anything clickable in the left 40%)."""
        try:
            items = page.evaluate(MENU_JS, False)
            if len(items) < 3:
                items = page.evaluate(MENU_JS, True)
                if items and not self._loose_noted:
                    self._loose_noted = True
                    self.notes.append("Sidebar not recognised by the strict rule; the loose menu rule (clickable items in the left 40% of the page) was used.")
            return items
        except Exception:
            return []

    def _enqueue(self, rc, facts):
        menu = self._menu(rc.page)
        if not menu and not rc.menu_noted:
            rc.menu_noted = True
            self.notes.append(f"{rc.role}: NO menu/navigation items were found on {norm_path(rc.page.url)}; only that page and its plain links could be explored. "
                              f"Page offers {facts['buttons']} button(s), {len(facts['links'])} link(s). Tell the developer the sidebar markup so detection can be extended.")
        for it in menu:
            if DANGEROUS_RX.search(it["text"]):
                continue
            if it["href"]:
                self._enqueue_url(rc, it["href"], f"menu link '{it['text']}'")
            elif it["text"] not in rc.menu_seen:
                rc.queue.append(("menu", it["text"], rc.page.url))
        for l in facts["links"]:
            if not DANGEROUS_RX.search(l["text"] or ""):
                self._enqueue_url(rc, l["href"], f"link '{l['text']}'" if l["text"] else "link")

    def _enqueue_url(self, rc, href, via):
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "blob:")):
            return
        try:
            if normalize_origin(href) != rc.origin:
                return
        except Exception:
            return
        u = urlsplit(href)
        if u.path.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".svg", ".zip", ".csv", ".xlsx", ".doc", ".docx", ".mp4", ".ico")):
            return
        if DANGEROUS_RX.search(u.path):
            return
        k = norm_path(href)
        if k in rc.seen or any(q[0] == "url" and norm_path(q[1]) == k for q in rc.queue):
            return
        rc.pattern_count[k] = rc.pattern_count.get(k, 0) + 1
        if ":id" in k and sum(1 for s in rc.seen if s == k) >= 1:
            return
        rc.queue.append(("url", href, via))

    # ------------------------------------------------------------------ generic page checks
    def _check_page(self, rc, rec, facts, status, load_s, via, b0):
        page, path = rc.page, rec.path
        blocked_now = len(rc.guard.blocked) - b0
        repro = self._repro(rc, path)
        shot = dict(shot=True, page=page)
        if status and status >= 500:
            self.add(rc, f"http:{path}:{status}", "High", "http-error", f"Page returns HTTP {status}: {path}",
                     f"Opening {path} returned a server error.", path, [f"HTTP {status}"], repro, **shot)
        elif status and status >= 400:
            self.add(rc, f"http:{path}:{status}", "Medium", "http-error", f"Page returns HTTP {status}: {path}",
                     f"Opening {path} returned HTTP {status}" + (f" (reached from {via})" if via else "") + ".", path, [f"HTTP {status}"], repro, **shot)
        if facts["textLen"] < 20:
            self.add(rc, f"blank:{path}", "High", "blank-page", f"Blank page: {path}",
                     "The page rendered with (almost) no visible content after the app had finished loading.", path,
                     [f"{facts['textLen']} visible characters"], repro, **shot)
        try:
            body = page.inner_text("body", timeout=3000)[:60000]
        except Exception:
            body = ""
        phrases = list({m.group(0).lower(): m.group(0) for m in ERROR_TEXT_RX.finditer(body)}.values())
        if phrases:
            sev = "Info" if blocked_now else "Medium"
            note = (f" ({blocked_now} write request(s) were blocked by the read-only guard while this page loaded, so this may be a side effect of testing)"
                    if blocked_now else "")
            self.add(rc, f"errtext:{path}", sev, "error-text", f"Error text visible on {path}: {', '.join(phrases[:3])}",
                     "The page shows text that looks like an unhandled error or unrendered value" + note + ".", path,
                     [f"matched: {', '.join(phrases[:5])}"], repro, **shot)
        if load_s > self.cfg.slow_s:
            self.add(rc, f"slow:{path}", "Low", "performance", f"Slow page: {path} ({load_s:.1f}s)",
                     f"The page needed {load_s:.1f}s until it was usable (threshold {self.cfg.slow_s:g}s).", path, [f"{load_s:.1f}s"], repro)
        if facts["brokenImages"]:
            self.add(rc, f"img:{path}", "Low", "broken-image", f"{facts['brokenImages']} broken image(s) on {path}",
                     "Image(s) failed to load (the browser shows a broken-image icon).", path, [f"{facts['brokenImages']} image(s)"], repro, **shot)
        if facts["overflowX"] > 8:
            self.add(rc, f"overflow:{path}", "Low", "layout", f"Horizontal scrollbar on {path}",
                     f"The page is {facts['overflowX']}px wider than the 1440px viewport.", path, [f"{facts['overflowX']}px"], repro, **shot)
        if facts["unlabeledInputs"]:
            self.add(rc, f"nolabel:{path}", "Low", "accessibility", f"{facts['unlabeledInputs']} form field(s) without a label on {path}",
                     "Visible inputs have no label, placeholder or aria-label (screen readers cannot name them).", path,
                     [f"{facts['unlabeledInputs']} field(s)"], repro)
        if facts["unnamedButtons"]:
            self.add(rc, "nobtn", "Info", "accessibility", "Icon button(s) without an accessible name",
                     "Buttons have neither text nor aria-label, so screen readers cannot announce them. It is usually ONE shared control "
                     "(for example a bell, menu or chat icon) repeated on every page - fix it once. Pages affected are listed under 'Where'.",
                     path, [f"{path}: {facts['unnamedButtons']} button(s)"], repro)
        if not (facts["title"] or "").strip():
            self.add(rc, f"notitle:{path}", "Info", "seo", f"Page has no <title>: {path}", "document.title is empty.", path, [], repro)
        rec.checks.append("loaded, content, error text, console, network, layout")
        self._harvest(rc, path, repro, page)

    def _harvest(self, rc, path, repro, page=None, ctx_label=""):
        """Turn the console/network noise collected since the last clear() into (de-duplicated) findings."""
        label = f" ({ctx_label})" if ctx_label else ""
        for s in list(rc.mon.failed):
            m = re.match(r"(\d{3}) (\w+) (\S+)", s)
            if m:
                code, method, url = int(m.group(1)), m.group(2), m.group(3)
                api = norm_path(url)
                if code >= 500:
                    sev, why = "High", "server error"
                elif code == 429:
                    sev, why = "Low", "rate limited"
                elif code in (401, 403):
                    sev, why = "Medium", "access denied for a logged-in user"
                else:
                    sev, why = "Medium", "client error"
                host = urlsplit(url).netloc
                self.add(rc, f"net:{method}:{api}:{code}", sev, "api-error", f"{method} {api} returned HTTP {code}",
                         f"A request made by the page failed ({why}){label}. Host: {host}.", path, [f"{code} {method} {host}{api}"],
                         repro + ([ctx_label] if ctx_label else []), page=page, shot=bool(page) and code >= 500)
            else:
                m2 = re.match(r"FAILED (\w+) (\S+) \((.*)\)", s)
                if m2:
                    method, url, reason = m2.groups()
                    api = norm_path(url)
                    self.add(rc, f"netfail:{method}:{api}", "Medium", "network-failure", f"{method} {api} failed ({reason})",
                             f"A request could not be completed{label}.", path, [f"{method} {urlsplit(url).netloc}{api}: {reason}"], repro)
        for c in list(rc.mon.console):
            if "Failed to load resource" in c:
                continue                                  # already reported through the network list
            if c.startswith("[pageerror]"):
                msg = c[len("[pageerror]"):].strip()[:160]
                self.add(rc, f"jserr:{msg[:80]}", "Medium", "javascript-error", f"Uncaught JavaScript error: {msg[:70]}",
                         f"The page threw an uncaught exception{label}. Users may see a broken or frozen screen.", path, [msg],
                         repro + ([ctx_label] if ctx_label else []), page=page, shot=bool(page))
            else:
                msg = re.sub(r"^\[console\.error\]\s*", "", c)[:160]
                self.add(rc, f"console:{msg[:80]}", "Low", "console-error", f"Console error: {msg[:70]}",
                         f"The browser console logged an error{label}.", path, [msg], repro)
        rc.mon.clear()

    # ------------------------------------------------------------------ interactions
    def _interact(self, rc, rec, url, facts):
        steps = (self._do_validation_page, self._do_dialogs, self._do_tabs, self._do_search)
        for fn in steps:
            if time.time() >= rc.deadline or rc.lost:
                break
            try:
                fn(rc, rec, url, facts)
            except Exception as e:
                self.notes.append(redact(f"{rc.role} {rec.path}: {fn.__name__} skipped ({type(e).__name__}: {(str(e).splitlines() or [''])[0][:100]})"))
                self._reset(rc, url)

    def _reset(self, rc, url):
        rc.mon.clear()
        self._goto(rc, url)
        rc.mon.clear()

    def _feedback(self, page):
        try:
            return page.evaluate(FEEDBACK_JS, FEEDBACK_SEL)
        except Exception:
            return {"texts": [], "invalid": 0}

    def _find_submit(self, page, scope):
        """-> (locator, label) of the primary submit-like button inside `scope`, or (None, '')."""
        try:
            cands = scope.evaluate(SUBMIT_JS)
        except Exception:
            return None, ""
        best = None
        for c in cands:
            label = c["label"]
            if not SUBMIT_RX.search(label) or DANGEROUS_RX.search(label):
                continue
            if re.search(r"save|submit|register|create|update|book|confirm", label, re.I):
                return page.locator(f'[data-qa-btn="{c["idx"]}"]').first, label
            best = best or c
        if best:
            return page.locator(f'[data-qa-btn="{best["idx"]}"]').first, best["label"]
        return None, ""

    def _empty_submit(self, rc, rec, url, scope, where, repro_prefix):
        """Press the primary submit button with nothing filled (all writes blocked). Records what validation feedback the user sees."""
        page = rc.page
        try:
            facts = scope.evaluate(FORM_FACTS_JS)
        except Exception:
            return None
        if not facts["required"] or not facts["empty"]:
            return None
        btn, label = self._find_submit(page, scope)
        if btn is None:
            return None
        page.evaluate(OBSERVER_JS)
        before = self._feedback(page)
        rc.mon.clear()
        b0, url0 = len(rc.guard.blocked), page.url
        btn.click(timeout=4000)
        end, new, after = time.time() + 2.8, [], before
        while time.time() < end:
            page.wait_for_timeout(250)
            after = self._feedback(page)
            seen = []
            try:
                seen = page.evaluate("() => window.__qaSeen || []")
            except Exception:
                pass
            new = [t for t in dict.fromkeys(list(after["texts"]) + [s for s in seen if len(s) < 140])
                   if t not in before["texts"] and not NOISE_RX.match(t) and label not in t]
            if new or after["invalid"] > before["invalid"] or len(rc.guard.blocked) > b0:
                page.wait_for_timeout(350)
                break
        after = self._feedback(page)
        seen = []
        try:
            seen = page.evaluate("() => window.__qaSeen || []")
        except Exception:
            pass
        new = [t for t in dict.fromkeys(list(after["texts"]) + [s for s in seen if len(s) < 140])
               if t not in before["texts"] and not NOISE_RX.match(t) and label not in t][:5]
        invalid = max(0, after["invalid"] - before["invalid"])
        blocked_new = rc.guard.blocked[b0:]
        navigated = norm_path(page.url) != norm_path(url0)
        rc.stats["forms"] += 1
        res = {"app": rc.app, "role": rc.role, "path": rec.path, "where": where, "button": redact(label),
               "messages": [redact(m)[:140] for m in new], "invalid_fields": invalid, "write_attempts_blocked": len(blocked_new),
               "navigated": navigated}
        self.validations.append(res)
        repro = self._repro(rc, rec.path, *repro_prefix, f"Leave the required fields empty and click '{label}'")
        if navigated:
            self.add(rc, f"empty-advance:{rec.path}:{where}", "Medium", "validation",
                     f"Form lets the user continue with empty required fields: {rec.path}",
                     f"Clicking '{label}' with every required field empty moved the user to {norm_path(page.url)} instead of showing a validation message.",
                     rec.path, [f"now at {norm_path(page.url)}"], repro, shot=True, page=page)
        elif not new and not invalid:
            if blocked_new:
                self.add(rc, f"empty-sent:{rec.path}:{where}", "Low", "validation",
                         f"Empty form is sent to the server without any visible validation: {rec.path}",
                         f"Clicking '{label}' with required fields empty made the app send a request ({len(blocked_new)} blocked by the read-only guard) and "
                         "no validation message was shown. The server may still reject it - confirm manually that the user sees a clear message.",
                         rec.path, [b.split(" -> ")[0] for b in blocked_new[:3]], repro, shot=True, page=page)
            else:
                self.add(rc, f"empty-silent:{rec.path}:{where}", "Low", "validation",
                         f"No feedback when submitting an empty required form: {rec.path}",
                         f"Clicking '{label}' with required fields empty showed no message, marked no field invalid and sent nothing.",
                         rec.path, [], repro, shot=True, page=page)
        self._harvest(rc, rec.path, repro, page, f"after clicking '{label}' on an empty form")
        rec.checks.append(f"empty submit ({where}): " + (f"message '{res['messages'][0]}'" if res["messages"] else f"{invalid} invalid field(s)" if invalid else "no feedback"))
        return res

    def _do_validation_page(self, rc, rec, url, facts):
        if not facts["inputs"]:
            return
        res = self._empty_submit(rc, rec, url, rc.page.locator("body"), "page form", [])
        if res is not None:
            self._reset(rc, url)
        if self.cfg.fill and time.time() < rc.deadline:
            if self._fill_submit(rc, rec, url, rc.page.locator("body"), "page form", []) is not None:
                self._reset(rc, url)

    def _do_dialogs(self, rc, rec, url, facts):
        page = rc.page
        try:
            names = page.evaluate(OPEN_BUTTONS_JS, OPEN_RX)
        except Exception:
            return
        for name in names[:5]:
            if time.time() >= rc.deadline or DANGEROUS_RX.search(name):
                break
            rc.mon.clear()
            before = page.evaluate("() => ({n: document.getElementsByTagName('*').length, t: (document.body.innerText||'').length})")
            b0, url0 = len(rc.guard.blocked), page.url
            try:
                page.get_by_text(name, exact=True).first.click(timeout=3000)
            except Exception:
                continue
            repro = [f"Click '{name}'"]
            dlg = page.locator(DIALOG_SEL + " >> visible=true")
            opened = False
            try:
                dlg.first.wait_for(state="visible", timeout=2500)
                opened = True
            except Exception:
                pass
            if opened:
                rc.stats["dialogs"] += 1
                info = page.evaluate("(sel) => { const d = [...document.querySelectorAll(sel)].find(e => e.offsetWidth || e.offsetHeight); "
                                     "if (!d) return {}; const h = d.querySelector('h1,h2,h3,h4,h5,h6,[class*=title i]'); "
                                     "return {title: h ? h.innerText.trim().slice(0,60) : '', inputs: d.querySelectorAll('input,select,textarea').length}; }", DIALOG_SEL)
                self.dialogs.append({"app": rc.app, "role": rc.role, "path": rec.path, "button": redact(name),
                                     "title": redact(info.get("title", "")), "inputs": info.get("inputs", 0)})
                res = self._empty_submit(rc, rec, url, dlg.first, f"dialog '{name}'", repro)
                if self.cfg.fill:
                    self._fill_submit(rc, rec, url, dlg.first, f"dialog '{name}'", repro)
                rec.checks.append(f"opened dialog '{name}'")
                self._harvest(rc, rec.path, self._repro(rc, rec.path, *repro), page, f"while the '{name}' dialog was open")
            elif norm_path(page.url) != norm_path(url0):
                self._enqueue_url(rc, page.url, f"button '{name}'")
                rec.checks.append(f"button '{name}' navigates to {norm_path(page.url)}")
                self._harvest(rc, rec.path, self._repro(rc, rec.path, *repro), page, f"after clicking '{name}'")
            else:
                page.wait_for_timeout(600)
                after = page.evaluate("() => ({n: document.getElementsByTagName('*').length, t: (document.body.innerText||'').length})")
                touched = len(rc.guard.blocked) > b0 or bool(rc.mon.failed) or bool(rc.mon.console)
                if after == before and not touched:
                    self.add(rc, f"noeffect:{rec.path}:{name}", "Low", "dead-control", f"Button '{name}' has no visible effect on {rec.path}",
                             f"Clicking '{name}' changed nothing: no dialog, no navigation, no DOM change and no network request.",
                             rec.path, [], self._repro(rc, rec.path, *repro), shot=True, page=page)
                else:
                    rec.checks.append(f"clicked '{name}' (in-page change)")
                self._harvest(rc, rec.path, self._repro(rc, rec.path, *repro), page, f"after clicking '{name}'")
            self._reset(rc, url)

    def _do_tabs(self, rc, rec, url, facts):
        page = rc.page
        labels = [t for t in facts["tabs"] if t and not DANGEROUS_RX.search(t)][:8]
        if len(labels) < 2:
            return
        clicked = 0
        for t in labels:
            rc.mon.clear()
            try:
                tab = page.get_by_role("tab", name=t, exact=True).first
                if tab.get_attribute("aria-selected") == "true":
                    continue
                tab.click(timeout=3000)
            except Exception:
                continue
            self._settle(page)
            clicked += 1
            rc.stats["tabs"] += 1
            if self._logged_out(rc):
                self.add(rc, f"tab-logout:{rec.path}:{t}", "High", "session", f"Opening tab '{t}' logs the user out ({rec.path})",
                         "After clicking the tab the app showed the login page.", rec.path, [], self._repro(rc, rec.path, f"Click the '{t}' tab"), shot=True, page=page)
                self._relogin(rc)
                break
            self._harvest(rc, rec.path, self._repro(rc, rec.path, f"Click the '{t}' tab"), page, f"after opening tab '{t}'")
        if clicked:
            rec.checks.append(f"{clicked} tab(s) opened")
            self._reset(rc, url)

    def _do_search(self, rc, rec, url, facts):
        if not facts["searchBoxes"]:
            return
        page = rc.page
        box = page.locator('input[type=search], input[placeholder*="search" i], input[name*="search" i]').first
        if not box.is_visible():
            return
        rc.mon.clear()
        term = "zzqq_no_such_record_9"
        box.fill(term)
        box.press("Enter")
        self._settle(page)
        rc.stats["searches"] += 1
        repro = self._repro(rc, rec.path, f"Type '{term}' in the search box and press Enter")
        if self._logged_out(rc):
            self.add(rc, f"search-logout:{rec.path}", "High", "session", f"Searching logs the user out ({rec.path})", "The login page appeared after a search.",
                     rec.path, [], repro, shot=True, page=page)
            self._relogin(rc)
        else:
            try:
                usable = len(page.inner_text("body", timeout=3000).strip()) >= 20
            except Exception:
                usable = False
            if not usable:
                self.add(rc, f"search-blank:{rec.path}", "High", "blank-page", f"Page goes blank after an unmatched search ({rec.path})",
                         "Searching for a term that matches nothing leaves the page empty instead of showing a 'no results' state.", rec.path, [], repro,
                         shot=True, page=page)
        self._harvest(rc, rec.path, repro, page, "after an unmatched search")
        rec.checks.append("search with unmatched term")
        self._reset(rc, url)


    # ------------------------------------------------------------------ fill mode (staging only)
    PAYMENT_PAGE_RX = re.compile(r"pay(?:ment)?|billing|invoice|checkout|refund|wallet|razorpay", re.I)
    SUCCESS_RX = re.compile(r"success|created|saved|registered|added|booked|submitted|updated|completed|thank", re.I)
    ERROR_RX = re.compile(r"error|fail|invalid|already|exist|must|required|please (?:enter|select|fill)|cannot|unable|not (?:valid|allowed)", re.I)

    def _synth(self, kind, hint, tag):
        """Obviously-fake value for a field, chosen from its type and label/name/placeholder."""
        h = hint.lower()
        digits = "".join(ch for ch in self.run_id if ch.isdigit()).ljust(9, "7")[:9]
        if kind == "email" or "email" in h:
            return f"autotest.{self.run_id.lower()}@example.com"
        if kind == "tel" or any(k in h for k in ("phone", "mobile", "contact", "whatsapp")):
            return "9" + digits
        if kind == "number" or any(k in h for k in ("age", "amount", "quantity", "qty", "fee", "price", "rate", "count", "weight", "height")):
            return "30" if "age" in h else "10"
        if kind == "date":
            return "1990-01-01" if any(k in h for k in ("birth", "dob")) else (datetime.now(timezone.utc).date().isoformat())
        if kind == "time":
            return "10:00"
        if "last" in h and "name" in h:
            return "Tester"
        if "pin" in h or "zip" in h:
            return "110001"
        if "address" in h or "city" in h:
            return "Test Street"
        return self.marker

    def _fill_fields(self, page, scope) -> int:
        filled = 0
        fields = scope.locator("input:visible, textarea:visible, select:visible")
        for i in range(min(fields.count(), 40)):
            el = fields.nth(i)
            try:
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                kind = (el.get_attribute("type") or tag).lower()
                if kind in ("hidden", "password", "file", "submit", "button", "reset", "image", "range", "color"):
                    continue
                if el.evaluate("e => e.closest('nav,aside,header,[role=navigation],[class*=sidebar i]') !== null"):
                    continue
                hint = " ".join(filter(None, [el.get_attribute("name"), el.get_attribute("id"), el.get_attribute("placeholder"),
                                              el.get_attribute("aria-label"), el.evaluate("e => (e.labels && e.labels[0] ? e.labels[0].innerText : '')")]))
                if kind in ("checkbox", "radio"):
                    continue
                if tag == "select":
                    if el.locator("option").count() > 1 and not el.input_value():
                        el.select_option(index=1)
                        filled += 1
                    continue
                if el.get_attribute("role") == "combobox" or el.get_attribute("aria-haspopup") in ("listbox", "true") or el.get_attribute("readonly") is not None:
                    el.click(timeout=2000)
                    opt = page.locator('[role="option"]:visible, li[role="option"]:visible').first
                    opt.wait_for(state="visible", timeout=2000)
                    opt.click(timeout=2000)
                    filled += 1
                    continue
                if (el.input_value() or "").strip():
                    continue
                el.fill(self._synth(kind, hint, tag), timeout=3000)
                filled += 1
            except Exception:
                continue
        return filled

    def _fill_submit(self, rc, rec, url, scope, where, repro_prefix):
        """STAGING ONLY. Fill the visible form with synthetic AUTOTEST_ data, save it, and record what the user is shown.
        Never on payment/billing pages, never with password fields, never when the submit label looks destructive."""
        page = rc.page
        if self.PAYMENT_PAGE_RX.search(rec.path):
            self.notes.append(f"fill skipped on {rec.path}: payment/billing pages are never automated")
            return None
        try:
            facts = scope.evaluate(FORM_FACTS_JS)
            if not facts["inputs"] or scope.locator('input[type="password"]:visible').count():
                return None
        except Exception:
            return None
        btn, label = self._find_submit(page, scope)
        if btn is None:
            return None
        n = self._fill_fields(page, scope)
        if not n:
            return None
        # the button list is marked per call: look it up again after filling (a dialog may have re-rendered)
        btn, label = self._find_submit(page, scope)
        if btn is None:
            return None
        page.evaluate(OBSERVER_JS)
        before = self._feedback(page)
        rc.mon.clear()
        w0, url0 = len(rc.guard.sent_writes), page.url
        btn.click(timeout=4000)
        end, new = time.time() + 5.0, []
        while time.time() < end:
            page.wait_for_timeout(300)
            after = self._feedback(page)
            try:
                seen = page.evaluate("() => window.__qaSeen || []")
            except Exception:
                seen = []
            new = [t for t in dict.fromkeys(list(after["texts"]) + [x for x in seen if len(x) < 140])
                   if t not in before["texts"] and not NOISE_RX.match(t) and label not in t]
            if new or norm_path(page.url) != norm_path(url0):
                page.wait_for_timeout(600)
                break
        self._settle(page)
        sent = rc.guard.sent_writes[w0:]
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:
            body = ""
        navigated = norm_path(page.url) != norm_path(url0)
        marker_seen = self.marker in body
        msg = [redact(m)[:140] for m in new[:4]]
        if any(self.SUCCESS_RX.search(m) for m in msg) or marker_seen or (navigated and sent):
            outcome = "saved (confirmation shown)" if msg or marker_seen else "saved (page moved on)"
        elif msg and any(self.ERROR_RX.search(m) for m in msg):
            outcome = "not saved - the form still asks for something: " + "; ".join(msg)
        elif sent:
            outcome = "request sent, but the user was shown no confirmation"
        else:
            outcome = "nothing sent (form probably needs fields the agent cannot fill, e.g. chips/gender)"
        rec_row = {"app": rc.app, "role": rc.role, "path": rec.path, "where": where, "button": redact(label), "fields_filled": n,
                   "marker": self.marker, "writes_sent": len(sent), "outcome": outcome, "messages": msg}
        self.created.append(rec_row)
        repro = self._repro(rc, rec.path, *repro_prefix, f"Fill the form with valid data and click '{label}'")
        if sent and outcome.startswith("request sent"):
            self.add(rc, f"nofeedback-save:{rec.path}:{where}", "Medium", "validation", f"No confirmation after saving valid data: {rec.path}",
                     f"After filling '{where}' with valid data and clicking '{label}', a request was sent but the user saw no success or error message.",
                     rec.path, [f"{len(sent)} write request(s)"], repro, shot=True, page=page)
        self._harvest(rc, rec.path, repro, page, f"after saving valid data with '{label}'")
        rec.checks.append(f"filled {n} field(s) and saved: {outcome[:60]}")
        return rec_row

    # ------------------------------------------------------------------ anonymous + cross-role checks
    def _anonymous_login_page(self, browser, app):
        """Look at the public login page itself (no credentials): loads, no console/API errors."""
        from utils.monitor import PageMonitor
        ctx, guard, origin = self._new_ctx(browser, app)
        guard.block_all = True
        try:
            page = ctx.new_page()
            mon = PageMonitor(page)
            rc = _RoleRun(app, "anonymous", self._base(app), origin, ctx, page, guard, mon, time.time() + 60)
            for url in self._login_urls(app):
                status, load_s, err = self._goto(rc, url)
                if status is not None and status < 400 and page.locator('input[type="password"]').first.is_visible():
                    path = norm_path(page.url)
                    if load_s > self.cfg.slow_s:
                        self.add(rc, f"slow:{path}", "Low", "performance", f"Slow login page ({load_s:.1f}s)", "The login page is slow to load.", path, [f"{load_s:.1f}s"],
                                 [f"Open {path}"])
                    self._harvest(rc, path, [f"Open {path} without signing in"])
                    break
                if status and status >= 500:
                    self.add(rc, f"http:{norm_path(url)}:{status}", "High", "http-error", f"Login page returns HTTP {status}",
                             "The public login page returned a server error.", norm_path(url), [f"HTTP {status}"], [f"Open {norm_path(url)}"])
                    break
        finally:
            ctx.close()

    def _renders_like(self, page, headings) -> str:
        """'' = looks protected; otherwise 'strong' | 'weak' = the authenticated content/shell is visible to this visitor."""
        if self._logged_out_page(page):
            return ""
        try:
            body = page.inner_text("body", timeout=3000)
        except Exception:
            return ""
        if len(body.strip()) < 20:
            return ""
        hs = [h for h in headings if len(h) >= 5]
        if not hs or not any(h.lower() in body.lower() for h in hs):
            return ""
        facts = page.evaluate(PAGE_FACTS_JS)
        try:      # the sidebar alone can be hundreds of characters: judge the main content area when there is one
            main_len = page.evaluate("() => (document.querySelector('main,[role=main]') || document.body).innerText.length")
        except Exception:
            main_len = len(body)
        return "strong" if (facts["tables"] or facts["inputs"] >= 2 or main_len > 400) else "weak"

    @staticmethod
    def _logged_out_page(page) -> bool:
        try:
            return bool(LOGIN_URL_RX.search(page.url)) or page.locator('input[type="password"]').first.is_visible()
        except Exception:
            return False

    def _anonymous_access(self, browser, app):
        targets = {}
        for (a, path), owners in self.owner_pages.items():
            if a == app and "#" not in path and path not in ("/",):
                targets[path] = next(iter(owners.values()))
        if not targets:
            return
        from utils.monitor import PageMonitor
        ctx, guard, origin = self._new_ctx(browser, app)
        guard.block_all = True
        base = self._base(app)
        tested = 0
        try:
            page = ctx.new_page()
            page.set_default_timeout(15000)
            rc = _RoleRun(app, "anonymous", base, origin, ctx, page, guard, PageMonitor(page), time.time() + 240)
            for path, headings in list(targets.items())[:30]:
                if time.time() > rc.deadline:
                    break
                if ":id" in path:
                    continue
                self._goto(rc, base + path)
                page.wait_for_timeout(800)
                tested += 1
                verdict = self._renders_like(page, headings)
                if verdict:
                    sev = "High" if verdict == "strong" else "Medium"
                    self.add(rc, f"anon:{path}", sev, "access-control", f"Page opens without logging in: {path}",
                             "A visitor with NO session can open this page and sees the same headings as the logged-in user"
                             + (" plus data/forms." if verdict == "strong" else " (the shell renders; data may still be protected by the API - verify)."),
                             path, [f"headings visible anonymously: {', '.join(redact(h)[:30] for h in headings[:3])}"],
                             ["Open a private/incognito window", f"Go directly to {base}{path} without signing in"], shot=True, page=page)
        finally:
            ctx.close()
        self.notes.append(f"anonymous access check ({app}): {tested} page(s) opened without a session")

    def _cross_role_access(self, browser, app):
        roles = sorted({r for (a, _), owners in self.owner_pages.items() if a == app for r in owners})
        if len(roles) < 2:
            return
        from utils.monitor import PageMonitor
        for role in roles:
            state = self.sessions.get((app, role))
            if state is None:
                continue
            foreign = [(p, hs[next(iter(hs))]) for (a, p), hs in self.owner_pages.items()
                       if a == app and role not in hs and "#" not in p and ":id" not in p and len(hs) == 1]
            if not foreign:
                continue
            ctx, guard, origin = self._new_ctx(browser, app, state)
            guard.block_all = True
            try:
                page = ctx.new_page()
                page.set_default_timeout(15000)
                rc = _RoleRun(app, role, self._base(app), origin, ctx, page, guard, PageMonitor(page), time.time() + 180)
                tested = 0
                for path, headings in foreign[:12]:
                    if time.time() > rc.deadline:
                        break
                    owner = next(iter(self.owner_pages[(app, path)]))
                    refusals = []
                    handler = (lambda resp: refusals.append(f"{resp.status} {norm_path(resp.url)}")
                               if resp.status in (401, 403) and "/api" in resp.url and len(refusals) < 4 else None)
                    page.on("response", handler)
                    try:
                        self._goto(rc, self._base(app) + path)
                        page.wait_for_timeout(900)
                    finally:
                        page.remove_listener("response", handler)
                    tested += 1
                    if norm_path(page.url) != norm_path(path):        # the app sent this role somewhere else: the route IS protected
                        continue
                    verdict = self._renders_like(page, headings)
                    if not verdict:
                        continue
                    repro = [f"Sign in as '{role}'", f"Type {path} into the address bar"]
                    if refusals:
                        self.add(rc, f"cross-shell:{owner}", "Low", "access-control",
                                 f"Role '{role}' can open the screens of '{owner}' (the server refuses their data)",
                                 f"Typing these addresses as '{role}' shows the page layout of '{owner}', but the server answered 401/403 for the data, so nothing private "
                                 "was exposed. Low priority: ideally the app redirects the wrong role instead of showing an empty screen.",
                                 path, [f"{path}: {refusals[0]}"], repro, per_role=True)
                    else:
                        sev = "Medium" if verdict == "strong" else "Low"
                        self.add(rc, f"cross:{path}", sev, "access-control",
                                 f"Role '{role}' can open a page only '{owner}' reached by browsing: {path}",
                                 f"The '{role}' user did not find this page through the menu, but typing the address stays on that page and shows the same "
                                 f"{'content' if verdict == 'strong' else 'layout (little data)'} as for '{owner}', and the server did NOT refuse any data request. "
                                 "If this page should be limited to that role, the route is not protected. If it is meant to be shared, ignore this.",
                                 path, [f"owner role: {owner}", f"stayed on {norm_path(page.url)}", f"match strength: {verdict}", "no 401/403 seen"], repro,
                                 shot=True, page=page, per_role=True)
                self.notes.append(f"cross-role check ({app}/{role}): {tested} page(s) from other roles opened")
            finally:
                ctx.close()