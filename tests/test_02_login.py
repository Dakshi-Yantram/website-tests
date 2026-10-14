"""Phase 2: login for every role + safe negative login tests.

Statuses used here:
  PASS          - the expected behaviour was observed.
  FAIL          - the expected behaviour was NOT observed and we are confident it is the app (see confidence).
  BLOCKED       - we could not run the check (missing credentials, unknown login route).
  NOT VERIFIED  - the check ran but the evidence is not strong enough to call it a bug.
"""
import pytest

from config import APPS, ROLES, RUN_ID
from conftest import ROLE_NAMES, APP_NAMES
from pages.login_page import LoginPage, MIN_CONTENT_CHARS
from utils.monitor import PageMonitor
from utils.recorder import REC, Blocked

T = "02_login"
REJECT_CODES = (400, 401, 403, 404, 422, 429)


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_valid_login(role, sessions):
    s = sessions(role)
    if s.blocked and s.config_problem:
        REC.add(T, "Valid credentials log in", role, "Role logs in", "BLOCKED", s.blocked)
        pytest.skip(s.blocked)
    cfg = ROLES[role]
    page, mon = s.page, s.mon
    res = s.login_result
    lp = LoginPage(page, APPS[cfg.app].login_url, cfg.landing_prefix)

    with REC.step(T, "Valid credentials log in", role, "Login form disappears and the app's logged-in page renders",
                  page, mon, "Critical", shot=True) as st:
        st.actual = (f"login_ok={s.login_ok} in {s.login_secs}s; reason={getattr(res, 'reason', '?')}; "
                     f"HTTP {getattr(res, 'api_status', 0) or 'n/a'}; final_url={s.landing_url}; message='{s.login_error}'")
        st.confidence = "High" if getattr(res, "api_status", 0) in REJECT_CODES else "Medium"
        assert s.login_ok, s.login_error or "Login did not complete"

    if s.login_ok:
        if cfg.landing_prefix:
            with REC.step(T, f"Lands in the {role} area ({cfg.landing_prefix})", role,
                          f"URL path starts with {cfg.landing_prefix}", page, mon, "Medium") as st:
                st.actual = f"Landed on {page.url}"
                assert cfg.landing_prefix.rstrip("/") in page.url, "Landed somewhere else"
        with REC.step(T, "Landing page shows content (waits for the SPA to render)", role,
                      f"At least {MIN_CONTENT_CHARS} visible characters within 10 s", page, mon, "High") as st:
            n = lp.wait_for_content(10000)
            st.actual = f"Landed on {page.url}; title='{page.title()}'; {n} visible chars"
            st.confidence = "Medium"
            assert n >= MIN_CONTENT_CHARS, f"Page still has only {n} visible characters after 10 s"
            assert not lp.form_visible(), "Login form still visible"
        with REC.step(T, "Session survives a page reload", role, "Still authenticated after reload", page, mon, "High") as st:
            page.reload(wait_until="domcontentloaded")
            n = lp.wait_for_content(10000)
            still = not lp.form_visible() and n >= MIN_CONTENT_CHARS
            st.actual = f"Authenticated after reload: {still} ({n} chars, url={page.url})"
            assert still, "Redirected to login or page did not render after reload"


def _feedback_steps(st, res, what):
    """Shared checks for 'did the user get told something?'"""
    st.screenshot = res.screenshot
    st.actual = (f"{what}: HTTP {res.api_status or 'n/a'}; logged_in={res.ok}; "
                 f"new visible text={res.feedback[:3] or 'none'}; invalid fields flagged={res.invalid_fields}")


@pytest.mark.parametrize("app", APP_NAMES)
def test_negative_login(app, new_context):
    """Uses a NON-EXISTENT account so a real account can never be locked out."""
    cfg = APPS[app]
    page = new_context().new_page()
    mon = PageMonitor(page)
    lp = LoginPage(page, cfg.login_url)
    fake = f"autotest_invalid_{RUN_ID}@example.com"
    state = {}

    with REC.step(T, f"{app}: invalid credentials are rejected", "anonymous",
                  "User is NOT logged in and the server refuses the request", page, mon, "Critical") as st:
        lp.open()
        res = state["bad"] = lp.submit(fake, "Wrong-Password-123!")
        _feedback_steps(st, res, "invalid login")
        assert not res.ok, "Invalid credentials were accepted!"
        assert res.api_status in REJECT_CODES or res.api_status == 0, f"Unexpected server answer {res.api_status}"

    res = state.get("bad")
    if res is not None:
        with REC.step(T, f"{app}: invalid login shows a visible error message", "anonymous",
                      "A visible message (toast / inline text) tells the user the login failed", page, mon, "Medium") as st:
            _feedback_steps(st, res, "invalid login")
            st.confidence = "Medium"      # a human should still glance at the screenshot
            assert res.feedback or res.invalid_fields, (
                f"No new visible text appeared within 3 s after the server answered (HTTP {res.api_status}). "
                "Open the screenshot to confirm before logging this as a defect.")

    with REC.step(T, f"{app}: empty form does not log in", "anonymous",
                  "Nothing is submitted successfully; user stays on the login page", page, mon, "High") as st:
        lp.open()
        res = state["empty"] = lp.submit("", "", empty_ok=True)
        _feedback_steps(st, res, "empty submit")
        assert not res.ok, "Empty form logged in!"

    res = state.get("empty")
    if res is not None:
        with REC.step(T, f"{app}: empty form tells the user what is missing", "anonymous",
                      "Browser validation or a visible message appears", page, mon, "Low") as st:
            _feedback_steps(st, res, "empty submit")
            st.confidence = "Medium"
            note = " (the server rejected it with HTTP %s, so there is no client-side validation)" % res.api_status if res.api_status else ""
            assert res.feedback or res.invalid_fields, "No validation feedback seen" + note
    page.context.close()
