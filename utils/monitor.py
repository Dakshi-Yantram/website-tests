"""Collects browser console errors and failed network requests for a page."""
import re


_IGNORE = re.compile(
    r"(favicon|google-analytics|googletagmanager|doubleclick|"
    r"facebook\.net|hotjar|clarity\.ms|"
    r"fonts\.googleapis\.com/css2\?)",
    re.I,
)



class PageMonitor:
    def __init__(self, page):
        self.console: list[str] = []
        self.failed: list[str] = []
        page.on("console", self._on_console)
        page.on("pageerror", lambda e: self.console.append(f"[pageerror] {str(e)[:300]}"))
        page.on("requestfailed", self._on_failed)
        page.on("response", self._on_response)

    def _on_console(self, msg):
        if msg.type == "error":
            loc = (msg.location or {}).get("url", "")
            if not _IGNORE.search(loc):
                self.console.append(f"[console.error] {msg.text[:300]} @ {loc[:120]}")

    def _on_failed(self, req):
        reason = req.failure or ""
        if "ERR_ABORTED" in reason or "BLOCKED_BY_CLIENT" in reason or _IGNORE.search(req.url):
            return
        self.failed.append(f"FAILED {req.method} {req.url[:200]} ({reason})")

    def _on_response(self, resp):
        if resp.status >= 400 and not _IGNORE.search(resp.url):
            self.failed.append(f"{resp.status} {resp.request.method} {resp.url[:200]}")

    def clear(self):
        self.console.clear()
        self.failed.clear()
