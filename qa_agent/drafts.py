"""Deterministic draft-workflow generation from a discovery result. No AI model, no network.

Every draft is `status: draft`, `writes: false`, contains only read-only / no-write actions, names its source
files, is validated with the strict draft rules before it is saved, and can only run after a human approves it
(`python agent.py approve ...`). Drafts live in workflow_drafts/, never in workflows.yml or workflows_approved/.
"""
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import __version__
from .redaction import assert_clean, find_personal, find_secrets, redact
from .schema import SCHEMA_VERSION, validate_document
from .workflows import APPROVED_DIR, DRAFTS_DIR, DEFAULT_WORKFLOWS, content_hash, load_workflows

# Heuristic role guesses (a human must confirm before approval). Keyword -> role name used by config.ROLES.
ROLE_HINTS = {
    "nurseconnect": [("consumer", "consumer"), ("partner", "nurse"), ("nurse", "nurse")],
    "hospital": [("reception", "reception"), ("doctor", "doctor"), ("opd", "reception")],
}
DEFAULT_ROLE = {"nurseconnect": "nurse", "hospital": "reception"}
BOOKING_RX = re.compile(r"(?i)book|appointment|schedul|register|registration|opd|visit|enrol")
DYNAMIC_RX = re.compile(r"[:*\[\]()]")


def slug(s: str, limit: int = 36) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return (s or "root")[:limit].strip("_") or "root"


def _suffix(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:4]


def _role_for(app: str, route: str, source_file: str) -> str:
    hay = (route + " " + source_file).lower()
    for kw, role in ROLE_HINTS.get(app, []):
        if kw in hay:
            return role
    return DEFAULT_ROLE.get(app, "")


def _target_for(field: dict):
    """Prefer accessible selectors: label > placeholder > name attribute."""
    if field.get("label"):
        return {"label": field["label"]}
    if field.get("placeholder"):
        return {"placeholder": field["placeholder"]}
    if field.get("name") and re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", field["name"]):
        return {"css": f'[name="{field["name"]}"]'}
    return None


def _synthetic_value(field: dict) -> str:
    hay = " ".join(str(field.get(k, "")) for k in ("name", "label", "placeholder", "type")).lower()
    if "mail" in hay:
        return "{{SYNTH_EMAIL}}"
    if re.search(r"phone|mobile|contact|tel", hay):
        return "{{SYNTH_PHONE}}"
    if re.search(r"name", hay):
        return "{{SYNTH_NAME}}"
    return "{{MARKER}}"


def _button_target(btn: dict):
    n = btn.get("accessible_name") or btn.get("text")
    return {"role": "button", "name": n} if n else None


def _base(app, wid, title, description, risk, route, sources, preconditions, outcome, role, steps, method):
    wf = {"id": wid, "app": app, "title": title, "description": description, "status": "draft", "risk": risk, "writes": False,
          "preconditions": preconditions, "expected_outcome": outcome,
          "sources": [{"file": f, "line": l} for f, l in sources], "timeout_ms": 30000, "steps": steps,
          "generated": {"tool": f"qa_agent {__version__}", "method": method, "route": route}}
    if role:
        wf["role"] = role
    return wf


