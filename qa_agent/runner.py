"""Executes APPROVED workflows with Playwright. Execution logic only - definitions live in YAML.

Order of gates for every workflow (any failure => the workflow is not run and is reported, never silently skipped):
  1. selected (app / --workflow filter)
  2. approval record valid for the CURRENT content (approvals.py)
  3. write gate (safety.check_write_allowed) when the workflow writes; read-only mode turns writes into NOT TESTED
  4. credentials / base URL available
  5. run in an isolated browser context with a network RequestGuard attached
"""
import os
import platform
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .approvals import ApprovalStore
from .artifacts import safe_name, safe_screenshot, screenshot_mode
from .errors import (AgentError, ApprovalError, SafetyBlocked, StepBlocked, StepNotTested, StepNotVerified)
from .redaction import redact, register_secret
from .safety import Environment, RequestGuard, check_write_allowed, normalize_origin
from .schema import DANGEROUS_RX
from .workflows import expand, synth_values

STATUSES = ("PASS", "FAIL", "BLOCKED", "NOT VERIFIED", "NOT TESTED")
LOGIN_URL_RX = re.compile(r"/(?:auth/)?(?:login|signin|sign-in)\b", re.I)


@dataclass
class StepResult:
    n: int
    action: str
    expected: str
    actual: str = ""
    status: str = "NOT TESTED"
    severity: str = ""
    duration_s: float = 0.0
    url: str = ""
    error: str = ""
    screenshot: str = ""
    console_errors: list = field(default_factory=list)
    failed_requests: list = field(default_factory=list)
    timestamp: str = ""


@dataclass
class WorkflowResult:
    id: str
    app: str
    title: str
    status: str
    reason: str = ""
    approval_state: str = ""
    risk: str = ""
    writes: bool = False
    role: str = ""
    steps: list = field(default_factory=list)
    started: str = ""
    finished: str = ""
    blocked_requests: list = field(default_factory=list)
    source_file: str = ""


@dataclass
class RunConfig:
    apps: tuple = ("nurseconnect", "hospital")
    env: Environment | None = None
    mode: str = "run"                         # run | read-only | dry-run
    allow_write: bool = False
    confirm_phrase: str = ""
    confirmed_workflows: tuple = ()
    only: tuple = ()
    base_urls: dict | None = None
    headless: bool = True
    browser: str = "chromium"
    slow_mo: int = 0
    viewport: tuple = (1440, 900)
    ignore_https_errors: bool = False
    artifacts_dir: Path = Path("artifacts")
    run_id: str = ""
    store: ApprovalStore | None = None
    login_fn: object = None                   # callable(page, role, base_url) -> None; raises StepBlocked/AssertionError
    fetch: object = None                      # injectable HTTP fetch for environment verification (tests)
    screenshot_mode: str = ""
    lock_dir: Path | None = None


def default_login(page, role_name: str, base_url: str):
    """Reuse the existing heuristic LoginPage + config.ROLES credentials (read from environment variables)."""
    import config
    from pages.login_page import LoginPage
    role = config.ROLES.get(role_name)
    if role is None:
        raise StepBlocked(f"Unknown role {role_name!r} (not defined in config.ROLES or filtered by APP_ONLY)")
    if not role.email or not role.password:
        raise StepBlocked(f"Credentials missing: set {role.email_env} and {role.password_env}")
    register_secret(role.password)
    login_path = config.APPS[role.app].login_path if role.app in config.APPS else "/"
    lp = LoginPage(page, base_url.rstrip("/") + login_path, role.landing_prefix)
    lp.open()
    res = lp.submit(role.email, role.password)
    if not res.ok:
        raise AssertionError(f"Login failed for role {role_name}: {redact(res.error) or res.reason} (HTTP {res.api_status or 'n/a'})")


