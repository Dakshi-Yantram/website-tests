"""Load and validate the YAML plan, the fake-id files and the template values. Fails early with a clear message."""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN = ROOT / "login_tests" / "login-test-plan.yaml"
DEFAULT_FAKE_ID = ROOT / "fake-id"

SEVERITIES = ("Critical", "High", "Medium", "Low")
REQUIRES = ("anonymous", "staging")
ENV_KINDS = ("production", "staging", "local")

# action -> allowed parameter names (besides "action"); a typo in the YAML is an error, never silently ignored.
ACTIONS = {
    "goto": {"path", "query"},
    "fill": {"target", "value"},
    "clear": {"target"},
    "click": {"target"},
    "wait": {"ms"},
    "expect_visible": {"target", "timeout_ms"},
    "expect_hidden": {"target", "timeout_ms"},
    "expect_disabled": {"target", "timeout_ms"},
    "expect_attr": {"target", "attr", "equals", "timeout_ms"},
    "expect_value": {"target", "equals", "timeout_ms"},
    "expect_invalid_field": {"target", "timeout_ms"},
    "expect_url": {"path_equals", "path_startswith", "path_not_startswith", "same_origin", "timeout_ms"},
    "expect_text": {"target", "matches", "not_matches", "timeout_ms"},
    "expect_feedback": {"timeout_ms"},
    "expect_login_request": {"made", "status_in", "timeout_ms"},
    "expect_session": {"timeout_ms"},
    "expect_no_session": set(),
    "expect_no_request_to": {"host_contains"},
    "expect_no_writes": set(),
    "tab_through": {"max_tabs"},
    "expect_tab_order": {"order"},
    "expect_focus_visible": {"targets"},
    "expect_accessible_name": {"targets"},
}
COMMON_STEP_KEYS = {"action", "only_env_kinds", "note"}
TARGET_PARAMS = ("target",)
LIST_TARGET_PARAMS = ("targets", "order")


class PlanError(Exception):
    """The plan or the fake-id files are wrong. Exit code 2, nothing is run."""


@dataclass
class Test:
    __test__ = False          # not a pytest test class
    id: str
    title: str
    severity: str
    requires: str
    steps: list
    login_post: bool = False
    identity: str = ""
    foreach: str = ""
    foreach_where: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)


@dataclass
class Plan:
    suite: str
    target: dict
    selectors: dict
    tests: list
    data: dict
    limits: dict
    patterns: dict
    redirect_payloads: list
    identities: dict          # name -> raw dict from test-identities.yaml


