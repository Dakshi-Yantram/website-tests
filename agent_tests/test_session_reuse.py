"""One login per (origin, role) per run, with fallback and HTTP 429 handling. Real Chromium against the LOCAL synthetic server."""
import pytest

from agent_tests.conftest import wf
from agent_tests.test_runner_e2e import ctx, by_id  # noqa: F401  (fixture reuse)
from qa_agent.runner import Runner

SECRET_COOKIE = "sess-VALUE-do-not-leak-123"


def counting_login(calls, fail_with=None):
    def login(page, role, base):
        calls.append(role)
        if fail_with:
            raise AssertionError(fail_with(role))
        page.context.add_cookies([{"name": "sid", "value": SECRET_COOKIE, "url": base}])
        page.goto(base + "/dashboard")
    return login


def two(role="nurse", n=3):
    return [wf(f"reuse_{role}_{i}", role=role, steps=[{"action": "goto", "path": "/visits"}]) for i in range(n)]


def step0(results, wid):
    return by_id(results)[wid].steps[0]


def test_three_workflows_same_role_log_in_once(ctx):
    calls = []
    r, res = ctx.run(two(n=3), login_fn=counting_login(calls))
    assert calls == ["nurse"]
    assert [x.status for x in res] == ["PASS"] * 3
    assert "session reused" in step0(res, "reuse_nurse_1").actual
    assert step0(res, "reuse_nurse_0").actual == "Login completed"


def test_each_role_gets_its_own_login(ctx):
    calls = []
    wfs = two("nurse", 2) + two("consumer", 2)
    r, res = ctx.run(wfs, login_fn=counting_login(calls))
    assert sorted(calls) == ["consumer", "nurse"] and all(x.status == "PASS" for x in res)


def test_reuse_can_be_disabled(ctx):
    calls = []
    r, res = ctx.run(two(n=3), login_fn=counting_login(calls), reuse_sessions=False)
    assert calls == ["nurse"] * 3 and r._sessions == {}


def test_stored_state_carries_cookie_and_is_passed_to_next_context(ctx, monkeypatch):
    seen = []
    orig = Runner._new_context

    def spy(self, browser, state=None):
        seen.append(state)
        return orig(self, browser, state)
    monkeypatch.setattr(Runner, "_new_context", spy)
    calls = []
    r, res = ctx.run(two(n=2), login_fn=counting_login(calls))
    assert seen[0] is None and seen[1] is not None
    assert any(c["name"] == "sid" and c["value"] == SECRET_COOKIE for c in seen[1]["cookies"])


def test_rejected_session_falls_back_to_real_login(ctx, monkeypatch):
    # make every "landing page" look like a login page, as if the stored session had expired
    monkeypatch.setattr(Runner, "_landing_url", staticmethod(lambda role, base: base + "/login"))
    calls = []
    r, res = ctx.run(two(n=3), login_fn=counting_login(calls))
    assert calls == ["nurse"] * 3 and all(x.status == "PASS" for x in res)
    assert "session reused" not in step0(res, "reuse_nurse_1").actual


def test_session_state_is_never_written_to_disk_or_reports(ctx):
    calls = []
    r, res = ctx.run(two(n=2), login_fn=counting_login(calls))
    from qa_agent import reporting
    reporting.write_reports(res, r.meta, ctx.tmp / "artifacts" / "reports")
    leaked = [p for p in (ctx.tmp).rglob("*") if p.is_file() and SECRET_COOKIE.encode() in p.read_bytes()]
    assert leaked == []


def test_http_429_is_blocked_not_failed_and_stops_further_attempts(ctx):
    calls = []
    login = counting_login(calls, fail_with=lambda role: f"Login failed for role {role}: Too many login attempts. (HTTP 429)")
    r, res = ctx.run(two(n=4), login_fn=login)
    assert calls == ["nurse"]                                         # one attempt only
    assert [x.status for x in res] == ["BLOCKED"] * 4
    first, later = step0(res, "reuse_nurse_0"), step0(res, "reuse_nurse_1")
    assert "rate-limited" in first.error and "not an application failure" in first.error
    assert "skipped" in later.error
    assert all(s.status == "NOT TESTED" for s in by_id(res)["reuse_nurse_0"].steps[1:])


def test_http_429_for_one_role_does_not_block_another_role(ctx):
    calls = []

    def login(page, role, base):
        calls.append(role)
        if role == "nurse":
            raise AssertionError("Login failed for role nurse: Too many login attempts. (HTTP 429)")
        page.goto(base + "/dashboard")
    r, res = ctx.run(two("nurse", 2) + two("consumer", 2), login_fn=login)
    st = {x.id: x.status for x in res}
    assert st["reuse_nurse_0"] == st["reuse_nurse_1"] == "BLOCKED"
    assert st["reuse_consumer_0"] == st["reuse_consumer_1"] == "PASS"
    assert calls.count("nurse") == 1 and calls.count("consumer") == 1


def test_other_login_failures_are_still_FAIL_and_are_retried(ctx):
    calls = []
    login = counting_login(calls, fail_with=lambda role: f"Login failed for role {role}: Invalid credentials (HTTP 401)")
    r, res = ctx.run(two(n=2), login_fn=login)
    assert [x.status for x in res] == ["FAIL", "FAIL"] and calls == ["nurse", "nurse"]
    assert r._sessions == {}                                          # a failed login is never cached


def test_http_500_login_is_reported_once_then_blocked_not_retried(ctx):
    """A server-side 500 on login must not be retried for every workflow (that ends in a 429 lockout)."""
    calls = []
    login = counting_login(calls, fail_with=lambda role: f"Login failed for role {role}: Unable to complete login (HTTP 500)")
    r, res = ctx.run(two(n=3), login_fn=login)
    assert [x.status for x in res] == ["FAIL", "BLOCKED", "BLOCKED"]
    assert calls == ["nurse"]                                         # only ONE real login attempt for the role