"""Network safety for the login agent. Enforced inside the browser context, whatever the YAML says.

  * GET / HEAD / OPTIONS are always allowed.
  * Every other request is ABORTED, with exactly one exception: the login request (POST .../auth/login) of a test marked
    `login_post: true`, on a staging/local environment, to an allowlisted origin, while the per-run budget lasts.
  * On production nothing but safe methods ever leaves the browser.
"""
import re
from urllib.parse import urlsplit

from .safety import SAFE_METHODS, normalize_origin

LOGIN_POST_PATH = re.compile(r"^/(?:api/)?(?:v\d+/)?auth/login/?$", re.I)


class PostBudget:
    """Shared by all tests of a run: the maximum number of real login requests."""

    def __init__(self, limit: int):
        self.limit = int(limit)
        self.used = 0

    @property
    def left(self) -> int:
        return self.limit - self.used


class NetworkGuard:
    def __init__(self, env_kind: str, login_origins, budget: PostBudget):
        self.env_kind = env_kind
        self.login_origins = set(login_origins)
        self.budget = budget
        self.login_post_allowed = False          # switched on per test
        self.blocked: list = []                  # "METHOD origin/path -> reason"  (no query string, no body, no headers)

    def decide(self, method: str, url: str):
        method = method.upper()
        if method in SAFE_METHODS:
            return True, "safe method"
        try:
            origin = normalize_origin(url)
        except Exception:
            return False, "unparsable URL"
        path = urlsplit(url).path or "/"
        if self.env_kind not in ("staging", "local"):
            return False, "production is read-only"
        if not self.login_post_allowed:
            return False, "this test may not send writes"
        if method != "POST" or not LOGIN_POST_PATH.match(path):
            return False, "only the login request (POST) may be sent"
        if origin not in self.login_origins:
            return False, f"origin {origin} is not an allowed login origin (add it to auth_origins in config/environments.yml)"
        if self.budget.left <= 0:
            return False, f"login budget of {self.budget.limit} requests per run is used up"
        return True, "login request"

    def handler(self, route):
        req = route.request
        ok, why = self.decide(req.method, req.url)
        if ok:
            if why == "login request":
                self.budget.used += 1
            route.continue_()
        else:
            u = urlsplit(req.url)
            self.blocked.append(f"{req.method} {u.scheme}://{u.netloc}{u.path[:80]} -> {why}")
            route.abort("blockedbyclient")

    def attach(self, context):
        context.route("**/*", self.handler)
        return self


class Observer:
    """Records what the page did, without bodies, headers or query strings."""

    def __init__(self, page):
        self.write_attempts: list = []     # non-safe requests the page tried to send (allowed or blocked)
        self.hosts: set = set()            # every host any request was addressed to
        self.login_attempts: list = []     # {"status": int|None, "blocked": bool}
        self.edge_beacons = 0
        self.console_errors = 0
        self.page_errors = 0
        self._by_request = {}
        page.on("request", self._on_request)
        page.on("response", self._on_response)
        page.on("requestfailed", self._on_failed)
        page.on("console", lambda m: self._count_console(m))
        page.on("pageerror", lambda e: setattr(self, "page_errors", self.page_errors + 1))

    def _count_console(self, msg):
        if msg.type == "error":
            self.console_errors += 1

    def _on_request(self, req):
        u = urlsplit(req.url)
        if u.hostname:
            self.hosts.add(u.hostname.lower())
        if req.method.upper() not in SAFE_METHODS and (u.path or "").startswith("/cdn-cgi/"):
            self.edge_beacons += 1      # Cloudflare's own analytics beacon (e.g. /cdn-cgi/rum): not the app writing; still blocked by the guard
        elif req.method.upper() not in SAFE_METHODS:
            self.write_attempts.append(f"{req.method} {u.scheme}://{u.netloc}{u.path[:80]}")
            if LOGIN_POST_PATH.match(u.path or "/"):
                rec = {"status": None, "blocked": False}
                self.login_attempts.append(rec)
                self._by_request[id(req)] = rec

    def _on_response(self, resp):
        rec = self._by_request.get(id(resp.request))
        if rec is not None:
            rec["status"] = resp.status

    def _on_failed(self, req):
        rec = self._by_request.get(id(req))
        if rec is not None and rec["status"] is None:
            rec["blocked"] = True