def _load_yaml(path: Path, what: str) -> dict:
    if not path.is_file():
        raise PlanError(f"{what} not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise PlanError(f"{what} {path} is not valid YAML: {str(e)[:300]}")
    if not isinstance(data, dict):
        raise PlanError(f"{what} {path}: top level must be a mapping")
    return data


def load_plan(plan_path=None, fake_id_dir=None) -> Plan:
    plan_path = Path(plan_path or os.getenv("LOGIN_PLAN") or DEFAULT_PLAN)
    fid = Path(fake_id_dir or os.getenv("LOGIN_FAKE_ID_DIR") or DEFAULT_FAKE_ID)
    raw = _load_yaml(plan_path, "Test plan")
    td = _load_yaml(fid / "test-data.yaml", "fake-id/test-data.yaml")
    ids = _load_yaml(fid / "test-identities.yaml", "fake-id/test-identities.yaml")

    selectors = raw.get("selectors") or {}
    if not isinstance(selectors, dict) or not all(isinstance(v, str) and v.strip() for v in selectors.values()):
        raise PlanError("selectors: must map names to non-empty selector strings")
    identities = ids.get("identities") or {}
    if not isinstance(identities, dict) or not identities:
        raise PlanError("test-identities.yaml: 'identities' mapping is required")
    for name, ident in identities.items():
        if not isinstance(ident, dict) or "email" not in ident:
            raise PlanError(f"identity {name!r}: needs an 'email'")
        if "password" in ident and "password_env" in ident:
            raise PlanError(f"identity {name!r}: use either password or password_env, not both")

    payloads = td.get("redirect_payloads") or []
    for p in payloads:
        if not isinstance(p, dict) or "value" not in p or "id" not in p:
            raise PlanError("redirect_payloads entries need 'id' and 'value'")

    tests, seen = [], set()
    for t in raw.get("tests") or []:
        tid = str(t.get("id", "")).strip()
        if not re.fullmatch(r"[A-Z]+-\d{3}", tid):
            raise PlanError(f"test id {tid!r} must look like LOGIN-001")
        if tid in seen:
            raise PlanError(f"duplicate test id {tid}")
        seen.add(tid)
        if t.get("severity") not in SEVERITIES:
            raise PlanError(f"{tid}: severity must be one of {SEVERITIES}")
        if t.get("requires") not in REQUIRES:
            raise PlanError(f"{tid}: requires must be one of {REQUIRES}")
        if t.get("identity") and t["identity"] not in identities:
            raise PlanError(f"{tid}: unknown identity {t['identity']!r}")
        if t.get("foreach") and t["foreach"] != "redirect_payloads":
            raise PlanError(f"{tid}: foreach supports only 'redirect_payloads'")
        steps = t.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PlanError(f"{tid}: steps must be a non-empty list")
        for i, s in enumerate(steps, 1):
            _check_step(tid, i, s, selectors)
        if t["requires"] == "anonymous" and t.get("identity") and not t.get("login_post"):
            pass  # an identity on an anonymous test is allowed (values are only typed); credentials are not required
        tests.append(Test(id=tid, title=str(t.get("title", tid)), severity=t["severity"], requires=t["requires"], steps=steps,
                          login_post=bool(t.get("login_post")), identity=t.get("identity", ""), foreach=t.get("foreach", ""),
                          foreach_where=dict(t.get("foreach_where") or {}), tags=list(t.get("tags") or [])))
    if not tests:
        raise PlanError("the plan contains no tests")
    return Plan(suite=str(raw.get("suite", "login")), target=dict(raw.get("target") or {}), selectors=selectors, tests=tests,
                data=dict(td.get("data") or {}), limits=dict(td.get("limits") or {}), patterns=dict(td.get("patterns") or {}),
                redirect_payloads=payloads, identities=identities)


def _check_step(tid, i, s, selectors):
    where = f"{tid} step {i}"
    if not isinstance(s, dict) or "action" not in s:
        raise PlanError(f"{where}: each step needs an 'action'")
    action = s["action"]
    if action not in ACTIONS:
        raise PlanError(f"{where}: unknown action {action!r}; known: {sorted(ACTIONS)}")
    extra = set(s) - ACTIONS[action] - COMMON_STEP_KEYS
    if extra:
        raise PlanError(f"{where}: action {action!r} does not accept {sorted(extra)}")
    for key in TARGET_PARAMS:
        if key in s and s[key] not in selectors:
            raise PlanError(f"{where}: unknown selector {s[key]!r}")
    for key in LIST_TARGET_PARAMS:
        if key in s:
            if not isinstance(s[key], list) or not all(x in selectors for x in s[key]):
                raise PlanError(f"{where}: {key} must list names from 'selectors'")
    if "only_env_kinds" in s and not set(s["only_env_kinds"]) <= set(ENV_KINDS):
        raise PlanError(f"{where}: only_env_kinds must be a subset of {ENV_KINDS}")
    needs_target = {"fill", "clear", "click", "expect_visible", "expect_hidden", "expect_disabled", "expect_attr", "expect_value",
                    "expect_invalid_field"}
    if action in needs_target and "target" not in s:
        raise PlanError(f"{where}: action {action!r} needs a 'target'")
    if action == "expect_text" and not (s.get("matches") or s.get("not_matches")):
        raise PlanError(f"{where}: expect_text needs 'matches' or 'not_matches'")
    if action == "expect_login_request" and "made" not in s and "status_in" not in s:
        raise PlanError(f"{where}: expect_login_request needs 'made' or 'status_in'")


# ------------------------------------------------------------------------------------------------ identities and templates
@dataclass
class Identity:
    name: str
    email: str = ""
    password: str = ""
    landing_path: str = ""
    role: str = ""
    password_env: str = ""
    email_env: str = ""

    @property
    def needs_secret(self) -> bool:
        return bool(self.password_env)

    @property
    def missing(self) -> list:
        out = []
        if self.password_env and not self.password:
            out.append(self.password_env)
        if not self.email:
            out.append(self.email_env or f"email of {self.name}")
        return out


def resolve_identity(name: str, raw: dict) -> Identity:
    email_env = str(raw.get("email_env", "") or "")
    email = (os.getenv(email_env, "").strip() if email_env else "") or str(raw.get("email", ""))
    pw_env = str(raw.get("password_env", "") or "")
    password = os.getenv(pw_env, "") if pw_env else str(raw.get("password", ""))
    return Identity(name=name, email=email, password=password, landing_path=str(raw.get("landing_path", "") or ""),
                    role=str(raw.get("role", "")), password_env=pw_env, email_env=email_env)


_TPL = re.compile(r"\{\{\s*([a-z_]+)\.([A-Za-z0-9_]+)\s*\}\}")


class TemplateError(Exception):
    pass


def render(value, scopes: dict):
    """Replace {{scope.key}} in strings (recursively in dict/list). Unknown or empty-but-required values raise TemplateError."""
    if isinstance(value, str):
        def sub(m):
            scope, key = m.group(1), m.group(2)
            if scope not in scopes:
                raise TemplateError(f"'{{{{{scope}.{key}}}}}' is not available in this test")
            src = scopes[scope]
            val = getattr(src, key, None) if not isinstance(src, dict) else src.get(key)
            if val is None or val == "":
                raise TemplateError(f"'{{{{{scope}.{key}}}}}' has no value")
            return str(val)
        return _TPL.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, scopes) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, scopes) for v in value]
    return value