class _Lock:
    """Cross-process lock so two runs never write test data to the same environment+app at once."""

    def __init__(self, path: Path, wait_s: float = 60, stale_s: float = 900):
        self.path, self.wait_s, self.stale_s, self.fd = Path(path), wait_s, stale_s, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        end = time.time() + self.wait_s
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, f"{os.getpid()} {time.time()}".encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.stale_s:
                        self.path.unlink()
                        continue
                except OSError:
                    pass
                if time.time() > end:
                    raise StepBlocked(f"Another run holds the write lock {self.path.name}; not sharing mutable test data")
                time.sleep(0.5)

    def __exit__(self, *a):
        try:
            if self.fd is not None:
                os.close(self.fd)
            self.path.unlink()
        except OSError:
            pass


def _roll_up(steps) -> str:
    sts = [s.status for s in steps]
    for s in ("FAIL", "BLOCKED", "NOT VERIFIED"):
        if s in sts:
            return s
    if sts and all(s == "NOT TESTED" for s in sts):
        return "NOT TESTED"
    return "PASS" if sts else "NOT TESTED"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Runner:
    def __init__(self, cfg: RunConfig):
        self.cfg = cfg
        self.store = cfg.store or ApprovalStore()
        self.run_id = cfg.run_id or os.getenv("RUN_ID") or datetime.now().strftime("%Y%m%d%H%M%S")
        self.meta = {"run_id": self.run_id, "started": _now(), "mode": cfg.mode,
                     "environment": cfg.env.name if cfg.env else "(none - read-only)",
                     "environment_kind": cfg.env.kind if cfg.env else "unverified",
                     "python": platform.python_version(), "platform": platform.platform(),
                     "browser": cfg.browser, "browser_version": "(not launched)"}
        self.shot_mode = cfg.screenshot_mode or screenshot_mode(cfg.env.kind if cfg.env else None)

    # ---- gates ---------------------------------------------------------------------------------
    def _base_url(self, app: str) -> str:
        if self.cfg.base_urls and self.cfg.base_urls.get(app):
            return self.cfg.base_urls[app]
        if self.cfg.env:
            return self.cfg.env.base_url(app)
        raise SafetyBlocked(f"No base URL for {app} (pass --base-url or select an environment)")

    def evaluate_gates(self, wf: dict) -> tuple:
        """-> (status|None, reason, approval_state). status None means 'allowed to run'."""
        state, detail = self.store.state(wf)
        if state != "approved":
            return "BLOCKED", f"NOT APPROVED ({state}): {detail}", state
        try:
            base = self._base_url(wf["app"])
            origin = normalize_origin(base)
        except SafetyBlocked as e:
            return "BLOCKED", str(e), state
        if self.cfg.env is not None and self.cfg.env.kind in ("staging", "local") and origin not in self.cfg.env.allowed_origins:
            return "BLOCKED", f"Base URL origin {origin} is not in the allowlist of environment {self.cfg.env.name!r}", state
        if wf["writes"]:
            if self.cfg.mode == "read-only":
                return "NOT TESTED", "read-only mode: workflow writes test records", state
            try:
                check_write_allowed(self.cfg.env, wf["app"], wf["id"], allow_write=self.cfg.allow_write,
                                    confirm_phrase=self.cfg.confirm_phrase, confirmed_workflows=self.cfg.confirmed_workflows,
                                    fetch=self.cfg.fetch)
            except SafetyBlocked as e:
                return "BLOCKED", f"WRITE BLOCKED: {e}", state
        return None, "", state

    # ---- run -----------------------------------------------------------------------------------
    def run(self, workflows) -> list:
        results, runnable = [], []
        for wf in workflows:
            if wf["app"] not in self.cfg.apps or (self.cfg.only and wf["id"] not in self.cfg.only):
                continue
            status, reason, state = self.evaluate_gates(wf)
            base = WorkflowResult(id=wf["id"], app=wf["app"], title=wf["title"], status=status or "NOT TESTED", reason=reason,
                                  approval_state=state, risk=wf["risk"], writes=wf["writes"], role=wf.get("role", ""),
                                  source_file=Path(wf.get("_file", "")).name)
            if status is not None:
                base.started = base.finished = _now()
                results.append(base)
            elif self.cfg.mode == "dry-run":
                base.reason = f"dry-run: all gates passed; would execute {len(wf['steps'])} step(s)"
                base.started = base.finished = _now()
                results.append(base)
            else:
                runnable.append((wf, base))
        if runnable:
            results.extend(self._run_browser(runnable))
        self.meta["finished"] = _now()
        order = {w["id"]: i for i, w in enumerate(workflows)}
        results.sort(key=lambda r: order.get(r.id, 0))
        return results

    def _run_browser(self, runnable) -> list:
        from playwright.sync_api import sync_playwright
        out = []
        with sync_playwright() as p:
            browser = getattr(p, self.cfg.browser).launch(headless=self.cfg.headless, slow_mo=self.cfg.slow_mo)
            self.meta["browser_version"] = f"{self.cfg.browser} {browser.version}"
            try:
                for wf, res in runnable:
                    lock = None
                    if wf["writes"]:
                        envname = self.cfg.env.name if self.cfg.env else "none"
                        lock = _Lock((self.cfg.lock_dir or self.cfg.artifacts_dir / ".locks") / f"{safe_name(envname)}-{wf['app']}.lock")
                    try:
                        if lock:
                            with lock:
                                self._run_one(browser, wf, res)
                        else:
                            self._run_one(browser, wf, res)
                    except StepBlocked as e:
                        res.status, res.reason = "BLOCKED", redact(str(e))
                    out.append(res)
            finally:
                browser.close()
        return out

    # ---- one workflow --------------------------------------------------------------------------
    def _run_one(self, browser, wf: dict, res: WorkflowResult):
        from utils.monitor import PageMonitor
        cfg = self.cfg
        res.started = _now()
        base = self._base_url(wf["app"])
        origin = normalize_origin(base)
        ctx = browser.new_context(viewport={"width": cfg.viewport[0], "height": cfg.viewport[1]}, ignore_https_errors=cfg.ignore_https_errors,
                                  accept_downloads=False, service_workers="block")
        guard = RequestGuard(cfg.env, origin, writes_enabled=bool(wf["writes"]))
        guard.attach(ctx)
        page = ctx.new_page()
        mon = PageMonitor(page)
        values = synth_values(self.run_id, wf["id"])
        default_timeout = wf.get("timeout_ms", 30000)
        results = []
        failed = False
        try:
            if wf.get("role"):
                login = cfg.login_fn or default_login
                t0 = time.time()
                sr = StepResult(0, "login", f"Logged in as role '{wf['role']}'", timestamp=_now())
                page.set_default_timeout(default_timeout)
                try:
                    login(page, wf["role"], base)
                    sr.status, sr.actual = "PASS", "Login completed"
                except StepBlocked as e:
                    sr.status, sr.error = "BLOCKED", redact(str(e))
                except AssertionError as e:
                    sr.status, sr.error = "FAIL", redact(str(e))
                except Exception as e:
                    sr.status, sr.error = "FAIL", redact(f"{type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}")
                sr.duration_s, sr.url = round(time.time() - t0, 2), redact(page.url)
                sr.actual = sr.actual or sr.error
                if sr.status != "PASS":
                    sr.severity = "High" if sr.status == "FAIL" else ""
                    results.append(sr)
                    failed = True
                    for n, st in enumerate(wf["steps"], 1):
                        results.append(StepResult(n, st["action"], st.get("expected", ""), status="NOT TESTED",
                                                  actual=f"not run: login {sr.status}", timestamp=_now()))
                else:
                    results.append(sr)
                mon.clear()
            if not failed:
                for n, st in enumerate(wf["steps"], 1):
                    sr = StepResult(n, st["action"], st.get("expected") or f"{st['action']} succeeds", timestamp=_now())
                    c0, f0 = len(mon.console), len(mon.failed)
                    t0 = time.time()
                    timeout = st.get("timeout_ms", default_timeout)
                    page.set_default_timeout(timeout)
                    try:
                        sr.actual = self._do(page, guard, wf, st, base, origin, values, timeout, res) or "Step completed"
                        sr.status = "PASS"
                    except StepBlocked as e:
                        sr.status, sr.error = "BLOCKED", str(e)
                    except StepNotTested as e:
                        sr.status, sr.error = "NOT TESTED", str(e)
                    except StepNotVerified as e:
                        sr.status, sr.error = "NOT VERIFIED", str(e)
                    except AssertionError as e:
                        sr.status, sr.error = "FAIL", str(e) or "Assertion failed"
                    except Exception as e:
                        name = type(e).__name__
                        first = (str(e).strip().splitlines() or [""])[0][:240]
                        if name == "TimeoutError":
                            sr.status, sr.error = "FAIL", f"Timeout after {timeout} ms: {first}"
                        elif "net::ERR" in first or "NS_ERROR" in first:
                            sr.status, sr.error = "FAIL", f"Navigation error: {first}"
                        else:
                            sr.status, sr.error = "FAIL", f"{name}: {first}"
                    if sr.status == "FAIL" and st.get("optional"):
                        sr.status = "NOT VERIFIED"
                        sr.error = "optional step failed: " + sr.error
                    sr.duration_s = round(time.time() - t0, 2)
                    try:
                        sr.url = page.url
                    except Exception:
                        pass
                    if sr.status == "FAIL":
                        sr.severity = st.get("severity", "Medium")
                    sr.console_errors = [redact(x) for x in mon.console[c0:][:8]]
                    sr.failed_requests = [redact(x) for x in mon.failed[f0:][:8]]
                    if sr.status in ("FAIL", "NOT VERIFIED") and self.shot_mode != "off":
                        shot = Path(cfg.artifacts_dir) / "screenshots" / f"{self.run_id}_{safe_name(wf['id'])}_{n:02d}_{sr.status.replace(' ', '')}.png"
                        sr.screenshot = safe_screenshot(page, shot, self.shot_mode)
                    sr.actual, sr.error, sr.url = redact(sr.actual), redact(sr.error), redact(sr.url)
                    results.append(sr)
                    if sr.status == "FAIL":
                        failed = True
                        for m, rest in enumerate(wf["steps"][n:], n + 1):
                            results.append(StepResult(m, rest["action"], rest.get("expected", ""), status="NOT TESTED",
                                                      actual=f"not run: step {n} failed", timestamp=_now()))
                        break
                    if sr.status == "BLOCKED":
                        for m, rest in enumerate(wf["steps"][n:], n + 1):
                            results.append(StepResult(m, rest["action"], rest.get("expected", ""), status="NOT TESTED",
                                                      actual=f"not run: step {n} blocked", timestamp=_now()))
                        break
        finally:
            res.steps = results
            res.blocked_requests = [redact(x) for x in guard.blocked[:50]]
            try:
                ctx.close()
            except Exception:
                pass
        res.status = _roll_up(results)
        bad = next((s for s in results if s.status == res.status and res.status != "PASS"), None)
        if bad:
            res.reason = redact(f"step {bad.n} ({bad.action}): {bad.error}")
        res.finished = _now()

    # ---- actions -------------------------------------------------------------------------------
    @staticmethod
    def locator(page, t: dict):
        if "role" in t:
            kw = {"exact": bool(t.get("exact", False))}
            if "name" in t:
                kw["name"] = t["name"]
            loc = page.get_by_role(t["role"], **kw)
        elif "label" in t:
            loc = page.get_by_label(t["label"], exact=bool(t.get("exact", False)))
        elif "placeholder" in t:
            loc = page.get_by_placeholder(t["placeholder"], exact=bool(t.get("exact", False)))
        elif "text" in t:
            loc = page.get_by_text(t["text"], exact=bool(t.get("exact", False)))
        elif "test_id" in t:
            loc = page.get_by_test_id(t["test_id"])
        else:
            loc = page.locator(t["css"])
        return loc.nth(t["nth"]) if "nth" in t else loc.first

    @staticmethod
    def _describe(t: dict) -> str:
        return ", ".join(f"{k}={v!r}" for k, v in t.items())

    @staticmethod
    def _settle(page):
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

    def _require_found(self, loc, t, timeout):
        try:
            loc.wait_for(state="attached", timeout=timeout)
        except Exception:
            raise AssertionError(f"Element not found within {timeout} ms: {self._describe(t)}")

    def _check_not_dangerous(self, loc, t):
        label = ""
        try:
            label = (loc.get_attribute("aria-label", timeout=1500) or "") + " " + (loc.inner_text(timeout=1500) or "")
        except Exception:
            pass
        if DANGEROUS_RX.search(label):
            raise StepBlocked(f"Refused to click an element whose visible text looks destructive/payment/messaging: {self._describe(t)}")

    @staticmethod
    def _poll(page, fn, timeout, interval_ms=250):
        """Retry `fn` until it returns (True, msg). Uses page.wait_for_timeout (NOT time.sleep): the sync Playwright API only
        processes browser events while it is inside a Playwright call, so sleeping would leave page.url etc. stale."""
        end = time.time() + timeout / 1000
        last = ""
        while True:
            try:
                ok, last = fn()
                if ok:
                    return last
            except Exception as e:
                last = str(e).splitlines()[0][:150] if str(e) else type(e).__name__
            if time.time() > end:
                raise AssertionError(last or "condition not met")
            page.wait_for_timeout(interval_ms)

    def _do(self, page, guard, wf, st, base, origin, values, timeout, res) -> str:
        a = st["action"]
        if a == "goto":
            url = origin + expand(st["path"], values)
            if normalize_origin(url) != origin:
                raise StepBlocked("goto may only target the app's own origin")
            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            self._settle(page)
            code = resp.status if resp else None
            if code and code >= 400:
                raise AssertionError(f"HTTP {code} for {st['path']}")
            return f"Loaded {st['path']} (HTTP {code})"
        if a in ("click", "login_submit", "guarded_submit", "submit"):
            loc = self.locator(page, st["target"])
            self._require_found(loc, st["target"], timeout)
            self._check_not_dangerous(loc, st["target"])
            if a == "guarded_submit":
                before_blocked = len(guard.blocked)
                guard.block_all = True
                try:
                    loc.click(timeout=timeout)
                    self._settle(page)
                finally:
                    guard.block_all = False
                return f"Clicked with all writes blocked; {len(guard.blocked) - before_blocked} write request(s) were prevented"
            loc.click(timeout=timeout)
            self._settle(page)
            return f"Clicked {self._describe(st['target'])}"
        if a == "fill":
            if "value_env" in st:
                v = os.environ.get(st["value_env"], "")
                if not v:
                    raise StepBlocked(f"Environment variable {st['value_env']} is not set")
                register_secret(v)
            else:
                v = expand(st["value"], values)
            loc = self.locator(page, st["target"])
            self._require_found(loc, st["target"], timeout)
            loc.fill(v, timeout=timeout)
            return f"Filled {self._describe(st['target'])} with {'a value from the environment' if 'value_env' in st else 'synthetic data'}"
        if a == "select_option":
            loc = self.locator(page, st["target"])
            self._require_found(loc, st["target"], timeout)
            loc.select_option(expand(st["value"], values), timeout=timeout)
            return "Option selected"
        if a == "check":
            loc = self.locator(page, st["target"])
            self._require_found(loc, st["target"], timeout)
            loc.check(timeout=timeout)
            return "Checked"
        if a == "press":
            if "target" in st:
                loc = self.locator(page, st["target"])
                self._require_found(loc, st["target"], timeout)
                loc.press(st["key"], timeout=timeout)
            else:
                page.keyboard.press(st["key"])
            self._settle(page)
            return f"Pressed {st['key']}"
        if a == "wait_for":
            if "ms" in st:
                page.wait_for_timeout(st["ms"])
                return f"Waited {st['ms']} ms"
            self.locator(page, st["target"]).wait_for(state="visible", timeout=timeout)
            return "Element became visible"
        if a == "open_section":
            from pages.app_page import AppPage
            if not AppPage(page).open_section(st["pattern"]):
                raise StepBlocked(f"No visible menu/link/button matching /{st['pattern']}/ - feature not found in the UI for this role")
            return f"Opened section /{st['pattern']}/"
        if a == "expect_visible":
            loc = self.locator(page, st["target"])
            try:
                loc.wait_for(state="visible", timeout=timeout)
            except Exception:
                raise AssertionError(f"Not visible within {timeout} ms: {self._describe(st['target'])}")
            return "Element is visible"
        if a == "expect_hidden":
            loc = self.locator(page, st["target"])
            try:
                loc.wait_for(state="hidden", timeout=timeout)
            except Exception:
                raise AssertionError(f"Still visible after {timeout} ms: {self._describe(st['target'])}")
            return "Element is hidden"
        if a in ("expect_text", "expect_url", "expect_title"):
            needle, rx = st.get("contains"), st.get("matches")
            needle = expand(needle, values) if needle is not None else None

            def ok(txt):
                return (needle.lower() in txt.lower()) if needle is not None else bool(re.search(rx, txt, re.I))

            def probe():
                if a == "expect_url":
                    txt = page.url
                elif a == "expect_title":
                    txt = page.title()
                elif "target" in st:
                    txt = self.locator(page, st["target"]).inner_text(timeout=1000)
                else:
                    txt = page.inner_text("body", timeout=1000)
                return ok(txt), f"{a[7:]} did not contain/match {'%r' % needle if needle is not None else '/' + rx + '/'}"
            self._poll(page, probe, timeout)
            return f"{a[7:]} matched"
        if a == "expect_count":
            def probe():
                n = self.locator_all(page, st["target"]).count()
                good = (("min" not in st or n >= st["min"]) and ("max" not in st or n <= st["max"]) and ("equals" not in st or n == st["equals"]))
                return good, f"found {n} element(s); wanted " + ", ".join(f"{k}={st[k]}" for k in ("min", "max", "equals") if k in st)
            return self._poll(page, probe, timeout)
        if a == "expect_authenticated":
            def probe():
                pw = page.locator('input[type="password"]').first
                on_login = bool(LOGIN_URL_RX.search(page.url)) or pw.is_visible()
                return (not on_login), "Login form/URL is showing: the session is not authenticated"
            self._poll(page, probe, timeout)
            return "Authenticated (no login form, not on a login URL)"
        if a == "expect_no_writes_sent":
            if guard.sent_writes and not wf["writes"]:
                raise AssertionError(f"{len(guard.sent_writes)} write request(s) reached the server: {guard.sent_writes[:3]}")
            return f"No write request reached the server ({len(guard.blocked)} attempt(s) blocked by the guard)"
        if a == "screenshot":
            if self.shot_mode == "off":
                raise StepNotTested("SCREENSHOT_MODE=off")
            path = Path(self.cfg.artifacts_dir) / "screenshots" / f"{self.run_id}_{safe_name(wf['id'])}_{safe_name(st.get('name', 'shot'))}.png"
            got = safe_screenshot(page, path, self.shot_mode)
            if not got:
                raise StepNotVerified("Screenshot could not be taken")
            return f"Screenshot saved ({self.shot_mode} mode)"
        raise AgentError(f"Unhandled action {a!r}")          # unreachable: the schema rejects unknown actions

    @staticmethod
    def locator_all(page, t):
        """Like locator() but without .first so counts are meaningful."""
        if "role" in t:
            kw = {"exact": bool(t.get("exact", False))}
            if "name" in t:
                kw["name"] = t["name"]
            return page.get_by_role(t["role"], **kw)
        if "label" in t:
            return page.get_by_label(t["label"], exact=bool(t.get("exact", False)))
        if "placeholder" in t:
            return page.get_by_placeholder(t["placeholder"], exact=bool(t.get("exact", False)))
        if "text" in t:
            return page.get_by_text(t["text"], exact=bool(t.get("exact", False)))
        if "test_id" in t:
            return page.get_by_test_id(t["test_id"])
        return page.locator(t["css"])
