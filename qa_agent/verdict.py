"""Honest run verdict: classify BLOCKED results, flag critical NOT VERIFIED ones and decide the process exit code.

Exit codes (cmd_run): 0 ok | 1 FAIL | 4 BLOCKED (--fail-on-blocked) | 5 any NOT VERIFIED / NOT TESTED (--fail-on-incomplete)
| 6 critical workflow NOT VERIFIED / NOT TESTED / BLOCKED (any kind) / never executed (--fail-on-critical-unverified)
--ignore-blocked KIND[,KIND] (e.g. unapproved) lets CI tolerate intentional blocks of NON-critical workflows only; a critical
workflow is never ignored, whatever the reason it did not run.
"""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .schema import RISKS

CRITICAL_FILE = Path(__file__).resolve().parent.parent / "config" / "critical_workflows.yml"
DEFAULT_CRITICAL_RISK = ("high",)
BLOCK_KINDS = ("credentials", "unapproved", "policy", "other")   # default: every kind fails --fail-on-blocked (legacy contract)

_CRED = ("Credentials missing", "Unknown role", "is not set")
_UNAPPROVED = ("NOT APPROVED",)
_POLICY = ("WRITE BLOCKED", "STAGING-ONLY", "Refused to click", "goto may only target")


def block_kind(reason: str) -> str:
    """credentials | unapproved | policy | other  (substring match: reasons are prefixed with 'step N (action): ')."""
    r = reason or ""
    if any(m in r for m in _UNAPPROVED):
        return "unapproved"
    if any(m in r for m in _CRED):
        return "credentials"
    if any(m in r for m in _POLICY):
        return "policy"
    return "other"


class CriticalConfigError(Exception):
    """config/critical_workflows.yml is missing or malformed. Callers must fail closed."""


def load_critical(path: Path | None = None) -> tuple:
    p = Path(path) if path else CRITICAL_FILE
    if not p.is_file():
        raise CriticalConfigError(f"critical workflow list not found: {p}")
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise CriticalConfigError(f"critical workflow list unreadable: {type(e).__name__}") from e
    if not isinstance(doc, dict):
        raise CriticalConfigError("critical workflow list must be a YAML mapping")
    extra = set(doc) - {"risk_levels", "ids"}
    if extra:
        raise CriticalConfigError(f"unknown key(s) in critical workflow list: {sorted(extra)}")
    levels, ids = doc.get("risk_levels"), doc.get("ids", [])
    if not isinstance(levels, list) or not levels or not all(isinstance(x, str) for x in levels):
        raise CriticalConfigError("risk_levels must be a non-empty list of strings")
    bad = sorted({x for x in levels if x not in RISKS})
    if bad:
        raise CriticalConfigError(f"unsupported risk_levels {bad}; the workflow schema supports {list(RISKS)}")
    if not isinstance(ids, list) or not all(isinstance(x, str) and x for x in ids):
        raise CriticalConfigError("ids must be a list of workflow id strings")
    return tuple(levels), frozenset(ids)


def check_known_ids(workflows, path: Path | None = None) -> list:
    """Problems for `agent.py validate`: configured ids that match no loaded workflow (typo / renamed / deleted)."""
    _, ids = load_critical(path)
    have = {w["id"] for w in workflows}
    return [f"critical workflow id {i!r} matches no loaded workflow" for i in sorted(ids - have)]


def is_critical(wf: dict | None, critical=None) -> bool:
    if not wf:
        return False
    levels, ids = critical if critical is not None else load_critical()
    return wf.get("risk") in levels or wf.get("id") in ids


