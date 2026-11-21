"""Executes one YAML step. Every expectation polls until it holds or its timeout passes; a failed expectation raises
StepFailed (-> FAIL); a condition that stops the test from running at all raises Blocked (-> BLOCKED)."""
import re
import time
from urllib.parse import quote, urlencode, urlsplit

from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout


class StepFailed(AssertionError):
    pass


class Blocked(Exception):
    pass


_STORAGE_JS = """() => ['access_token','refresh_token','nc.session.v1'].some(k => {
  try { return !!(localStorage.getItem(k) || sessionStorage.getItem(k)); } catch (e) { return false; } })"""

_FOCUS_JS = """(map) => {
  const el = document.activeElement;
  if (!el || el === document.body) return {name: null, tag: 'body', focus_visible: false};
  let name = null;
  for (const [n, s] of Object.entries(map)) {
    let m; try { m = document.querySelector(s); } catch (e) { continue; }
    if (m === el) { name = n; break; }
  }
  const cs = getComputedStyle(el);
  const c = (cs.outlineColor || '').replace(/\\s+/g, '');
  const transparent = c === 'transparent' || /rgba\\(\\d+,\\d+,\\d+,0\\)$/.test(c);
  const outline = cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0 && !transparent;
  const shadow = !!cs.boxShadow && cs.boxShadow !== 'none';
  return {name, tag: el.tagName.toLowerCase(), focus_visible: outline || shadow};
}"""

_NAME_JS = """(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {found: false, via: null};
  const t = (x) => (x || '').trim();
  if (t(el.getAttribute('aria-label'))) return {found: true, via: 'aria-label'};
  const lb = el.getAttribute('aria-labelledby');
  if (lb && lb.split(/\\s+/).some(id => t((document.getElementById(id) || {}).textContent))) return {found: true, via: 'aria-labelledby'};
  if (el.labels && [...el.labels].some(l => t(l.textContent))) return {found: true, via: 'label'};
  if (['BUTTON', 'A'].includes(el.tagName)) {
    if (t(el.innerText || el.textContent)) return {found: true, via: 'content'};
    const img = el.querySelector('img[alt]'); if (img && t(img.alt)) return {found: true, via: 'img alt'};
    const st = el.querySelector('svg title'); if (st && t(st.textContent)) return {found: true, via: 'svg title'};
  }
  if (t(el.getAttribute('title'))) return {found: true, via: 'title'};
  return {found: true, via: null, placeholder: !!el.getAttribute('placeholder')};
}"""


def _plain_css(sel: str) -> bool:
    return not re.search(r":(has-text|text-is|text|has|nth-match|visible)\b|^(text|role|xpath)=", sel)


