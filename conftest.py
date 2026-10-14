"""Shared fixtures: one browser, one independent logged-in context per role, cached inventories."""
import time
from dataclasses import dataclass
from typing import Any

import pytest
from playwright.sync_api import sync_playwright

from config import APPS, ROLES, HEADLESS, SLOW_MO, NAV_TIMEOUT, IGNORE_HTTPS_ERRORS
from pages.login_page import LoginPage
from pages.discovery import crawl
from utils.monitor import PageMonitor
from utils.recorder import REC, Blocked
from utils.report import write_reports

ROLE_NAMES = list(ROLES)
APP_NAMES = list(APPS)


@dataclass
class RoleSession:
    name: str
    context: Any = None
    page: Any = None
    mon: Any = None
    blocked: str = ""
    login_ok: bool = False
    login_secs: float = 0.0
    login_error: str = ""
    landing_url: str = ""
    config_problem: bool = False     # True = we could not even try (missing creds / wrong login route): BLOCKED, not FAIL
    login_result: Any = None         # full LoginResult (status code, feedback text, screenshot ...)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        yield b
        b.close()


@pytest.fixture(scope="session")
def new_context(browser):
    made = []

    def _new():
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=IGNORE_HTTPS_ERRORS)
        ctx.set_default_timeout(NAV_TIMEOUT)
        made.append(ctx)
        return ctx

    yield _new
    for c in made:
        try:
            c.close()
        except Exception:
            pass


@pytest.fixture(scope="session")
def sessions(new_context):
    cache: dict[str, RoleSession] = {}

    def get(role_name: str) -> RoleSession:
        if role_name in cache:
            return cache[role_name]
        role = ROLES[role_name]
        s = RoleSession(role_name)
        cache[role_name] = s
        if not role.email or not role.password:
            s.blocked = f"Set {role.email_env} and {role.password_env} in .env"
            s.config_problem = True
            return s
        s.context = new_context()
        s.page = s.context.new_page()
        s.mon = PageMonitor(s.page)
        lp = LoginPage(s.page, APPS[role.app].login_url, role.landing_prefix)
        t0 = time.time()
        try:
            lp.open()
            res = lp.submit(role.email, role.password)
            s.login_result = res
            s.login_ok = res.ok
            s.login_error = "" if res.ok else (res.error or f"login did not complete ({res.reason}, HTTP {res.api_status or 'n/a'})")
        except Blocked as e:        # wrong/unknown login route: a configuration problem, not an application bug
            s.login_error = str(e)
            s.config_problem = True
        except Exception as e:
            s.login_error = f"{type(e).__name__}: {str(e)[:200]}"
        s.login_secs = round(time.time() - t0, 2)
        s.landing_url = s.page.url
        s.mon.clear()
        if not s.login_ok:
            s.blocked = f"Login failed: {s.login_error}"
        return s

    return get


@pytest.fixture(scope="session")
def inventories(sessions):
    cache: dict[str, list] = {}

    def get(role_name: str) -> list:
        if role_name not in cache:
            s = sessions(role_name)
            cache[role_name] = [] if s.blocked else crawl(s.page, role_name, s.landing_url)
            if s.mon:
                s.mon.clear()
        return cache[role_name]

    return get


@pytest.fixture
def need_session(sessions):
    """Return a usable session or skip (and record BLOCKED) when the role cannot log in."""
    def get(role_name: str, test: str):
        s = sessions(role_name)
        if s.blocked:
            REC.add(test, "Role session available", role_name, "Role can log in", "BLOCKED", s.blocked)
            pytest.skip(s.blocked)
        return s
    return get


def pytest_sessionfinish(session, exitstatus):
    html_path, json_path = write_reports(REC)
    print(f"\n\nHTML report: {html_path}\nJSON report: {json_path}\nSummary: {REC.summary()}")
    if REC.failures() and exitstatus == 0:
        session.exitstatus = 1
