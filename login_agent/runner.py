"""Runs the plan: gates each test, drives Playwright, collects results. Statuses: PASS | FAIL | BLOCKED | NOT TESTED."""
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from .artifacts import safe_name, safe_screenshot
from .errors import SafetyBlocked
from .redaction import register_secret
from .safety import get_environment, normalize_origin, verify_environment

from .actions import Blocked, Executor, StepFailed
from .guard import NetworkGuard, Observer, PostBudget
from .plan import Plan, TemplateError, render, resolve_identity

STAGING_LIKE = ("staging", "local")


@dataclass
class Target:
    env_name: str
    env_kind: str
    base_url: str
    base_origin: str
    login_origins: set
    allowed_origins: set


@dataclass
class TestResult:
    __test__ = False          # not a pytest test class
    id: str
    title: str
    severity: str
    requires: str
    status: str = "NOT TESTED"
    reason: str = ""
    duration_ms: int = 0
    steps: list = field(default_factory=list)       # {"n", "action", "status", "detail"}
    items: list = field(default_factory=list)       # one entry per ?redirect= payload etc.
    screenshots: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    console_errors: int = 0


def resolve_target(env_name: str, env_file=None, base_url_override: str = "") -> Target:
    """Fail closed: the base URL must belong to the environment declared in config/environments.yml."""
    env = get_environment(env_name, env_file)
    declared = env.base_urls.get("nurseconnect", "")
    base = (base_url_override or os.getenv("LOGIN_TEST_BASE_URL", "") or declared).strip()
    if not base:
        raise SafetyBlocked(f"environment {env_name!r} declares no nurseconnect base_url and LOGIN_TEST_BASE_URL is empty")
    origin = normalize_origin(base)
    if env.kind == "production":
        if not declared or origin != normalize_origin(declared):
            raise SafetyBlocked(f"{origin} is not the production origin declared for {env_name!r} ({declared or 'none'})")
        login_origins, allowed = set(), {origin}
    else:
        allowed = set(env.allowed_origins)
        if origin not in allowed:
            raise SafetyBlocked(f"{origin} is not in allowed_origins of environment {env_name!r}")
        if origin in env.production_origins:
            raise SafetyBlocked(f"{origin} is a production origin")
        login_origins = {origin} | set(env.auth_origins) | {normalize_origin(v) for v in env.api_urls.values()}
        extra = os.getenv("LOGIN_TEST_API_ORIGIN", "").strip()
        if extra:
            e = normalize_origin(extra)
            if e not in allowed:
                raise SafetyBlocked(f"LOGIN_TEST_API_ORIGIN {e} is not in allowed_origins of environment {env_name!r}")
            login_origins.add(e)
        bad = login_origins & set(env.production_origins)
        if bad:
            raise SafetyBlocked(f"login origin(s) {sorted(bad)} are production origins and can never receive a login request")
        verify_environment(env, "nurseconnect")        # optional probe declared in environments.yml; no-op when none declared
    return Target(env.name, env.kind, base.rstrip("/"), origin, login_origins, allowed)


def gate(plan: Plan, test, tgt: Target):
    """Decide before any browser starts. Returns (status, reason) or (None, "") when the test may run."""
    if test.requires == "staging" and tgt.env_kind not in STAGING_LIKE:
        return "NOT TESTED", f"login/credential tests never run on {tgt.env_kind}; use --env staging against a staging site"
    if test.identity and test.requires == "staging":
        ident = resolve_identity(test.identity, plan.identities[test.identity])
        if ident.missing:
            return "BLOCKED", "missing in .env: " + ", ".join(ident.missing)
    return None, ""


def _items(plan: Plan, test):
    if not test.foreach:
        return [None]
    src = plan.redirect_payloads
    return [p for p in src if all(p.get(k) == v for k, v in test.foreach_where.items())]


def run_suite(plan: Plan, tgt: Target, *, only=None, tags=None, skip=None, headed=False, slow_mo=0, screenshot_mode="masked",
              out_dir: Path, dry_run=False, log=print):
    only, tags, skip = set(only or []), set(tags or []), set(skip or [])
    selected = [t for t in plan.tests
                if (not only or t.id in only) and (not tags or tags & set(t.tags)) and t.id not in skip]
    results = []
    runnable = []
    for t in selected:
        r = TestResult(t.id, t.title, t.severity, t.requires)
        status, why = gate(plan, t, tgt)
        if status:
            r.status, r.reason = status, why
        else:
            r.status, r.reason = "NOT TESTED", "dry run" if dry_run else ""
            runnable.append((t, r))
        results.append(r)
    if dry_run or not runnable:
        return results

    mode = screenshot_mode
    if mode == "full" and (tgt.env_kind not in STAGING_LIKE or os.getenv("CI", "").lower() in ("1", "true")):
        mode = "masked"
    shots = Path(out_dir) / "screenshots"
    budget = PostBudget(int(plan.limits.get("max_login_posts_per_run", 14)) if tgt.env_kind in STAGING_LIKE else 0)
    for ident in plan.identities.values():            # every secret that may appear is masked everywhere, regardless of pattern
        i = resolve_identity("x", ident)
        register_secret(i.password)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed, slow_mo=slow_mo)
        try:
            ok, why = _preflight(browser, plan, tgt)
            if not ok:
                for t, r in runnable:
                    r.status, r.reason = "BLOCKED", why
                return results
            for t, r in runnable:
                _run_test(browser, plan, tgt, t, r, budget, shots, mode, log)
        finally:
            browser.close()
    return results