class Executor:
    def __init__(self, page, context, observer, guard, selectors, base_url, env_kind, limits, notes):
        self.page, self.context, self.obs, self.guard = page, context, observer, guard
        self.selectors, self.base_url, self.env_kind = selectors, base_url.rstrip("/"), env_kind
        self.step_timeout = int(limits.get("step_timeout_ms", 15000))
        self.nav_timeout = int(limits.get("navigation_timeout_ms", 30000))
        self.login_wait = int(limits.get("login_wait_ms", 20000))
        self.notes = notes
        self.tab_stops: list = []
        self.base_origin = f"{urlsplit(base_url).scheme}://{urlsplit(base_url).netloc}"

    # ------------------------------------------------------------------ helpers
    def loc(self, name):
        return self.page.locator(self.selectors[name]).first

    def _timeout(self, step, default=None):
        return int(step.get("timeout_ms", default or self.step_timeout))

    def _poll(self, check, timeout_ms, interval=0.1):
        """Return the last value of check() once it is truthy, else None after the timeout."""
        end = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                val = check()
            except PWError:
                val = None            # page is navigating; try again
            if val:
                return val
            if time.monotonic() >= end:
                return None
            # NOT time.sleep(): Playwright's sync API only delivers events (request/response/...) while we call into it.
            self.page.wait_for_timeout(int(interval * 1000))

    def _path(self):
        return urlsplit(self.page.url).path or "/"

    # ------------------------------------------------------------------ dispatcher
    def run(self, step: dict) -> str:
        kinds = step.get("only_env_kinds")
        if kinds and self.env_kind not in kinds:
            return f"skipped on {self.env_kind} (only for {', '.join(kinds)})"
        return getattr(self, "do_" + step["action"])(step) or ""

    # ------------------------------------------------------------------ actions
    def do_goto(self, s):
        q = s.get("query") or {}
        parts = []
        for k, v in q.items():
            v = str(v)
            parts.append(f"{quote(str(k), safe='')}={v[4:] if v.startswith('raw:') else quote(v, safe='')}")
        url = self.base_url + s["path"] + ("?" + "&".join(parts) if parts else "")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=self.nav_timeout)
        except PWError as e:
            msg = str(e).split("\n")[0]
            if "net::" in msg or "NS_ERROR" in msg or "Timeout" in msg:
                raise Blocked(f"could not open {self.base_origin}{s['path']}: {msg[:160]}")
            raise
        # Server-rendered React pages show their buttons before the JS that wires them up has loaded; clicking too early
        # does nothing. Best effort: wait for the network to go quiet (never fails the test if it does not).
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except PWTimeout:
            pass
        return f"opened {s['path']}" + (" with a ?redirect= payload" if q else "")

    def do_wait(self, s):
        self.page.wait_for_timeout(min(int(s["ms"]), 5000))

    def do_fill(self, s):
        self._act(lambda: self.loc(s["target"]).fill(s["value"], timeout=self.step_timeout), s)
        return f"typed into {s['target']}"

    def do_clear(self, s):
        self._act(lambda: self.loc(s["target"]).fill("", timeout=self.step_timeout), s)
        return f"cleared {s['target']}"

    def do_click(self, s):
        self._act(lambda: self.loc(s["target"]).click(timeout=self.step_timeout), s)
        return f"clicked {s['target']}"

    def _act(self, fn, s):
        try:
            fn()
        except PWTimeout:
            raise StepFailed(f"'{s['target']}' was not found or not usable within {self.step_timeout} ms")

    def do_expect_visible(self, s):
        t = self._timeout(s)
        try:
            self.loc(s["target"]).wait_for(state="visible", timeout=t)
        except PWTimeout:
            raise StepFailed(f"'{s['target']}' is not visible after {t} ms")
        return f"{s['target']} visible"

    def do_expect_hidden(self, s):
        t = self._timeout(s)
        try:
            self.loc(s["target"]).wait_for(state="hidden", timeout=t)
        except PWTimeout:
            raise StepFailed(f"'{s['target']}' is still visible after {t} ms")
        return f"{s['target']} hidden"

    def do_expect_disabled(self, s):
        ok = self._poll(lambda: self.loc(s["target"]).is_disabled(timeout=500), self._timeout(s, 5000))
        if not ok:
            raise StepFailed(f"'{s['target']}' is enabled but should be disabled")
        return f"{s['target']} disabled"

    def do_expect_attr(self, s):
        got = {}

        def chk():
            got["v"] = self.loc(s["target"]).get_attribute(s["attr"], timeout=500)
            return got["v"] == str(s["equals"])
        if not self._poll(chk, self._timeout(s, 5000)):
            raise StepFailed(f"attribute {s['attr']} of '{s['target']}' is {got.get('v')!r}, expected {s['equals']!r}")
        return f"{s['target']}[{s['attr']}]={s['equals']}"

    def do_expect_value(self, s):
        got = {}

        def chk():
            got["v"] = self.loc(s["target"]).input_value(timeout=500)
            return got["v"] == s["equals"]
        if not self._poll(chk, self._timeout(s, 5000)):
            # never echo the values (the field may hold a secret)
            raise StepFailed(f"value of '{s['target']}' differs from the expected value (length {len(got.get('v') or '')} vs {len(s['equals'])})")
        return f"{s['target']} holds the typed value"

    def do_expect_invalid_field(self, s):
        ok = self._poll(lambda: self.loc(s["target"]).evaluate("el => !el.validity.valid"), self._timeout(s, 5000))
        if not ok:
            raise StepFailed(f"browser validation did not flag '{s['target']}' as invalid")
        return f"browser validation flagged {s['target']}"

    def do_expect_url(self, s):
        t = self._timeout(s, self.login_wait if "path_startswith" in s and s.get("same_origin") else 8000)
        why = {}

        def chk():
            u = urlsplit(self.page.url)
            path = u.path or "/"
            origin = f"{u.scheme}://{u.netloc}"
            if s.get("same_origin") and origin != self.base_origin:
                why["m"] = f"left the site (now on another origin, path {path})"
                return False
            if "path_equals" in s and path.rstrip("/") != s["path_equals"].rstrip("/"):
                why["m"] = f"path is {path}, expected {s['path_equals']}"
                return False
            if "path_startswith" in s and not path.startswith(s["path_startswith"]):
                why["m"] = f"path is {path}, expected it to start with {s['path_startswith']}"
                return False
            if "path_not_startswith" in s and path.startswith(s["path_not_startswith"]):
                why["m"] = f"path {path} must not start with {s['path_not_startswith']}"
                return False
            return True
        if not self._poll(chk, t):
            raise StepFailed(why.get("m", "URL check failed"))
        return f"URL path {self._path()}"

    def do_expect_text(self, s):
        sel = s.get("target")
        t = self._timeout(s, 8000)
        if s.get("matches"):
            rx = re.compile(s["matches"], re.I)
            got = {}

            def chk():
                got["t"] = self.loc(sel).inner_text(timeout=500) if sel else self.page.inner_text("body", timeout=500)
                return rx.search(got["t"] or "")
            if not self._poll(chk, t):
                raise StepFailed(f"no text matching the expected pattern in '{sel or 'page'}' (saw: {(got.get('t') or '')[:120]!r})")
        if s.get("not_matches"):
            rx = re.compile(s["not_matches"], re.I)
            try:
                text = self.loc(sel).inner_text(timeout=1000) if sel else self.page.inner_text("body", timeout=1000)
            except PWError:
                text = ""
            if rx.search(text or ""):
                raise StepFailed(f"'{sel or 'page'}' shows text that must not appear (saw: {text[:120]!r})")
        return f"text check on {sel or 'page'} ok"

    def do_expect_feedback(self, s):
        def chk():
            if self.page.locator(self.selectors["error_box"]).first.is_visible(timeout=300):
                return "an error message is shown"
            n = self.page.evaluate("() => document.querySelectorAll('input:invalid').length")
            return f"browser validation flagged {n} field(s)" if n else None
        got = self._poll(chk, self._timeout(s, 8000))
        if not got:
            raise StepFailed("the user got no feedback (no error message and no browser validation message)")
        return got

    def do_expect_login_request(self, s):
        att = self.obs.login_attempts
        if s.get("made") is False:
            self.page.wait_for_timeout(500)
            if att:
                raise StepFailed("a login request was sent but none was expected")
            return "no login request was sent"
        wanted = s.get("status_in")
        got = {}

        def chk():
            sent = [a for a in att if not a["blocked"]]
            got["codes"] = [a["status"] for a in sent]
            if not sent:
                return False
            return any(a["status"] is not None and (not wanted or a["status"] in wanted) for a in sent)
        if not self._poll(chk, self._timeout(s, 10000)):
            if att and all(a["blocked"] for a in att):
                if self.guard.blocked:
                    raise StepFailed("the login request was blocked by the agent's safety guard: " + "; ".join(self.guard.blocked[-2:]))
                err = next((a.get("error") for a in att if a.get("error")), "")
                raise StepFailed("the login request was allowed by the agent's guard but failed in the browser/network"
                                 + (f" ({err})" if err else "")
                                 + ". Typical causes: the API does not allow this site's origin (CORS), the API address is wrong or the tunnel is down.")
            if not att:
                raise StepFailed("no login request was sent")
            raise StepFailed(f"login request answered HTTP {got.get('codes')}, expected one of {wanted}")
        code = next(a["status"] for a in att if a["status"] is not None)
        return f"login request answered HTTP {code}"

    def _has_session(self):
        if self.page.evaluate(_STORAGE_JS):
            return True
        return any(re.search(r"session|token|auth", c["name"], re.I) for c in self.context.cookies())

    def do_expect_session(self, s):
        if not self._poll(self._has_session, self._timeout(s, self.login_wait)):
            raise StepFailed("no session was stored after login")
        return "a session is stored (value not read)"

    def do_expect_no_session(self, s):
        self.page.wait_for_timeout(300)
        if self._has_session():
            raise StepFailed("a session/token is stored although the user must not be signed in")
        return "no session stored"

    def do_expect_no_request_to(self, s):
        marker = s["host_contains"].lower()
        hit = [h for h in self.obs.hosts if marker in h]
        if hit:
            raise StepFailed("the page sent a request to the external host used in the redirect payload")
        return "no request to the external host"

    def do_expect_no_writes(self, s):
        if self.obs.write_attempts:
            raise StepFailed(f"the page tried to send {len(self.obs.write_attempts)} write request(s): " + "; ".join(self.obs.write_attempts[:3]))
        return "no write request attempted"

    # ---- keyboard / accessibility
    def do_tab_through(self, s):
        self.tab_stops = []
        self.page.evaluate("() => { if (document.activeElement) document.activeElement.blur(); }")
        cmap = {n: sel for n, sel in self.selectors.items() if _plain_css(sel)}
        for _ in range(int(s.get("max_tabs", 14))):
            self.page.keyboard.press("Tab")
            info = self.page.evaluate(_FOCUS_JS, cmap)
            self.tab_stops.append(info)
            if info["name"] == "login_button":
                break
        return f"pressed Tab {len(self.tab_stops)} time(s)"

    def do_expect_tab_order(self, s):
        seen = [x["name"] for x in self.tab_stops if x["name"]]
        missing = [n for n in s["order"] if n not in seen]
        if missing:
            raise StepFailed(f"Tab never reached: {', '.join(missing)} (reached in order: {', '.join(seen) or 'nothing'})")
        positions = [seen.index(n) for n in s["order"]]
        if positions != sorted(positions):
            raise StepFailed(f"Tab order is {', '.join(seen)}; expected {', '.join(s['order'])}")
        return "focus order: " + " > ".join(seen)

    def do_expect_focus_visible(self, s):
        by = {x["name"]: x["focus_visible"] for x in self.tab_stops if x["name"]}
        bad = [n for n in s["targets"] if n in by and not by[n]]
        absent = [n for n in s["targets"] if n not in by]
        if absent:
            raise StepFailed(f"never focused with Tab, so the focus indicator could not be checked: {', '.join(absent)}")
        if bad:
            raise StepFailed(f"no visible focus indicator on: {', '.join(bad)}")
        return "visible focus indicator on every control"

    def do_expect_accessible_name(self, s):
        bad = []
        for n in s["targets"]:
            r = self.page.evaluate(_NAME_JS, self.selectors[n]) if _plain_css(self.selectors[n]) else {"found": True, "via": "n/a"}
            if not r["found"]:
                bad.append(f"{n} (element not found)")
            elif r["via"] is None:
                bad.append(f"{n} (no label/aria-label" + ("; placeholder only" if r.get("placeholder") else "; icon only") + ")")
        if bad:
            raise StepFailed("no programmatic accessible name for: " + "; ".join(bad))
        return "all controls have an accessible name"
