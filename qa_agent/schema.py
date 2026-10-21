"""Workflow definition schema + validation. Pure data checks: nothing here executes anything.

A workflow file looks like:

    version: 1
    workflows:
      - id: nurse_login
        app: nurseconnect
        title: ...
        status: draft | approved
        risk: low | medium | high
        writes: false
        steps:
          - action: goto
            path: /auth/login
            expected: "Login page loads"

Only the actions listed in ACTIONS exist. There is no eval, no script/shell action and no way to run code.
"""
import difflib
import re

from .errors import SchemaError
from .redaction import find_personal, find_secrets

SCHEMA_VERSION = 1
APPS = ("nurseconnect", "hospital")
STATUSES = ("draft", "approved")
RISKS = ("low", "medium", "high")
SEVERITIES = ("Critical", "High", "Medium", "Low")
ID_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")
ENV_NAME_RX = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
TEMPLATE_RX = re.compile(r"\{\{\s*([A-Za-z_]+)\s*\}\}")
TEMPLATE_VARS = {"RUN_ID", "MARKER", "WF_ID", "SYNTH_PHONE", "SYNTH_EMAIL", "SYNTH_NAME"}
MAX_TIMEOUT_MS = 120_000
MIN_TIMEOUT_MS = 500
MAX_WAIT_MS = 10_000

# Never clicked / submitted by any workflow, approved or not (same spirit as config.DANGEROUS).
DANGEROUS_RX = re.compile(
    r"log\s?out|sign\s?out|delete|remove|discharge|cancel|\bpay\b|pay now|checkout|confirm payment|"
    r"deactivate|terminate|revoke|reset password|clear all|withdraw|refund|cash\s?out|send (?:sms|message|email)|"
    r"notify|broadcast|\bwhatsapp\b",
    re.I,
)

TARGET_KEYS = ("role", "label", "placeholder", "text", "test_id", "css")
TARGET_EXTRA = {"name", "exact", "nth"}
ROLES_OK = {"button", "link", "textbox", "checkbox", "radio", "combobox", "menuitem", "tab", "heading",
            "dialog", "alert", "listbox", "option", "searchbox", "spinbutton", "switch", "row", "cell", "navigation"}

# action -> (required params, optional params, is_write)
ACTIONS = {
    "goto":            ({"path"}, set(), False),
    "click":           ({"target"}, set(), False),
    "fill":            ({"target"}, {"value", "value_env"}, False),
    "select_option":   ({"target", "value"}, set(), False),
    "check":           ({"target"}, set(), False),
    "press":           ({"key"}, {"target"}, False),
    "wait_for":        (set(), {"target", "ms"}, False),
    "open_section":    ({"pattern"}, set(), False),
    "login_submit":    ({"target"}, set(), False),      # click login button; session POST to own origin is allowed
    "guarded_submit":  ({"target"}, set(), False),      # click submit while EVERY non-GET request is blocked
    "submit":          ({"target"}, set(), True),       # real write: staging only, needs confirmation
    "expect_visible":  ({"target"}, set(), False),
    "expect_hidden":   ({"target"}, set(), False),
    "expect_text":     (set(), {"target", "contains", "matches"}, False),
    "expect_url":      (set(), {"contains", "matches"}, False),
    "expect_title":    (set(), {"contains", "matches"}, False),
    "expect_count":    ({"target"}, {"min", "max", "equals"}, False),
    "expect_authenticated": (set(), set(), False),       # no password field visible, not on a login URL
    "expect_no_writes_sent": (set(), set(), False),      # nothing non-GET got past the guard in this workflow so far
    "screenshot":      (set(), {"name"}, False),
}
WRITE_ACTIONS = {a for a, (_, _, w) in ACTIONS.items() if w}
STEP_KEYS = {"id", "action", "expected", "timeout_ms", "severity", "optional"}
WORKFLOW_KEYS = {"id", "app", "title", "description", "status", "risk", "writes", "role", "preconditions",
                 "expected_outcome", "sources", "timeout_ms", "steps", "generated", "approval", "tags"}


def _near(word, options):
    m = difflib.get_close_matches(str(word), list(options), n=1)
    return f" (did you mean {m[0]!r}?)" if m else ""


def _check_template_text(s, where, problems, allow_templates=True):
    for m in TEMPLATE_RX.finditer(s):
        if not allow_templates or m.group(1) not in TEMPLATE_VARS:
            problems.append(f"{where}: unknown template variable {{{{{m.group(1)}}}}}; allowed: {sorted(TEMPLATE_VARS)}")
    if "{{" in TEMPLATE_RX.sub("", s) or "}}" in TEMPLATE_RX.sub("", s):
        problems.append(f"{where}: malformed template braces")


def _check_regex(s, where, problems):
    try:
        re.compile(s)
    except re.error as e:
        problems.append(f"{where}: invalid regular expression ({e})")
        return
    if len(s) > 200:
        problems.append(f"{where}: regular expression too long (max 200)")
    if re.search(r"\((?:[^()]*[+*])\)[+*{]", s):
        problems.append(f"{where}: nested quantifier in regular expression (catastrophic backtracking risk)")


