"""Login page object.

What changed in Stage 1 (and why):
  * "Login worked" now means: password field gone AND we left the login URL (and are under the role's
    expected area, if known) AND the new page has real visible text.  Before, "password field gone"
    alone counted as success, so a half-rendered SPA page was reported as a blank page.
  * After a failed login we do NOT look for a CSS class once.  A MutationObserver records every piece of
    text that appears on the page (even a toast that disappears after 2 seconds) and we compare it with
    what was already on the page before the click.
  * We record the HTTP status of the login request (401, 422 ...) so reports can say what the server did.
  * A screenshot is taken right after the server answers, while any toast is still on screen.
"""
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from config import NAV_TIMEOUT, SHOTS, RUN_ID, LOGIN_TIMEOUT_S
from utils.recorder import Blocked

EMAIL_SEL = ('input[type="email"], input[name*="email" i], input[id*="email" i], input[placeholder*="email" i], '
             'input[name*="user" i], input[id*="user" i], input[type="text"]')
PASS_SEL = 'input[type="password"]'
SUBMIT_SEL = ('button[type="submit"], input[type="submit"], button:has-text("Log in"), '
              'button:has-text("Login"), button:has-text("Sign in")')

# Anything on the page that is not a plain-text change still counts as "the app told the user something".
FEEDBACK_ELEMENTS_SEL = ('[role="alert"], [role="status"], [aria-live], [data-sonner-toast], [class*="toast" i], '
                         '[class*="error" i], [class*="invalid" i], [class*="danger" i], [aria-invalid="true"]')

MIN_CONTENT_CHARS = 20          # a logged-in page with fewer visible characters is treated as "not rendered"
FEEDBACK_WAIT_S = 3.0           # how long we wait for an error message after the server answers
_LOGIN_API = re.compile(r"login|signin|sign-in|auth|session|token", re.I)
_NOISE = re.compile(r"^(signing in|logging in|loading|please wait|log ?in|sign ?in)\W*$", re.I)

_OBSERVER_JS = """() => {
  window.__seen = [];
  const add = t => { t = (t || '').replace(/\\s+/g, ' ').trim(); if (t && t.length < 300) window.__seen.push(t); };
  const grab = n => { if (n.nodeType === 3) add(n.textContent); else if (n.nodeType === 1) add(n.innerText || n.textContent); };
  new MutationObserver(ms => ms.forEach(m => {
      m.addedNodes.forEach(grab);
      if (m.type === 'characterData') add(m.target.textContent);
  })).observe(document.documentElement, {childList: true, subtree: true, characterData: true});
}"""


@dataclass
class LoginResult:
    ok: bool
    error: str = ""                 # first piece of NEW visible feedback ("" = none seen)
    seconds: float = 0.0
    reason: str = ""                # ok | rejected | blank_after_login | unexpected_landing | timeout
    api_status: int = 0             # HTTP status of the login request (0 = none seen)
    feedback: list = field(default_factory=list)   # all new visible text after submit
    invalid_fields: int = 0         # fields flagged invalid by the browser / aria-invalid
    screenshot: str = ""            # taken right after the server answered
    final_url: str = ""


