#!/usr/bin/env python3
"""NurseConnect QA agent command line.

  python agent.py validate                       check workflows.yml + approvals (no browser, no network)
  python agent.py discover --app nurseconnect=apps/nurseconnect --app hospital=apps/hospital
  python agent.py draft                          generate DRAFT workflows from the latest discovery
  python agent.py approve --draft FILE --by NAME --reason TEXT      (human decision, auditable)
  python agent.py approve --id ID --by NAME --reason TEXT           (approve a workflow already in workflows.yml)
  python agent.py revoke --id ID --by NAME --reason TEXT
  python agent.py inventory                      draft / approved / unapproved / changed + last result
  python agent.py run --env NAME [--dry-run | --read-only] [--app A] [--workflow ID]
  python agent.py verify-env --env NAME --app A
  python agent.py check-approvals                CI approval gate
  python agent.py scan-artifacts DIR [--fix]     redact + fail-closed scan of reports before upload

Exit codes: 0 ok | 1 test FAIL | 2 invalid input/config | 3 sensitive data withheld a report | 4 blocked (with --fail-on-blocked)
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from qa_agent import discovery as disc                                   # noqa: E402
from qa_agent import drafts as drafts_mod                                # noqa: E402
from qa_agent import reporting                                           # noqa: E402
from qa_agent.approvals import ApprovalStore, promote_draft              # noqa: E402
from qa_agent.errors import AgentError, ApprovalError, SafetyBlocked, SchemaError, SensitiveDataError, WorkflowFileError  # noqa: E402
from qa_agent.redaction import redact                                    # noqa: E402
from qa_agent.runner import RunConfig, Runner                            # noqa: E402
from qa_agent.safety import DEFAULT_ENV_FILE, get_environment, verify_environment  # noqa: E402
from qa_agent.schema import APPS                                         # noqa: E402
from qa_agent.workflows import APPROVED_DIR, DEFAULT_WORKFLOWS, DRAFTS_DIR, load_all, load_workflows  # noqa: E402

ART = Path(os.getenv("ARTIFACTS_DIR", ROOT / "artifacts"))
LEGACY_URLS = {"nurseconnect": ("NURSECONNECT_URL", "https://nurseconnect.co.in"),
               "hospital": ("HOSPITAL_URL", "http://hospitaldashboard-env.eba-n7anfees.ap-south-1.elasticbeanstalk.com")}


def say(msg=""):
    print(redact(msg))


def kv_pairs(items, what):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise SystemExit(f"{what} expects APP=VALUE, got {it!r}")
        k, v = it.split("=", 1)
        if k not in APPS:
            raise SystemExit(f"{what}: unknown app {k!r}; use one of {list(APPS)}")
        out[k] = v
    return out


def cmd_validate(a):
    files = [Path(f) for f in a.file] if a.file else [DEFAULT_WORKFLOWS] + sorted(APPROVED_DIR.glob("*.yml")) if APPROVED_DIR.is_dir() else [DEFAULT_WORKFLOWS]
    problems = 0
    for f in files:
        try:
            wfs = load_workflows(f, draft_rules=a.drafts)
            say(f"OK    {f}  ({len(wfs)} workflow(s))")
        except (SchemaError, WorkflowFileError) as e:
            problems += 1
            say(f"ERROR {f}")
            for p in (e.problems if isinstance(e, SchemaError) else [str(e)]):
                say(f"        - {p}")
    if not (a.drafts or a.file):
        try:
            load_all()
        except SchemaError as e:
            problems += 1
            say("ERROR cross-file check: " + "; ".join(e.problems))
        audit = ApprovalStore().verify_audit()
        for p in audit:
            problems += 1
            say(f"ERROR approvals audit log: {p}")
        try:
            ApprovalStore().records()
        except ApprovalError as e:
            problems += 1
            say(f"ERROR {e}")
    if a.environments:
        try:
            from qa_agent.safety import load_environments
            envs = load_environments(a.environments if isinstance(a.environments, str) else None)
            say(f"OK    environments: {', '.join(f'{n}({e.kind})' for n, e in envs.items())}")
        except SafetyBlocked as e:
            problems += 1
            say(f"ERROR {e}")
    return 2 if problems else 0


def cmd_discover(a):
    pairs = kv_pairs(a.app, "--app")
    if not pairs:
        raise SystemExit("give at least one --app APP=PATH (path to the CHECKED-OUT application repo)")
    bases = kv_pairs(a.base_url, "--base-url")
    out_root = Path(a.out)
    rc = 0
    for app, path in pairs.items():
        try:
            result = disc.analyze_repo(path, app)
        except FileNotFoundError as e:
            say(f"ERROR {e}")
            rc = 2
            continue
        browser = None
        if a.inspect:
            if app not in bases:
                say(f"NOTE  {app}: --inspect needs --base-url {app}=URL; skipping browser inspection")
            else:
                env = get_environment(a.env) if a.env else None
                concrete = [r["route"] for r in result["routes"] if r["protected"] is not True]
                browser = disc.inspect_routes(bases[app], concrete, max_pages=a.max_pages, env=env)
        paths = disc.write_discovery(result, out_root / app, browser)
        s = result["stats"]
        say(f"{app}: {s['routes']} routes, {s['forms']} forms, {s['controls']} controls, {s['validation_messages']} validation messages; "
            f"{s['files_analysed']} files analysed, {s['files_skipped']} skipped -> {paths['json']}")
        if a.strict and any("secret-like" in x["reason"] for x in result["skipped"]):
            say(f"ERROR {app}: files with secret-like content were found (see skipped list); failing because of --strict")
            rc = 3
    return rc


def cmd_draft(a):
    src = Path(a.discovery)
    apps = [a.only] if a.only else list(APPS)
    total = 0
    for app in apps:
        f = src / app / "discovery.json"
        if not f.is_file():
            say(f"SKIP  {app}: {f} not found (run discover first)")
            continue
        result = json.loads(f.read_text(encoding="utf-8"))
        wfs, skipped = drafts_mod.generate_drafts(result)
        info = drafts_mod.save_drafts(wfs, skipped, app, drafts_dir=Path(a.out))
        total += len(info["written"])
        say(f"{app}: {len(info['written'])} draft(s) written to {Path(a.out) / app}; {len(info['skipped'])} item(s) skipped; "
            f"{len(info['unchanged_approved'])} already approved")
    say("All drafts have status: draft and CANNOT run until approved with `python agent.py approve`.")
    return 0


def cmd_approve(a):
    store = ApprovalStore()
    try:
        if a.draft:
            ids = promote_draft(a.draft, store=store, by=a.by, reason=a.reason, confirm_writes=a.i_understand_this_writes)
        else:
            wfs = {w["id"]: w for w in load_all()}
            if a.id not in wfs:
                raise ApprovalError(f"workflow {a.id!r} not found in workflows.yml / workflows_approved/")
            w = wfs[a.id]
            if w["status"] != "approved":
                raise ApprovalError(f"{a.id!r} has status {w['status']!r}; only workflows marked status: approved can be approved in place")
            store.approve(w, by=a.by, reason=a.reason, confirm_writes=a.i_understand_this_writes)
            ids = [a.id]
    except (AgentError, SystemExit) as e:
        say(f"REFUSED: {e}")
        return 2
    say(f"Approved: {', '.join(ids)} (recorded in approvals/approvals.json and approvals/audit.jsonl). Commit these files so CI sees the approval.")
    return 0


def cmd_revoke(a):
    try:
        ApprovalStore().revoke(a.id, by=a.by, reason=a.reason)
    except AgentError as e:
        say(f"REFUSED: {e}")
        return 2
    say(f"Revoked {a.id}")
    return 0


TEXT_EXT = {".json", ".md", ".html", ".htm", ".txt", ".log", ".yml", ".yaml", ".csv"}


def cmd_check_approvals(a):
    """CI approval gate: fail if any workflow claims to be approved without a valid approval record."""
    store = ApprovalStore()
    bad = 0
    try:
        wfs = load_all()
        store.records()
    except (SchemaError, WorkflowFileError, ApprovalError) as e:
        say(f"ERROR: {e}")
        return 2
    for p in store.verify_audit():
        say(f"ERROR audit log: {p}")
        bad += 1
    for w in wfs:
        state, detail = store.state(w)
        if state in ("unapproved", "changed"):
            bad += 1
            say(f"GATE  {w['id']}: {state} - {detail}")
        else:
            say(f"ok    {w['id']}: {state}")
    drafts = list(Path(a.drafts_dir).glob("*/*.yml")) if Path(a.drafts_dir).is_dir() else []
    say(f"{len(drafts)} draft file(s) in {a.drafts_dir} - drafts are never executed.")
    if bad:
        say(f"{bad} problem(s): approve the workflows (python agent.py approve ...), or set status: draft, then commit approvals/.")
        return 2
    return 0


def cmd_scan_artifacts(a):
    """Redact (with --fix) and re-scan every text artifact. Non-zero exit = do NOT upload."""
    from qa_agent.redaction import find_personal, find_secrets, redact as _redact
    root = Path(a.dir)
    dirty = []
    for f in sorted(root.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_EXT or f.stat().st_size > 20_000_000:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        if find_secrets(text) or find_personal(text):
            if a.fix:
                new = _redact(text)
                if new != text:
                    f.write_text(new, encoding="utf-8")
                text = new
            if find_secrets(text):                   # secrets that survive redaction: fail closed
                dirty.append(str(f.relative_to(root)))
    if dirty:
        say("SENSITIVE DATA could not be removed from: " + ", ".join(dirty))
        return 3
    say(f"Artifact scan clean: {root}")
    return 0


def cmd_inventory(a):
    store = ApprovalStore()
    wfs = load_all()
    rows = reporting.build_inventory(wfs, [], store, Path(a.drafts_dir))
    say(f"{'id':48} {'app':13} {'approval':11} {'risk':7} writes")
    for r in rows:
        say(f"{r['id'][:48]:48} {r['app']:13} {r['approval']:11} {r['risk']:7} {r['writes']}")
    return 0


def _make_env(a):
    name = a.env or os.getenv("TEST_ENV", "")
    if not name:
        return None
    return get_environment(name, a.env_file)


def cmd_verify_env(a):
    try:
        env = get_environment(a.env, a.env_file)
        say(f"Environment {env.name!r}: kind={env.kind}, allowed origins={list(env.allowed_origins)}")
        if env.kind == "production":
            say("Production is read-only by definition: writes can never be enabled for it.")
            return 0
        verify_environment(env, a.app)
        say("OK   verification probe passed" if env.verify else "NOTE no verification probe configured (allowlist only)")
        return 0
    except SafetyBlocked as e:
        say(f"BLOCKED: {e}")
        return 2


def cmd_run(a):
    try:
        env = _make_env(a)
    except SafetyBlocked as e:
        say(f"BLOCKED (fail closed): {e}")
        if a.env or os.getenv("TEST_ENV"):
            return 2
        env = None
    apps = tuple(APPS) if a.app == "all" else (a.app,)
    base_urls = {app: os.getenv(*LEGACY_URLS[app]) if os.getenv(LEGACY_URLS[app][0]) else LEGACY_URLS[app][1] for app in apps}
    if env:
        for app in apps:
            if app in env.base_urls:
                base_urls[app] = env.base_urls[app]
    base_urls.update(kv_pairs(a.base_url, "--base-url"))
    mode = "dry-run" if a.dry_run else "read-only" if a.read_only else "run"
    allow = a.allow_write or os.getenv("ALLOW_WRITE_TESTS", "").strip().lower() in ("1", "true", "yes")
    try:
        wfs = load_all([Path(f) for f in a.file] if a.file else None)
    except (SchemaError, WorkflowFileError) as e:
        say("INVALID workflows - nothing was run:")
        for p in (e.problems if isinstance(e, SchemaError) else [str(e)]):
            say(f"  - {p}")
        return 2
    cfg = RunConfig(apps=apps, env=env, mode=mode, allow_write=allow, confirm_phrase=os.getenv("STAGING_WRITE_CONFIRMATION", ""),
                    confirmed_workflows=tuple(a.confirm_write or ()), only=tuple(a.workflow or ()), base_urls=base_urls,
                    headless=not a.headed, browser=a.browser, artifacts_dir=ART, run_id=os.getenv("RUN_ID", ""))
    runner = Runner(cfg)
    results = runner.run(wfs)
    inv = reporting.build_inventory(wfs, results, runner.store, Path(a.drafts_dir))
    try:
        paths = reporting.write_reports(results, runner.meta, Path(a.report_dir) if a.report_dir else ART / "reports", inv)
    except SensitiveDataError as e:
        say(f"REPORT WITHHELD (fail closed): {e}")
        return 3
    s = reporting.summarize(results)
    say(f"Run {runner.run_id} [{mode}] env={runner.meta['environment']}: " + ", ".join(f"{k}={s[k]}" for k in reporting.STATUS_ORDER))
    for r in results:
        say(f"  {r.status:12} {r.id}" + (f"  - {r.reason}" if r.reason and r.status != "PASS" else ""))
    say(f"Reports: {paths['html']}")
    if s["FAIL"]:
        return 1
    if a.fail_on_blocked and s["BLOCKED"]:
        return 4
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="agent.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="validate workflow YAML + approvals (offline)")
    v.add_argument("--file", action="append")
    v.add_argument("--drafts", action="store_true", help="apply the strict generated-draft rules")
    v.add_argument("--environments", nargs="?", const=True, help="also validate config/environments.yml (optionally give a path)")
    v.set_defaults(fn=cmd_validate)

    d = sub.add_parser("discover", help="read-only discovery of checked-out application repos")
    d.add_argument("--app", action="append", help="APP=PATH, e.g. nurseconnect=apps/nurseconnect")
    d.add_argument("--out", default=str(ART / "discovery"))
    d.add_argument("--inspect", action="store_true", help="also do GET-only browser inspection (needs --base-url)")
    d.add_argument("--base-url", action="append", help="APP=URL for --inspect")
    d.add_argument("--env", help="environment name (for production-origin awareness during inspection)")
    d.add_argument("--max-pages", type=int, default=25)
    d.add_argument("--strict", action="store_true", help="fail if any file had to be skipped for secret-like content")
    d.set_defaults(fn=cmd_discover)

    g = sub.add_parser("draft", help="generate DRAFT workflows from discovery output")
    g.add_argument("--discovery", default=str(ART / "discovery"))
    g.add_argument("--out", default=str(DRAFTS_DIR))
    g.add_argument("--only", choices=list(APPS))
    g.set_defaults(fn=cmd_draft)

    ap = sub.add_parser("approve", help="explicitly approve a draft file or an existing workflow")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--draft")
    grp.add_argument("--id")
    ap.add_argument("--by", required=True)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--i-understand-this-writes", action="store_true")
    ap.set_defaults(fn=cmd_approve)

    rv = sub.add_parser("revoke", help="revoke an approval")
    rv.add_argument("--id", required=True)
    rv.add_argument("--by", required=True)
    rv.add_argument("--reason", required=True)
    rv.set_defaults(fn=cmd_revoke)

    iv = sub.add_parser("inventory", help="list workflows with approval state")
    iv.add_argument("--drafts-dir", default=str(DRAFTS_DIR))
    iv.set_defaults(fn=cmd_inventory)

    ca = sub.add_parser("check-approvals", help="CI approval gate (fails on unapproved / changed workflows)")
    ca.add_argument("--drafts-dir", default=str(DRAFTS_DIR))
    ca.set_defaults(fn=cmd_check_approvals)

    sa = sub.add_parser("scan-artifacts", help="redact + re-scan text artifacts before upload")
    sa.add_argument("dir")
    sa.add_argument("--fix", action="store_true", help="redact in place first")
    sa.set_defaults(fn=cmd_scan_artifacts)

    ve = sub.add_parser("verify-env", help="check an environment definition (and its probe)")
    ve.add_argument("--env", required=True)
    ve.add_argument("--app", required=True, choices=list(APPS))
    ve.add_argument("--env-file")
    ve.set_defaults(fn=cmd_verify_env)

    r = sub.add_parser("run", help="run APPROVED workflows")
    r.add_argument("--app", choices=list(APPS) + ["all"], default="all")
    r.add_argument("--env", help="environment from config/environments.yml (or TEST_ENV)")
    r.add_argument("--env-file")
    r.add_argument("--workflow", action="append", help="run only this id (repeatable)")
    r.add_argument("--file", action="append", help="use these workflow files instead of workflows.yml + workflows_approved/")
    r.add_argument("--base-url", action="append", help="APP=URL override")
    r.add_argument("--dry-run", action="store_true", help="evaluate every gate, launch no browser")
    r.add_argument("--read-only", action="store_true", help="skip every workflow that writes")
    r.add_argument("--allow-write", action="store_true", help="needed (with the other gates) for workflows that create test records")
    r.add_argument("--confirm-write", action="append", help="workflow id explicitly confirmed for writing (repeatable)")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    r.add_argument("--fail-on-blocked", action="store_true")
    r.add_argument("--report-dir")
    r.add_argument("--drafts-dir", default=str(DRAFTS_DIR))
    r.set_defaults(fn=cmd_run)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (SchemaError, WorkflowFileError) as e:
        say(f"ERROR: {e}")
        return 2
    except AgentError as e:
        say(f"ERROR: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