def _check_target(t, where, problems):
    if not isinstance(t, dict):
        problems.append(f"{where}: target must be a mapping such as {{role: button, name: Save}}")
        return
    keys = [k for k in TARGET_KEYS if k in t]
    unknown = set(t) - set(TARGET_KEYS) - TARGET_EXTRA
    if unknown:
        problems.append(f"{where}: unknown target key(s) {sorted(unknown)}; allowed: {list(TARGET_KEYS) + sorted(TARGET_EXTRA)}")
    if len(keys) != 1:
        problems.append(f"{where}: target needs exactly one of {list(TARGET_KEYS)}, got {keys or 'none'}")
        return
    k = keys[0]
    v = t[k]
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{where}: target.{k} must be a non-empty string")
        return
    if k == "role" and v not in ROLES_OK:
        problems.append(f"{where}: unsupported ARIA role {v!r}{_near(v, ROLES_OK)}")
    if "name" in t and k != "role":
        problems.append(f"{where}: target.name is only valid together with role")
    if "css" == k and re.search(r"(?i)javascript:|<script|\bexpression\(", v):
        problems.append(f"{where}: css selector contains script content")
    if "nth" in t and (not isinstance(t["nth"], int) or t["nth"] < 0 or t["nth"] > 50):
        problems.append(f"{where}: target.nth must be an integer 0..50")


def _target_words(t):
    if not isinstance(t, dict):
        return ""
    return " ".join(str(t.get(k, "")) for k in ("name", "label", "text", "placeholder", "css", "test_id"))