def generate_drafts(discovery: dict) -> tuple:
    """-> (workflows, skipped) where skipped is a list of {item, reason}."""
    app, out, skipped = discovery["app"], [], []
    seen_ids = set()
    method = discovery.get("method", "static-source-analysis")

    def add(wf):
        base, n = wf["id"], 2
        while wf["id"] in seen_ids:
            wf["id"] = f"{base[:58]}_{n}"
            n += 1
        seen_ids.add(wf["id"])
        out.append(wf)

    routes = [r for r in discovery["routes"] if r["route"] != "(index)" and not r.get("relative") and not DYNAMIC_RX.search(r["route"])]
    for r in discovery["routes"]:
        if r not in routes:
            skipped.append({"item": f"route {r['route']} ({r['source_file']}:{r['line']})",
                            "reason": "dynamic, relative or index route - needs a concrete URL; add by hand"})
    for r in routes:
        protected = r["protected"] is True
        role = _role_for(app, r["route"], r["source_file"]) if protected or r["protected"] == "unknown" else ""
        steps = [{"action": "goto", "path": r["route"], "expected": "Page responds without a server error"},
                 {"action": "expect_visible", "target": {"css": "body"}, "expected": "Page body renders"}]
        pre = ["Environment variables for the chosen role's test account are set (credentials are never stored in YAML)."]
        if protected:
            steps.insert(1, {"action": "expect_authenticated", "expected": "Authenticated user stays on the page (no login redirect)"})
            pre.append(f"Route appears auth-protected in source ({r['protection_evidence']}); role '{role}' is a heuristic guess - confirm it.")
        else:
            pre.append("Protection unknown from source: role is a guess; remove 'role' if the page is public.")
        steps.append({"action": "expect_no_writes_sent", "expected": "No write request was sent"})
        add(_base(app, f"nav_{slug(r['route'])}_{_suffix(app, r['route'], r['source_file'])}", f"Open {r['route']}",
                  f"Read-only navigation check for route {r['route']} discovered in {r['source_file']}.", "low", r["route"],
                  [(r["source_file"], r["line"])], pre, ["The page loads and renders content", "No write request is sent"], role, steps, method))

    route_by_file = {}
    for r in routes:
        for f in {r["source_file"], r.get("component_file")}:
            if f:
                route_by_file.setdefault(f, r)
    for form in discovery["forms"]:
        label = f"form at {form['source_file']}:{form['line']}"
        if form["has_password"]:
            skipped.append({"item": label, "reason": "credentials form (password field) - never auto-generated"})
            continue
        r = route_by_file.get(form["source_file"])
        if not r:
            skipped.append({"item": label, "reason": "no concrete route resolved for this form's file"})
            continue
        buttons = [b for b in form["buttons"] if not b.get("dangerous")]
        if len(buttons) != len(form["buttons"]) and not buttons:
            skipped.append({"item": label, "reason": "only destructive/payment/messaging buttons - skipped"})
            continue
        submit = next((_button_target(b) for b in buttons if _button_target(b)), None)
        if not submit:
            skipped.append({"item": label, "reason": "no submit button with a static accessible name"})
            continue
        role = _role_for(app, r["route"], r["source_file"]) if r["protected"] is not False else ""
        vmsgs = [v for v in discovery["validation"] if v["source_file"] == form["source_file"] and v["rule"] == "required"][:4]
        steps = [{"action": "goto", "path": r["route"], "expected": "Form page loads"},
                 {"action": "guarded_submit", "target": submit, "expected": "Submitting empty form is rejected by validation; every write request is blocked"}]
        for v in vmsgs:
            steps.append({"action": "expect_text", "contains": v["message"], "expected": "Validation message from source is shown", "severity": "Low"})
        steps.append({"action": "expect_no_writes_sent", "expected": "Nothing was sent to the server"})
        src = [(form["source_file"], form["line"])] + [(v["source_file"], v["line"]) for v in vmsgs]
        add(_base(app, f"form_validation_{slug(r['route'])}_{_suffix(app, form['source_file'], form['line'])}", f"Empty-submit validation on {r['route']}",
                  f"Submits the empty form found at {form['source_file']}:{form['line']} with all non-GET requests blocked and checks client-side validation.",
                  "low", r["route"], src,
                  ["Role credentials available via environment variables", "Form page is reachable for the chosen role"],
                  ["Empty submit does not create a record", "Validation feedback is shown (messages come from source and may differ in the UI)"], role, steps, method))

        if BOOKING_RX.search(r["route"] + " " + form["source_file"]):
            fsteps = [{"action": "goto", "path": r["route"], "expected": "Form page loads"}]
            filled = 0
            for fld in form["fields"]:
                if fld["kind"] in ("select", "choice") or fld["type"].lower() in ("password", "file", "hidden", "checkbox", "radio") or fld.get("dangerous"):
                    continue
                tgt = _target_for(fld)
                if tgt:
                    fsteps.append({"action": "fill", "target": tgt, "value": _synthetic_value(fld), "expected": "Synthetic value accepted", "optional": True})
                    filled += 1
            if filled:
                fsteps += [{"action": "expect_visible", "target": submit, "expected": "Submit button is available after the form is filled"},
                           {"action": "guarded_submit", "target": submit, "expected": "Submit is attempted with writes blocked; nothing is created"},
                           {"action": "expect_no_writes_sent", "expected": "No record was created"}]
                add(_base(app, f"booking_flow_{slug(r['route'])}_{_suffix(app, form['source_file'], form['line'], 'b')}", f"Fill (no submit) {r['route']}",
                          "Fills the form with synthetic data and attempts submit with all writes blocked. A real create needs a separate, hand-written, "
                          "approved staging workflow.", "medium", r["route"], [(form["source_file"], form["line"])],
                          ["Role credentials available via environment variables", "Select/dropdown fields are not filled automatically"],
                          ["Form accepts synthetic input", "No record is created"], role, fsteps, method))
            else:
                skipped.append({"item": label + " (booking fill)", "reason": "no fields with a static label/placeholder/name to fill"})

    # validate with the strict generated-draft rules before they are ever shown
    valid = []
    for wf in out:
        probs = validate_document({"version": SCHEMA_VERSION, "workflows": [wf]}, draft_rules=True, source=wf["id"])
        if probs:
            skipped.append({"item": f"draft {wf['id']}", "reason": "failed validation: " + "; ".join(probs)[:300]})
        else:
            valid.append(wf)
    return valid, skipped


