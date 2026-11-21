"""The login agent must not depend on qa_agent, and its own safety modules must behave. Offline, no credentials."""
import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from playwright.sync_api import sync_playwright

from login_agent import report as report_mod
from login_agent.artifacts import safe_name, safe_screenshot
from login_agent.errors import SafetyBlocked, SensitiveDataError
from login_agent.redaction import (assert_clean, assert_clean_obj, clear_registered_secrets, find_secrets, redact, redact_obj,
                                   register_secret)
from login_agent.runner import TestResult, resolve_target
from login_agent.safety import SAFE_METHODS, get_environment, load_environments, normalize_origin, verify_environment

ROOT = Path(__file__).resolve().parent.parent
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijk123"


@pytest.fixture(autouse=True)
def _clean():
    clear_registered_secrets()
    yield
    clear_registered_secrets()


# ------------------------------------------------------------------------------------------------ imports
def test_login_agent_never_imports_qa_agent():
    for f in (ROOT / "login_agent").glob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not any(n == "qa_agent" or n.startswith("qa_agent.") for n in names), f"{f.name} imports {names}"


def test_cli_works_when_qa_agent_is_unavailable():
    """Simulates the broken repo: any import of qa_agent raises ModuleNotFoundError."""
    code = textwrap.dedent(f"""
        import sys, importlib.abc
        class Block(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path, target=None):
                if name == "qa_agent" or name.startswith("qa_agent."):
                    raise ModuleNotFoundError("No module named " + repr(name))
        sys.meta_path.insert(0, Block())
        sys.path.insert(0, {str(ROOT)!r})
        from login_agent import cli
        assert cli.main(["--list"]) == 0
        assert cli.main(["--dry-run"]) == 0
        assert "qa_agent" not in sys.modules
        print("OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-800:]
    assert "LOGIN-001" in r.stdout and "Dry run" in r.stdout


# ------------------------------------------------------------------------------------------------ redaction
@pytest.mark.parametrize("raw,secret", [
    (f"token {JWT}", JWT),
    ("Authorization: Bearer abcdef1234567890XYZ", "abcdef1234567890XYZ"),
    ("password=Hunter2-Secret!", "Hunter2-Secret!"),
    ('{"password": "Hunter2-Secret!", "x": 1}', "Hunter2-Secret!"),
    ("access_token: tok_live_9f8e7d6c5b4a", "tok_live_9f8e7d6c5b4a"),
    ("otp=482913", "482913"),
    ("Your OTP is 482913", "482913"),
    ("verification code: 771204", "771204"),
    ("Cookie: session=abc123def456; theme=dark", "abc123def456"),
    ("postgres://user:pw@db.internal:5432/app", "pw@db"),
])
def test_redact_masks_secrets(raw, secret):
    out = redact(raw)
    assert secret not in out and "[REDACTED" in out
    assert find_secrets(out) == []


def test_redact_masks_personal_data_and_keeps_normal_text():
    out = redact("login for consumer.qa@example.invalid phone +919000000001 failed with HTTP 401")
    assert "consumer.qa@" not in out and "9000000001" not in out and "HTTP 401" in out
    assert redact("Invalid email or password") == "Invalid email or password"
    assert "x***@" in redact("x@example.com", personal=True) and redact("a@b.co", personal=False) == "a@b.co"


def test_registered_and_environment_secrets_are_masked(monkeypatch):
    register_secret("Zq9-plain-literal")
    monkeypatch.setenv("NC_TEST_CONSUMER_PASSWORD", "Env-Pass-Value-77")
    monkeypatch.setenv("NC_TEST_CONSUMER_EMAIL", "keepme@example.com")          # name is not secret-looking: not registered
    out = redact("typed Zq9-plain-literal and Env-Pass-Value-77, mail 123")
    assert "Zq9-plain-literal" not in out and "Env-Pass-Value-77" not in out
    register_secret("ab")                                                       # too short to register
    assert "ab" in redact("ab")


def test_assert_clean_fails_closed():
    with pytest.raises(SensitiveDataError):
        assert_clean(f"leaked {JWT}")
    with pytest.raises(SensitiveDataError):
        assert_clean_obj({"a": ["otp: 123456"]})
    with pytest.raises(SensitiveDataError):
        assert_clean("mail me at someone@example.com")
    assert_clean(redact(f"leaked {JWT} otp=123456 someone@example.com"))         # redacted text passes
    assert redact_obj({"k": [f"{JWT}"]})["k"][0] == "[REDACTED]"


def test_report_never_contains_password_otp_or_token(tmp_path):
    register_secret("Zq9-Report-Pass")
    t = TestResult("LOGIN-099", "demo", "High", "anonymous", status="FAIL",
                   reason=f"saw password=Zq9-Report-Pass otp: 551234 {JWT} Bearer abcdef1234567890XYZ for me@example.com",
                   steps=[{"n": 1, "action": "fill", "status": "FAIL", "detail": "Zq9-Report-Pass and token=abcd1234efgh"}],
                   notes=["Cookie: session=abc123def456"])
    paths = report_mod.write_reports([t], {"suite": "s", "environment": "e", "env_kind": "local", "base_origin": "http://x",
                                           "run_id": "r", "started": "now", "agent_version": "t"}, tmp_path)
    blob = "".join(Path(p).read_text(encoding="utf-8") for p in paths.values())
    for leak in ("Zq9-Report-Pass", "551234", JWT, "abcdef1234567890XYZ", "me@example.com", "abc123def456", "abcd1234efgh"):
        assert leak not in blob, leak


def test_report_is_withheld_when_redaction_is_bypassed(tmp_path, monkeypatch):
    monkeypatch.setattr(report_mod, "redact_obj", lambda o, **k: o)              # simulate a redaction failure
    t = TestResult("LOGIN-099", "demo", "High", "anonymous", status="FAIL", reason=f"leak {JWT}")
    out = tmp_path / "withheld"
    with pytest.raises(SensitiveDataError):
        report_mod.write_reports([t], {"suite": "s", "environment": "e", "env_kind": "local", "base_origin": "http://x",
                                       "run_id": "r", "started": "now", "agent_version": "t"}, out)
    assert not out.exists() or not any(out.iterdir()), "nothing may be written when the check fails"


# ------------------------------------------------------------------------------------------------ safety / environments
def _envfile(tmp_path, envs, **top):
    p = tmp_path / "env.yml"
    p.write_text(yaml.safe_dump({"version": 1, **top, "environments": envs}))
    return p


PROD = {"kind": "production", "apps": {"nurseconnect": {"base_url": "https://prod.example.org"}}}
STG = {"kind": "staging", "allowed_origins": ["https://stg.example.net"], "auth_origins": ["https://api.stg.example.net"],
       "apps": {"nurseconnect": {"base_url": "https://stg.example.net"}}}


def test_normalize_origin_and_safe_methods():
    assert normalize_origin("HTTPS://Example.COM:443/a?b=1") == "https://example.com"
    assert normalize_origin("http://localhost:8080/x") == "http://localhost:8080"
    for bad in ("ftp://x.com", "javascript:alert(1)", "//x.com", ""):
        with pytest.raises(SafetyBlocked):
            normalize_origin(bad)
    assert SAFE_METHODS == {"GET", "HEAD", "OPTIONS"} and "POST" not in SAFE_METHODS


def test_environment_rules_fail_closed(tmp_path):
    ok = load_environments(_envfile(tmp_path, {"production": PROD, "staging": STG}))
    assert ok["production"].kind == "production" and ok["staging"].is_staging_like
    assert "https://prod.example.org" in ok["staging"].production_origins
    cases = [
        ({"staging": {**STG, "allowed_origins": ["https://prod.example.org"]}, "production": PROD}, "production origin"),
        ({"staging": {**STG, "allowed_origins": ["https://other.example.org", "https://prod.example.org:8443"]}, "production": PROD}, "production origin"),
        ({"staging": {**STG, "allowed_origins": ["http://stg.example.net"], "apps": {}}}, "plain http"),
        ({"staging": {**STG, "allowed_origins": []}}, "allowed_origins"),
        ({"staging": {**STG, "apps": {"nurseconnect": {"base_url": "https://elsewhere.example.net"}}}}, "not in allowed_origins"),
        ({"weird": {"kind": "demo"}}, "kind must be"),
    ]
    for envs, msg in cases:
        with pytest.raises(SafetyBlocked, match=msg):
            load_environments(_envfile(tmp_path, envs))
    with pytest.raises(SafetyBlocked, match="not found"):
        load_environments(tmp_path / "missing.yml")
    bad = tmp_path / "bad.yml"
    bad.write_text("environments: [unclosed")
    with pytest.raises(SafetyBlocked):
        load_environments(bad)
    with pytest.raises(SafetyBlocked, match="unknown environment"):
        get_environment("nope", _envfile(tmp_path, {"production": PROD}))


def test_resolve_target_refuses_production_origins_as_login_targets(tmp_path):
    f = _envfile(tmp_path, {"production": PROD, "staging": {**STG, "auth_origins": ["https://prod.example.org"]}})
    with pytest.raises(SafetyBlocked, match="production"):
        resolve_target("staging", f)
    with pytest.raises(SafetyBlocked):
        resolve_target("production", _envfile(tmp_path, {"production": PROD}), "https://not-prod.example.org")
    t = resolve_target("production", _envfile(tmp_path, {"production": PROD}))
    assert t.env_kind == "production" and t.login_origins == set(), "production gets no login-POST origin at all"


def test_verify_probe(tmp_path):
    env = get_environment("staging", _envfile(tmp_path, {"production": PROD, "staging": {**STG, "verify": {"path": "/qa.json", "json_key": "environment", "equals": "staging"}}}))
    verify_environment(env, "nurseconnect", fetch=lambda url: json.dumps({"environment": "staging"}))
    with pytest.raises(SafetyBlocked, match="different"):
        verify_environment(env, "nurseconnect", fetch=lambda url: json.dumps({"environment": "production"}))
    with pytest.raises(SafetyBlocked, match="could not be verified"):
        verify_environment(env, "nurseconnect", fetch=lambda url: (_ for _ in ()).throw(OSError("down")))


# ------------------------------------------------------------------------------------------------ artifacts
def test_safe_name():
    assert "/" not in safe_name("a/b\\c") and ".." not in safe_name("../../etc/passwd")
    assert safe_name("") == "file" and len(safe_name("x" * 200)) == 60 and safe_name("LOGIN-009 R1") == "LOGIN-009_R1"


def _in_clean_process(code: str) -> str:
    """Run Python WITHOUT the project's root conftest.py, which installs a process-wide Page.screenshot guard under pytest."""
    r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + textwrap.dedent(code)],
                       capture_output=True, text=True, cwd=str(ROOT), timeout=90)
    assert r.returncode == 0, r.stderr[-1500:]
    return r.stdout