def _new_context(browser, tgt, plan):
    ctx = browser.new_context(viewport={"width": 1280, "height": 800}, locale="en-IN", ignore_https_errors=False)
    ctx.set_default_timeout(int(plan.limits.get("step_timeout_ms", 15000)))
    return ctx


def _preflight(browser, plan, tgt):
    """One GET of the login page. If the site cannot be reached every test is BLOCKED at once (not a slow wall of FAILs)."""
    ctx = _new_context(browser, tgt, plan)
    NetworkGuard(tgt.env_kind, tgt.login_origins, PostBudget(0)).attach(ctx)     # the preflight page may run scripts too: nothing but GET
    try:
        page = ctx.new_page()
        page.goto(tgt.base_url + plan.target.get("login_path", "/auth/login"), wait_until="domcontentloaded",
                  timeout=int(plan.limits.get("navigation_timeout_ms", 30000)))
        return True, ""
    except PWError as e:
        return False, f"target not reachable from this machine: {str(e).splitlines()[0][:200]}"
    finally:
        ctx.close()


def _run_test(browser, plan, tgt, test, res: TestResult, budget, shots_dir, shot_mode, log):
    t0 = time.monotonic()
    items = _items(plan, test)
    subs = []
    for item in items:
        sub = _run_once(browser, plan, tgt, test, item, budget, shots_dir, shot_mode)
        subs.append((item, sub))
        if item:
            res.items.append({"item": f"{item['id']} {item.get('label', '')}".strip(), "status": sub["status"], "reason": sub["reason"]})
        res.screenshots += sub["screenshots"]
        res.notes += [n for n in sub["notes"] if n not in res.notes]
        res.console_errors += sub["console_errors"]
    statuses = [s["status"] for _, s in subs]
    first_bad = next(((i, s) for i, s in subs if s["status"] != "PASS"), None)
    res.steps = (first_bad[1] if first_bad else subs[0][1])["steps"] if subs else []
    if not subs:
        res.status, res.reason = "NOT TESTED", "no data items matched foreach_where"
    elif "FAIL" in statuses:
        res.status = "FAIL"
        i, s = next((i, s) for i, s in subs if s["status"] == "FAIL")
        res.reason = (f"[{i['id']}] " if i else "") + s["reason"]
        if len(subs) > 1:
            res.reason += f"  ({statuses.count('FAIL')} of {len(subs)} payloads failed)"
    elif "BLOCKED" in statuses:
        res.status = "BLOCKED"
        res.reason = next(s["reason"] for _, s in subs if s["status"] == "BLOCKED")
    else:
        res.status, res.reason = "PASS", ""
    res.duration_ms = int((time.monotonic() - t0) * 1000)
    log(f"{res.status:<10} {res.id}  {res.title}  ({res.duration_ms / 1000:.1f}s)" + (f"\n           -> {res.reason}" if res.reason else ""))


def _run_once(browser, plan, tgt, test, item, budget, shots_dir, shot_mode):
    ctx = _new_context(browser, tgt, plan)
    guard = NetworkGuard(tgt.env_kind, tgt.login_origins, budget).attach(ctx)
    guard.login_post_allowed = bool(test.login_post)
    page = ctx.new_page()
    obs = Observer(page)
    notes: list = []
    ex = Executor(page, ctx, obs, guard, plan.selectors, tgt.base_url, tgt.env_kind, plan.limits, notes)
    ident = resolve_identity(test.identity, plan.identities[test.identity]) if test.identity else None
    scopes = {"target": plan.target, "data": plan.data, "patterns": plan.patterns, "item": item or {}}
    if ident:
        scopes["identity"] = ident
    steps, status, reason, shots = [], "PASS", "", []
    try:
        for n, raw in enumerate(test.steps, 1):
            rec = {"n": n, "action": raw["action"], "status": "PASS", "detail": ""}
            steps.append(rec)
            try:
                rec["detail"] = ex.run(render(raw, scopes))
                if rec["detail"].startswith("skipped"):
                    rec["status"] = "SKIPPED"
            except StepFailed as e:
                rec["status"], rec["detail"] = "FAIL", str(e)
                status, reason = "FAIL", f"step {n} ({raw['action']}): {e}"
                break
            except TemplateError as e:
                rec["status"], rec["detail"] = "BLOCKED", str(e)
                status, reason = "BLOCKED", str(e)
                break
            except Blocked as e:
                rec["status"], rec["detail"] = "BLOCKED", str(e)
                status, reason = "BLOCKED", str(e)
                break
            except PWError as e:
                first = str(e).splitlines()[0][:200]
                rec["status"], rec["detail"] = "FAIL", first
                status, reason = "FAIL", f"step {n} ({raw['action']}): {first}"
                break
        if status == "FAIL" and shot_mode != "off":
            name = safe_name(test.id + (f"-{item['id']}" if item else "")) + ".png"
            p = safe_screenshot(page, Path(shots_dir) / name, mode=shot_mode)
            if p:
                shots.append("screenshots/" + name)
            else:
                notes.append("no screenshot was written (SCREENSHOT_MODE/another process-wide screenshot guard is active, or the capture failed)")
    finally:
        if guard.blocked:
            uniq = list(dict.fromkeys(guard.blocked))
            notes.append(f"{len(guard.blocked)} request(s) blocked by the agent's safety guard, e.g. {uniq[0]}")
        if obs.edge_beacons:
            notes.append(f"{obs.edge_beacons} Cloudflare edge beacon request(s) (/cdn-cgi/...) were blocked and are not counted as page writes")
        errs = obs.console_errors + obs.page_errors
        ctx.close()
    return {"status": status, "reason": reason, "steps": steps, "screenshots": shots, "notes": notes, "console_errors": errs}
