"""Phase 1: environment & website health checks (no login)."""
from urllib.parse import urlparse

import pytest

from config import APPS, SLOW_LOAD_SECONDS
from conftest import APP_NAMES
from pages.login_page import PASS_SEL
from utils.monitor import PageMonitor
from utils.recorder import REC, Blocked, NotVerified

T = "01_health"


@pytest.mark.parametrize("app", APP_NAMES)
def test_app_health(app, new_context):
    cfg = APPS[app]
    page = new_context().new_page()
    mon = PageMonitor(page)
    state = {}

    with REC.step(T, f"{app}: login URL responds", "anonymous", "HTTP status < 400", page, mon, "High", shot=True) as s:
        resp = page.goto(cfg.login_url, wait_until="load", timeout=60000)
        s.actual = f"HTTP {resp.status if resp else None}"
        assert resp is not None and resp.status < 400, s.actual
        try:
            page.wait_for_load_state("networkidle", timeout=8000)   # let the SPA settle; NOT part of the timing below
        except Exception:
            pass

    with REC.step(T, f"{app}: first load is not slow", "anonymous",
                  f"Browser 'load' event within {SLOW_LOAD_SECONDS:g} s (single sample, extra waiting excluded)", page, mon, "Medium") as s:
        nav = page.evaluate("""() => { const n = performance.getEntriesByType('navigation')[0];
            return n ? {ttfb: n.responseStart, dcl: n.domContentLoadedEventEnd, load: n.loadEventEnd} : null }""")
        if not nav or not nav["load"]:
            raise NotVerified("Browser did not report navigation timing")
        s.actual = f"first byte {nav['ttfb']/1000:.2f}s, DOM ready {nav['dcl']/1000:.2f}s, load {nav['load']/1000:.2f}s"
        s.confidence = "Low"      # one sample from one network - repeat before calling it a performance bug
        assert nav["load"] / 1000 <= SLOW_LOAD_SECONDS, "Slow first load (single sample - repeat to confirm)"

    with REC.step(T, f"{app}: page renders visible content", "anonymous", "Body has visible text", page, mon, "High") as s:
        n = len(page.inner_text("body").strip())
        s.actual = f"{n} characters of visible text"
        assert n > 0, "Blank page"

    with REC.step(T, f"{app}: login form present", "anonymous", "A password field is visible", page, mon, "High") as s:
        if not page.locator(PASS_SEL).first.is_visible():
            raise Blocked(f"No password field at {cfg.login_url}; set {app.upper()}_LOGIN_PATH in .env")
        s.actual = "Password field visible"

    with REC.step(T, f"{app}: no console errors / failed requests on load", "anonymous",
                  "0 console errors and 0 failed requests from the app's own server", page, mon, "Medium") as s:
        own = urlparse(cfg.base).netloc
        first_party = [f for f in mon.failed if own in f]
        third_party = [f for f in mon.failed if own not in f]
        s.actual = (f"{len(mon.console)} console errors, {len(first_party)} first-party failed requests, "
                    f"{len(third_party)} third-party failed requests {third_party[:2]}")
        assert not mon.console and not first_party, s.actual
        if third_party:
            # e.g. Google Fonts blocked. Could be the app, your network/browser, or the test environment.
            raise NotVerified(s.actual + " - re-test in a normal browser on another network before reporting")

    with REC.step(T, f"{app}: served over HTTPS", "anonymous",
                  "Sensitive app uses https:// (credentials/patient data encrypted in transit)", page, mon, "High") as s:
        final = page.url
        s.actual = f"Configured scheme={cfg.base.split(':')[0]}, final URL scheme={final.split(':')[0]}"
        s.confidence = "High"
        assert final.lower().startswith("https://"), "Site is served over plain HTTP - credentials and data are not encrypted"
    page.context.close()