def validate_workflow(wf, *, draft_rules: bool = False) -> list:
    """Return a list of problems (empty = valid). `draft_rules=True` applies the extra generated-draft limits."""
    problems = []
    if not isinstance(wf, dict):
        return ["workflow must be a mapping"]
    wid = wf.get("id")
    label = f"workflow {wid!r}" if isinstance(wid, str) else "workflow <no id>"

    unknown = set(wf) - WORKFLOW_KEYS
    if unknown:
        problems.append(f"{label}: unknown key(s) {sorted(unknown)}{_near(sorted(unknown)[0], WORKFLOW_KEYS)}")
    if not isinstance(wid, str) or not ID_RX.match(wid):
        problems.append(f"{label}: id must match {ID_RX.pattern}")
    if wf.get("app") not in APPS:
        problems.append(f"{label}: app must be one of {list(APPS)}, got {wf.get('app')!r}")
    for k in ("title", "description"):
        if not isinstance(wf.get(k), str) or not wf.get(k, "").strip():
            problems.append(f"{label}: {k} is required (non-empty string)")
    status = wf.get("status")
    if status not in STATUSES:
        problems.append(f"{label}: status must be one of {list(STATUSES)}, got {status!r}")
    if wf.get("risk") not in RISKS:
        problems.append(f"{label}: risk must be one of {list(RISKS)}, got {wf.get('risk')!r}")
    if not isinstance(wf.get("writes"), bool):
        problems.append(f"{label}: writes must be true or false (explicit)")
    for k in ("preconditions", "expected_outcome"):
        v = wf.get(k, [])
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            problems.append(f"{label}: {k} must be a string or a list of strings")
    if "sources" in wf and not (isinstance(wf["sources"], list) and all(isinstance(s, dict) and isinstance(s.get("file"), str) for s in wf["sources"])):
        problems.append(f"{label}: sources must be a list of mappings with a 'file' key")
    t = wf.get("timeout_ms", 30000)
    if not isinstance(t, int) or isinstance(t, bool) or not MIN_TIMEOUT_MS <= t <= MAX_TIMEOUT_MS:
        problems.append(f"{label}: timeout_ms must be an integer {MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS}")

    steps = wf.get("steps")
    if not isinstance(steps, list) or not steps:
        problems.append(f"{label}: steps must be a non-empty list")
        steps = []
    has_write_step = False
    for n, st in enumerate(steps, 1):
        where = f"{label} step {n}"
        if not isinstance(st, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        act = st.get("action")
        if act not in ACTIONS:
            problems.append(f"{where}: unknown action {act!r}{_near(act, ACTIONS)}; supported: {sorted(ACTIONS)}")
            continue
        where = f"{where} ({act})"
        required, optional, is_write = ACTIONS[act]
        allowed = STEP_KEYS | required | optional
        extra = set(st) - allowed
        if extra:
            problems.append(f"{where}: unexpected key(s) {sorted(extra)}; this action accepts {sorted(required | optional)}")
        missing = required - set(st)
        if missing:
            problems.append(f"{where}: missing required key(s) {sorted(missing)}")
        if "expected" in st and not isinstance(st["expected"], str):
            problems.append(f"{where}: expected must be a string")
        if "severity" in st and st["severity"] not in SEVERITIES:
            problems.append(f"{where}: severity must be one of {list(SEVERITIES)}")
        if "timeout_ms" in st and (not isinstance(st["timeout_ms"], int) or not MIN_TIMEOUT_MS <= st["timeout_ms"] <= MAX_TIMEOUT_MS):
            problems.append(f"{where}: timeout_ms must be an integer {MIN_TIMEOUT_MS}..{MAX_TIMEOUT_MS}")
        if "target" in st:
            _check_target(st["target"], where, problems)
        if act == "goto" and "path" in st:
            p = st["path"]
            if not isinstance(p, str) or not p.startswith("/") or p.startswith("//") or re.search(r"(://|\\|\.\.|\s)", p):
                problems.append(f"{where}: path must be a site-relative path starting with '/' (no scheme, host, '..' or spaces)")
            else:
                _check_template_text(p, where, problems)
        if act in ("expect_text", "expect_url", "expect_title"):
            if ("contains" in st) == ("matches" in st):
                problems.append(f"{where}: give exactly one of contains / matches")
        for k in ("contains", "matches", "pattern"):
            if k in st:
                if not isinstance(st[k], str) or not st[k]:
                    problems.append(f"{where}: {k} must be a non-empty string")
                elif k in ("matches", "pattern"):
                    _check_regex(st[k], f"{where}.{k}", problems)
                elif k == "contains":
                    _check_template_text(st[k], where, problems)
        if act == "fill":
            if ("value" in st) == ("value_env" in st):
                problems.append(f"{where}: give exactly one of value / value_env")
            if "value_env" in st and not (isinstance(st["value_env"], str) and ENV_NAME_RX.match(st["value_env"])):
                problems.append(f"{where}: value_env must be an environment variable NAME like NURSE_EMAIL (never the secret itself)")
            if "value" in st:
                v = st["value"]
                if not isinstance(v, str):
                    problems.append(f"{where}: value must be a string")
                else:
                    _check_template_text(v, where, problems)
                    lit = TEMPLATE_RX.sub("", v)
                    if find_secrets(lit) or find_personal(lit):
                        problems.append(f"{where}: literal value looks like a secret or personal data; use synthetic templates "
                                        f"({sorted(TEMPLATE_VARS)}) or value_env")
        if act == "select_option" and not isinstance(st.get("value"), str):
            problems.append(f"{where}: value must be a string")
        if act == "press" and not (isinstance(st.get("key"), str) and re.fullmatch(r"[A-Za-z0-9+]{1,30}", st["key"])):
            problems.append(f"{where}: key must be a simple key name such as Enter or Tab")
        if act == "wait_for":
            if "ms" in st and (not isinstance(st["ms"], int) or not 0 < st["ms"] <= MAX_WAIT_MS):
                problems.append(f"{where}: ms must be 1..{MAX_WAIT_MS}")
            if ("ms" in st) == ("target" in st):
                problems.append(f"{where}: give exactly one of ms / target")
        if act == "expect_count" and not any(k in st for k in ("min", "max", "equals")):
            problems.append(f"{where}: give at least one of min / max / equals")
        if act in ("click", "open_section", "submit", "guarded_submit") and DANGEROUS_RX.search(
                _target_words(st.get("target")) + " " + str(st.get("pattern", ""))):
            problems.append(f"{where}: target looks destructive / payment / messaging related (never allowed in any workflow)")
        if is_write:
            has_write_step = True

    writes = wf.get("writes")
    if isinstance(writes, bool):
        if has_write_step and not writes:
            problems.append(f"{label}: contains a write action but writes is false")
        if writes and not has_write_step:
            problems.append(f"{label}: writes is true but no write action exists")
        if writes and wf.get("risk") == "high":
            problems.append(f"{label}: high-risk workflows may not perform writes")

    if draft_rules:
        if status != "draft":
            problems.append(f"{label}: generated workflows must have status: draft")
        if writes is not False or has_write_step:
            problems.append(f"{label}: generated drafts may not contain write actions")
        if not wf.get("sources"):
            problems.append(f"{label}: generated drafts must reference their source files")
        for k in ("preconditions", "expected_outcome"):
            if not wf.get(k):
                problems.append(f"{label}: generated drafts must include {k}")
    return problems


def validate_document(doc, *, draft_rules: bool = False, source: str = "<memory>") -> list:
    """Validate a whole parsed YAML document. Returns problems (empty = valid)."""
    if not isinstance(doc, dict):
        return [f"{source}: top level must be a mapping with 'version' and 'workflows'"]
    problems = []
    if doc.get("version") != SCHEMA_VERSION:
        problems.append(f"{source}: version must be {SCHEMA_VERSION}, got {doc.get('version')!r}")
    extra = set(doc) - {"version", "workflows", "description"}
    if extra:
        problems.append(f"{source}: unknown top-level key(s) {sorted(extra)}")
    wfs = doc.get("workflows")
    if not isinstance(wfs, list) or not wfs:
        problems.append(f"{source}: 'workflows' must be a non-empty list")
        return problems
    seen = set()
    for w in wfs:
        problems += [f"{source}: {p}" for p in validate_workflow(w, draft_rules=draft_rules)]
        wid = w.get("id") if isinstance(w, dict) else None
        if wid in seen:
            problems.append(f"{source}: duplicate workflow id {wid!r}")
        seen.add(wid)
    return problems


def require_valid(doc, **kw):
    problems = validate_document(doc, **kw)
    if problems:
        raise SchemaError(problems)