class LoginPage:
    def __init__(self, page, login_url: str = "", landing_prefix: str = ""):
        self.page = page
        self.login_url = login_url
        self.landing_prefix = landing_prefix

    # ---- helpers -----------------------------------------------------------
    def open(self, url=None):
        url = url or self.login_url
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        return resp

    def form_visible(self) -> bool:
        try:
            return self.page.locator(PASS_SEL).first.is_visible()
        except Exception:
            return False

    def visible_text_len(self) -> int:
        try:
            return len(self.page.evaluate("() => (document.body ? document.body.innerText : '').trim()"))
        except Exception:
            return 0

    def wait_for_content(self, timeout_ms=10000) -> int:
        """Wait until the page has real visible text (SPAs render after the HTML loads). Returns char count."""
        try:
            self.page.wait_for_function(
                f"() => document.body && document.body.innerText.trim().length >= {MIN_CONTENT_CHARS}",
                timeout=timeout_ms)
        except Exception:
            pass
        return self.visible_text_len()

    def _path(self, url: str) -> str:
        return (urlparse(url).path.rstrip("/") or "/")

    def _left_login(self) -> bool:
        cur = self._path(self.page.url)
        if self.login_url and cur == self._path(self.login_url):
            return False
        if self.landing_prefix and not cur.startswith(self.landing_prefix.rstrip("/")):
            return False
        return True

    def _baseline_lines(self) -> set:
        try:
            txt = self.page.evaluate("() => document.body ? document.body.innerText : ''")
        except Exception:
            txt = ""
        return {re.sub(r"\s+", " ", l).strip() for l in txt.splitlines() if l.strip()}

    def _invalid_count(self) -> int:
        try:
            return self.page.evaluate(
                "() => document.querySelectorAll('input:invalid, [aria-invalid=\"true\"]').length")
        except Exception:
            return 0

    def _new_feedback(self, baseline: set) -> list:
        try:
            seen = self.page.evaluate("() => window.__seen || []")
        except Exception:
            seen = []
        out = []
        for raw in seen:
            for line in raw.splitlines():
                line = re.sub(r"\s+", " ", line).strip()
                if line and line not in baseline and not _NOISE.match(line) and line not in out:
                    out.append(line)
        return out

    def _shot(self, tag: str) -> str:
        try:
            p = SHOTS / f"{RUN_ID}_login_{tag}_{int(time.time() * 1000) % 100000}.png"
            self.page.screenshot(path=str(p))
            return str(p)
        except Exception:
            return ""

    # ---- main action -------------------------------------------------------
    def submit(self, email: str, password: str, empty_ok=False) -> LoginResult:
        t0 = time.time()
        pg = self.page
        if not self.form_visible():
            raise Blocked("No visible password field on the login URL (check *_LOGIN_PATH in .env)")

        if email or not empty_ok:
            pg.locator(EMAIL_SEL).first.fill(email)
        if password or not empty_ok:
            pg.locator(PASS_SEL).first.fill(password)

        api = {"status": 0, "at": 0.0}

        def on_response(resp):
            try:
                if not api["status"] and resp.request.method in ("POST", "PUT") and _LOGIN_API.search(resp.url):
                    api["status"], api["at"] = resp.status, time.time()
            except Exception:
                pass

        pg.on("response", on_response)
        try:
            baseline = self._baseline_lines()
            try:
                pg.evaluate(_OBSERVER_JS)
            except Exception:
                pass
            pg.locator(SUBMIT_SEL).first.click()

            deadline = t0 + LOGIN_TIMEOUT_S
            shot, shot_taken = "", False
            reason = "timeout"
            while time.time() < deadline:
                pg.wait_for_timeout(250)
                if api["status"] and not shot_taken and time.time() - api["at"] >= 0.8:
                    shot, shot_taken = self._shot("after_response"), True      # toast is usually still visible
                if not self.form_visible() and self._left_login():
                    if self.visible_text_len() >= MIN_CONTENT_CHARS:
                        try:
                            pg.wait_for_load_state("networkidle", timeout=5000)
                        except Exception:
                            pass
                        return LoginResult(True, "", round(time.time() - t0, 2), "ok", api["status"],
                                           final_url=pg.url, screenshot=shot)
                    reason = "blank_after_login"      # keep waiting - SPA may still be rendering
                elif not self.form_visible() and not self._left_login():
                    reason = "unexpected_landing"
                if api["status"] >= 400 and time.time() - api["at"] >= FEEDBACK_WAIT_S:
                    reason = "rejected"
                    break
            if reason == "timeout" and api["status"] >= 400:
                reason = "rejected"
            if not shot:
                shot = self._shot("end")
            feedback = self._new_feedback(baseline)
            return LoginResult(False, feedback[0] if feedback else "", round(time.time() - t0, 2), reason,
                               api["status"], feedback, self._invalid_count(), shot, pg.url)
        finally:
            try:
                pg.remove_listener("response", on_response)
            except Exception:
                pass