def analyse(results, workflows, critical=None, apps=None, selection=()) -> dict:
    """apps/selection describe what the run was asked to execute: every critical workflow of those apps must have a result
    (a critical workflow that was skipped or never executed is reported in `missing_critical`; explicit --workflow filters opt out)."""
    by_id = {w["id"]: w for w in workflows}
    crit = critical if critical is not None else load_critical()
    out = {"blocked": {"credentials": [], "unapproved": [], "policy": [], "other": []},
           "critical_unverified": [], "critical_blocked": [], "missing_critical": [], "unverified": [], "failed": [], "passed": 0, "total": len(results)}
    if apps is not None and not selection:
        seen = {r.id for r in results}
        out["missing_critical"] = sorted(w["id"] for w in workflows if w.get("app") in apps and is_critical(w, crit) and w["id"] not in seen)
    for r in results:
        if r.status == "PASS":
            out["passed"] += 1
        elif r.status == "FAIL":
            out["failed"].append(r.id)
        elif r.status == "BLOCKED":
            out["blocked"][block_kind(r.reason)].append(r.id)
            if is_critical(by_id.get(r.id), crit):
                out["critical_blocked"].append(r.id)
        elif r.status in ("NOT VERIFIED", "NOT TESTED"):
            out["unverified"].append(r.id)
            if is_critical(by_id.get(r.id), crit):
                out["critical_unverified"].append(r.id)
    return out


def decide_exit(a: dict, *, fail_on_blocked=False, fail_on_incomplete=False, fail_on_critical_unverified=False, ignore_blocked=()) -> int:
    if a["failed"]:
        return 1
    if fail_on_blocked:
        critical = set(a["critical_blocked"])
        for kind in BLOCK_KINDS:
            ids = a["blocked"][kind]
            if kind in ignore_blocked:
                ids = [i for i in ids if i in critical]      # ignoring a kind never covers a critical workflow
            if ids:
                return 4
    if fail_on_critical_unverified and (a["critical_unverified"] or a["critical_blocked"] or a["missing_critical"]):
        return 6
    if fail_on_incomplete and a["unverified"]:
        return 5
    return 0


def verdict_lines(a: dict, code: int) -> list:
    b = a["blocked"]
    if a["failed"]:
        head = "FAILED"
    elif a["passed"] == a["total"] and a["total"] and not a["missing_critical"]:
        head = "PASSED"
    else:
        head = "NOT FULLY TESTED"
    lines = [f"VERDICT: {head} - {a['passed']} of {a['total']} workflow(s) actually verified (PASS); exit code {code}",
             f"  FAIL={len(a['failed'])}  NOT VERIFIED/NOT TESTED={len(a['unverified'])} (critical: {len(a['critical_unverified'])})",
             f"  BLOCKED: missing credentials/config={len(b['credentials'])}, unapproved draft={len(b['unapproved'])}, "
             f"policy (read-only/write guard)={len(b['policy'])}, other={len(b['other'])}"]
    if a["missing_critical"]:
        lines.append("  CRITICAL NOT EXECUTED (no result at all): " + ", ".join(a["missing_critical"][:25]) + (" ..." if len(a["missing_critical"]) > 25 else ""))
    if a["critical_blocked"]:
        lines.append("  CRITICAL BLOCKED (never ignored): " + ", ".join(a["critical_blocked"][:25]) + (" ..." if len(a["critical_blocked"]) > 25 else ""))
    if a["critical_unverified"]:
        lines.append("  CRITICAL NOT VERIFIED: " + ", ".join(a["critical_unverified"][:25]) + (" ..." if len(a["critical_unverified"]) > 25 else ""))
    return lines


def finish(results, workflows, *, say, fail_on_blocked=False, fail_on_incomplete=False, fail_on_critical_unverified=False,
           ignore_blocked=(), apps=None, selection=(), critical_path=None) -> int:
    try:
        critical = load_critical(critical_path)
    except CriticalConfigError as e:
        if fail_on_critical_unverified:
            say(f"CRITICAL WORKFLOW LIST INVALID - failing closed: {e}")
            return 2
        say(f"WARNING: {e}; using default risk levels {DEFAULT_CRITICAL_RISK} only")
        critical = (DEFAULT_CRITICAL_RISK, frozenset())
    a = analyse(results, workflows, critical=critical, apps=apps, selection=selection)
    code = decide_exit(a, fail_on_blocked=fail_on_blocked, fail_on_incomplete=fail_on_incomplete,
                       fail_on_critical_unverified=fail_on_critical_unverified, ignore_blocked=ignore_blocked)
    lines = verdict_lines(a, code)
    for ln in lines:
        say(ln)
    summ = os.getenv("GITHUB_STEP_SUMMARY")
    if summ:
        try:
            with open(summ, "a", encoding="utf-8") as fh:
                fh.write("### QA agent verdict\n\n" + "\n".join("    " + ln for ln in lines) + "\n\n")
        except OSError:
            pass
    return code