def test_screenshot_modes_and_masking(tmp_path):
    out = _in_clean_process(f"""
        from pathlib import Path
        from PIL import Image
        from playwright.sync_api import sync_playwright
        from login_agent.artifacts import safe_screenshot
        t = Path({str(tmp_path)!r})
        html = "<body style='margin:0;background:#fff'><input value='TopSecret' style='width:300px;height:40px;font-size:20px;margin:10px'></body>"
        with sync_playwright() as pw:
            b = pw.chromium.launch(); page = b.new_page(viewport={{"width": 400, "height": 100}}); page.set_content(html)
            off = safe_screenshot(page, t / "off.png", "off")
            m = safe_screenshot(page, t / "masked.png", "masked")
            bogus = safe_screenshot(page, t / "bogus.png", "whatever")
            f = safe_screenshot(page, t / "full.png", "full")
            page.close()
            closed = safe_screenshot(page, t / "closed.png", "masked")
            b.close()
        px = lambda p: Image.open(p).convert("RGB").getpixel((150, 30))
        assert off == "" and not (t / "off.png").exists()
        assert closed == "", "a failed capture returns an empty path instead of raising"
        assert px(m) == (0x44, 0x44, 0x44), "masked: the input field must be painted over"
        assert px(bogus) == (0x44, 0x44, 0x44), "an unknown mode must fall back to masked"
        assert px(f) != (0x44, 0x44, 0x44), "full: unmasked"
        print("MASK-OK")
    """)
    assert "MASK-OK" in out