def save_drafts(workflows, skipped, app: str, drafts_dir=DRAFTS_DIR, approved_dir=APPROVED_DIR, main_file=DEFAULT_WORKFLOWS) -> dict:
    """Write each draft to workflow_drafts/<app>/<id>.yml + INDEX.md. Never touches approved workflows."""
    drafts_dir = Path(drafts_dir)
    app_dir = drafts_dir / app
    app_dir.mkdir(parents=True, exist_ok=True)
    approved = {}
    try:
        for f in [main_file] + sorted(Path(approved_dir).glob("*.yml")) if Path(approved_dir).is_dir() else [main_file]:
            if Path(f).is_file():
                for w in load_workflows(f):
                    approved[w["id"]] = content_hash(w)
    except Exception:
        approved = {}
    written, kept, blocked = [], [], []
    for wf in workflows:
        if wf["id"] in approved:
            (kept if approved[wf["id"]] == content_hash(wf) else blocked).append(wf["id"])
            continue
        text = yaml.safe_dump({"version": SCHEMA_VERSION, "workflows": [wf]}, sort_keys=False, allow_unicode=True)
        if find_secrets(text) or find_personal(text):
            skipped.append({"item": f"draft {wf['id']}", "reason": "contained secret/personal-looking content; not saved"})
            continue
        path = app_dir / f"{wf['id']}.yml"
        header = ("# GENERATED DRAFT - NOT APPROVED. This file cannot be executed.\n"
                  "# Review it, then: python agent.py approve --draft <this file> --by <you> --reason <why safe>\n")
        path.write_text(header + text, encoding="utf-8")
        written.append(str(path))
    idx = [f"# Draft workflows - {app}", "", f"_Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}. Every file below is a DRAFT and will not run until approved._", "",
           "| Draft | Risk | Role (guess) | Route | Sources |", "|---|---|---|---|---|"]
    for wf in workflows:
        if wf["id"] in approved:
            continue
        idx.append(f"| `{wf['id']}` | {wf['risk']} | {wf.get('role', '-')} | `{redact(wf['generated']['route'])}` | "
                   + ", ".join(f"{s['file']}:{s.get('line', '')}" for s in wf["sources"][:3]) + " |")
    if kept:
        idx += ["", "## Already approved and unchanged (no draft written)", ""] + [f"- `{i}`" for i in kept]
    if blocked:
        idx += ["", "## Approved workflows whose regenerated draft DIFFERS (compare manually; no draft written)", ""] + [f"- `{i}`" for i in blocked]
    idx += ["", "## Skipped (with reason)", ""] + [f"- {redact(s['item'])}: {redact(s['reason'])}" for s in skipped]
    text = "\n".join(idx) + "\n"
    assert_clean(text, what="draft index")
    (app_dir / "INDEX.md").write_text(text, encoding="utf-8")
    return {"written": written, "unchanged_approved": kept, "differs_from_approved": blocked, "skipped": skipped}