def test_cli_failure_screenshot_is_masked_and_report_is_clean(tmp_path):
    from agent_tests.login_mock import MockSite
    site = MockSite(users={"u@test.local": ("Zq9-Cli-Secret-1", "consumer")}, leaky_errors=True).start()
    try:
        envf = tmp_path / "env.yml"
        envf.write_text(yaml.safe_dump({"version": 1, "environments": {"local": {
            "kind": "local", "allowed_origins": [site.url], "apps": {"nurseconnect": {"base_url": site.url}}}}}))
        out = tmp_path / "out"
        code = f"""
            import os
            os.environ.update(NC_TEST_CONSUMER_EMAIL="u@test.local", NC_TEST_CONSUMER_PASSWORD="Zq9-Cli-Secret-1")
            from login_agent import cli
            raise SystemExit(cli.main(["--env", "local", "--env-file", {str(envf)!r}, "--only", "LOGIN-005", "--out", {str(out)!r}]))
        """
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + textwrap.dedent(code)],
                           capture_output=True, text=True, cwd=str(ROOT), timeout=90)
        assert r.returncode == 1, (r.stdout[-800:], r.stderr[-800:])            # LOGIN-005 FAILS: the mock leaks "No account found"
        shot = out / "screenshots" / "LOGIN-005.png"
        assert shot.is_file() and shot.stat().st_size > 500
        from PIL import Image
        assert Image.open(shot).size[0] > 100
        blob = r.stdout + "".join(p.read_text(encoding="utf-8") for p in out.glob("*") if p.is_file() and p.suffix != ".png")
        assert "Zq9-Cli-Secret-1" not in blob and "u@test.local" not in blob
        assert "LOGIN-005" in blob and "FAIL" in blob
    finally:
        site.stop()


# ------------------------------------------------------------------------------------------------ live-site findings (2026-11-21)
def test_cloudflare_beacon_is_blocked_but_not_counted_as_a_page_write(tmp_path):
    """Live run: POST /cdn-cgi/rum (Cloudflare analytics) made LOGIN-007/009 fail although the app wrote nothing."""
    from agent_tests.login_mock import MockSite
    from login_agent.plan import load_plan
    from login_agent.runner import run_suite
    prod = MockSite(host="localhost", rum_beacon=True).start()
    try:
        envf = tmp_path / "env.yml"
        envf.write_text(yaml.safe_dump({"version": 1, "environments": {"production": {"kind": "production", "apps": {"nurseconnect": {"base_url": prod.url}}}}}))
        res = run_suite(load_plan(), resolve_target("production", envf), only=["LOGIN-007", "LOGIN-009"], out_dir=tmp_path / "o", log=lambda m: None)
        assert [r.status for r in res] == ["PASS", "PASS"], [(r.id, r.reason) for r in res]
        assert any("Cloudflare edge beacon" in n for r in res for n in r.notes)
        assert prod.posts() == [], "the beacon must still be blocked: no POST may reach a production server"
    finally:
        prod.stop()


def test_a_real_page_write_is_still_detected(tmp_path):
    from agent_tests.login_mock import MockSite
    from login_agent.actions import Executor, StepFailed
    from login_agent.guard import NetworkGuard, Observer, PostBudget
    site = MockSite().start()
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            ctx = b.new_context()
            guard = NetworkGuard("production", set(), PostBudget(0)).attach(ctx)
            page = ctx.new_page()
            obs = Observer(page)
            ex = Executor(page, ctx, obs, guard, {}, site.url, "production", {}, [])
            page.goto(site.url + "/auth/login")
            page.evaluate("fetch('/api/auth/register', {method: 'POST', body: '{}'}).catch(() => {})")
            page.wait_for_timeout(300)
            with pytest.raises(StepFailed, match="write request"):
                ex.do_expect_no_writes({})
            b.close()
        assert site.posts() == []
    finally:
        site.stop()


def test_goto_waits_for_late_loading_scripts_before_the_test_clicks(tmp_path):
    """Live run: button clicks right after the page opened did nothing (handlers not wired yet)."""
    from agent_tests.login_mock import MockSite
    from login_agent.actions import Executor
    from login_agent.guard import NetworkGuard, Observer, PostBudget
    site = MockSite().start()
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            ctx = b.new_context()
            page = ctx.new_page()
            ex = Executor(page, ctx, Observer(page), NetworkGuard("local", set(), PostBudget(0)), {"btn": "#b", "out": "#out"},
                          site.url, "local", {}, [])
            ex.do_goto({"path": "/slow-app"})
            ex.do_click({"target": "btn"})
            assert page.inner_text("#out") == "wired", "the click came before the page was ready"
            b.close()
    finally:
        site.stop()
