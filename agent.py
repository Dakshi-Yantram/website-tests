#!/usr/bin/env python3
"""NurseConnect QA agent command line.

  python agent.py validate                       check workflows.yml + approvals (no browser, no network)
  python agent.py discover --app nurseconnect=apps/nurseconnect --app hospital=apps/hospital
  python agent.py draft                          generate DRAFT workflows from the latest discovery
  python agent.py approve --draft FILE --by NAME --reason TEXT      (human decision, auditable)
  python agent.py approve --id ID --by NAME --reason TEXT           (approve a workflow already in workflows.yml)
  python agent.py revoke --id ID --by NAME --reason TEXT
  python agent.py inventory                      draft / approved / unapproved / changed + last result
  python agent.py auto --app hospital            AUTONOMOUS: logs in, explores, tests, writes a bug report (no workflow files needed)
  python agent.py run --env NAME [--dry-run | --read-only] [--app A] [--workflow ID]
  python agent.py verify-env --env NAME --app A
  python agent.py preflight --env NAME --app A [--online] [--source frontend=PATH --source backend=PATH] [--json]
  python agent.py check-approvals                CI approval gate
  python agent.py scan-artifacts DIR [--fix]     redact + fail-closed scan of reports before upload

Exit codes: 0 ok | 1 test FAIL (auto: a bug at/above --fail-on) | 2 invalid input/config | 3 sensitive data withheld a report | 4 blocked by credentials/environment (auto: nothing could be tested) | 5 incomplete | 6 critical NOT VERIFIED
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
        try:
            from qa_agent import verdict
            for p in verdict.check_known_ids(load_all()):
                problems += 1
                say(f"ERROR config/critical_workflows.yml: {p}")
        except verdict.CriticalConfigError as e:
            problems += 1
            say(f"ERROR config/critical_workflows.yml: {e}")
        except SchemaError:
            pass     # already reported by the cross-file check above
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


def cmd_compile_spec(a):
    """Compile one application's semantic YAML to approval-gated executable drafts."""
    from qa_agent.spec_compiler import compile_package, read_specs, app_for
    from qa_agent.spec_evidence import source_hints, staging_ui_hints
    try:
        specs = read_specs(a.spec)
        chosen = [x for x in specs if not a.only or x[1]['id'] in a.only]
        if not chosen:
            say("No matching specification IDs")
            return 2
        doc_apps = {app_for(spec) for _, spec in chosen}
        if len(doc_apps) != 1:
            say("COMPILATION BLOCKED: compile one application at a time; use --app and separate app-specific documents")
            return 2
        app = next(iter(doc_apps))
        if a.app and a.app != app:
            say(f"COMPILATION BLOCKED: document belongs to {app}, but --app={a.app}; no source was inspected")
            return 2
        # A path is never inferred from the presence of two repos. The operator
        # explicitly binds a document app to its checked-out source directory.
        repo = None
        if a.repo:
            if '=' in a.repo:
                repo_app, repo = a.repo.split('=', 1)
                if repo_app != app or not repo:
                    say(f"COMPILATION BLOCKED: source binding {a.repo!r} does not match document application {app}")
                    return 2
            elif app == 'hospital':
                repo = a.repo  # compatibility with the original HIMS --repo PATH command
            else:
                say('COMPILATION BLOCKED: Nurse Connect requires --repo nurseconnect=PATH to prevent selecting HIMS accidentally')
                return 2
        if not repo:
            # Both repositories are checked out by the GitHub App discovery job.
            # Bind automatically from the *document application's key*; never
            # infer from source contents or pick a random repository.
            default_repo = ROOT / 'apps' / app
            if default_repo.is_dir():
                repo = str(default_repo)
            else:
                say(f"COMPILATION BLOCKED: source for {app} is not checked out at apps/{app}; provide --repo {app}=PATH")
                return 2
        source = source_hints(repo, chosen, app=app)
        browser = {}
        if a.inspect:
            if not a.env:
                say('--inspect requires --env with a declared staging/local deployment')
                return 2
            env = get_environment(a.env, a.env_file)
            browser = staging_ui_hints(chosen, env=env, app=app, limit=a.max_pages)
        out_dir = Path(a.out) if a.out else DRAFTS_DIR / app
        from qa_agent import provenance
        report = compile_package(a.spec, out_dir, model=a.model, only=a.only,
                                 source_lookup=source, browser_lookup=browser,
                                 refresh=a.refresh,
                                 provenance={app: provenance.git_info(app, repo)})
    except (AgentError, ValueError, OSError, RuntimeError) as e:
        say(f"COMPILATION BLOCKED: {redact(str(e))}")
        return 2
    summary = {}
    for x in report['results']:
        summary[x['state']] = summary.get(x['state'], 0) + 1
    say(f"Compiled {app} specifications: {report['selected']} selected; states={summary}")
    say(f"Unverified business checks: {sum(x.get('needs_verification', 0) for x in report['results'])}; "
        f"compiler warnings: {sum(bool(x.get('compile_issue')) for x in report['results'])}")
    for x in report['results']:
        if x['state'] != 'DRAFT':
            say(f"  {x['state']} {x['id']}: {x.get('problems', [])}")
    say(f"Drafts and compilation_report.json: {out_dir}")
    say('NOT EXECUTED. Review drafts, approve, and run against verified staging.')
    return 2 if any(x['state'] != 'DRAFT' for x in report['results']) else 0

def cmd_approve_batch(a):
    """Human explicitly approves a collection of drafts, without repeated manual selector editing."""
    from qa_agent.workflows import load_yaml_file
    paths = sorted(Path(a.dir).glob('spec_*.yml'))
    if not paths:
        say('No spec_*.yml drafts found')
        return 2
    approved, rejected = [], []
    store = ApprovalStore()
    for f in paths:
        doc = load_yaml_file(f)
        wf = doc.get('workflows', [{}])[0]
        if not a.include_unverified and any(s.get('action') == 'require_verified' for s in wf.get('steps', [])):
            rejected.append((f.name, 'missing test assertions; --include-unverified to approve partial tests'))
            continue
        try:
            ids = promote_draft(f, store=store, by=a.by, reason=a.reason,
                                confirm_writes=a.i_understand_this_writes)
            approved.extend(ids)
        except (AgentError, ValueError, OSError) as e:
            rejected.append((f.name, redact(str(e))))
    say(f"Batch approved: {len(approved)}; not approved: {len(rejected)}")
    for name, reason in rejected[:15]:
        say(f"  {name}: {reason}")
    return 2 if rejected else 0


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


def _collect_sources(a):
    """--source NAME=PATH (+ QA_SOURCE_<NAME> env vars) -> provenance records, or {} when none were given."""
    from qa_agent import provenance
    try:
        wanted = provenance.parse_source_args(getattr(a, "source", None))
    except ValueError as e:
        raise SystemExit(str(e))
    return provenance.collect(wanted)


def cmd_preflight(a):
    """Verify a staging environment BEFORE running tests. Nothing is written to the application."""
    from qa_agent import preflight
    try:
        env = get_environment(a.env, a.env_file)
    except SafetyBlocked as e:
        say(f"BLOCKED: {e}")
        return 2
    sources = _collect_sources(a)
    checks = preflight.run_preflight(env, a.app, online=a.online, sources=sources or None)
    if a.json:
        say(json.dumps({"environment": env.name, "app": a.app, "ready": preflight.is_ready(checks),
                        "checks": [c.as_dict() for c in checks], "sources": sources}, indent=2))
    else:
        say(f"Preflight for environment {env.name!r} (kind={env.kind}), app {a.app}, {'online' if a.online else 'offline'}")
        say(preflight.format_checks(checks))
    return 0 if preflight.is_ready(checks) else 2


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
        if a.spec:
            # The document is authoritative: do not run stale approved tests from an old revision.
            import hashlib
            from qa_agent.spec_compiler import read_specs, _make_id, app_for
            srclist = read_specs(a.spec)
            spec_apps = {app_for(spec) for _, spec in srclist}
            if a.app != 'all' and spec_apps != {a.app}:
                raise WorkflowFileError(f'--spec belongs to {sorted(spec_apps)}, but --app={a.app}; no workflows executed')
            wanted = {x['id']: (hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=True).encode('utf-8')).hexdigest(), _make_id(x))
                      for _, x in srclist if not a.workflow or _make_id(x) in a.workflow or x['id'] in a.workflow}
            if not wanted:
                raise WorkflowFileError('No HIMS specification IDs match the requested --workflow filters')
            indexed = {w['id']: w for w in wfs}
            problems = []
            for sid, (hash_now, wid) in wanted.items():
                wf = indexed.get(wid)
                if wf is None:
                    problems.append(f'{sid}: no approved executable workflow found; compile-spec and approve-batch first')
                elif wf.get('app') != app_for(next(spec for _, spec in srclist if spec['id'] == sid)):
                    problems.append(f'{sid}: compiled workflow app does not match the source document')
                elif wf.get('generated', {}).get('source_sha256') != hash_now:
                    problems.append(f'{sid}: source document changed after approval; recompile and reapprove')
            if problems:
                say('SPECIFICATION GATE BLOCKED - no tests executed:')
                for issue in problems[:25]:
                    say('  - ' + issue)
                if len(problems) > 25:
                    say(f'  ... {len(problems) - 25} more missing or stale workflows')
                return 2
            wfs = [indexed[wid] for _, wid in wanted.values()]
    except (SchemaError, WorkflowFileError) as e:
        say("INVALID workflows - nothing was run:")
        for p in (e.problems if isinstance(e, SchemaError) else [str(e)]):
            say(f"  - {p}")
        return 2
    selection = tuple(a.workflow or ())
    if a.spec and selection:
        from qa_agent.spec_compiler import _make_id
        selection = tuple(_make_id({'id': wid}) if not wid.startswith('spec_') else wid for wid in selection)
    cfg = RunConfig(apps=apps, env=env, mode=mode, allow_write=allow, confirm_phrase=os.getenv("STAGING_WRITE_CONFIRMATION", ""),
                    confirmed_workflows=tuple(a.confirm_write or ()), only=selection, base_urls=base_urls,
                    headless=not a.headed, browser=a.browser, artifacts_dir=ART, run_id=os.getenv("RUN_ID", ""),
                    reuse_sessions=not getattr(a, "no_session_reuse", False))
    runner = Runner(cfg)
    runner.meta["sources"] = _collect_sources(a)
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
    from qa_agent import verdict
    return verdict.finish(results, wfs, say=say, fail_on_blocked=a.fail_on_blocked,
                          fail_on_incomplete=getattr(a, 'fail_on_incomplete', False),
                          fail_on_critical_unverified=getattr(a, 'fail_on_critical_unverified', False),
                          ignore_blocked=tuple(k for k in (getattr(a, 'ignore_blocked', '') or '').split(',') if k),
                          apps=apps, selection=selection, placeholders_as_gaps=getattr(a, 'placeholders_as_gaps', False))


def cmd_auto(a):
    """Autonomous read-only exploration: log in, discover, test, report. Needs only URLs (defaults are built in) and credentials in .env."""
    from qa_agent import explore_report
    from qa_agent.explorer import Explorer, ExploreConfig, SEV_RANK
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
    use_ai = bool(getattr(a, "ai", False))
    Cfg = ExploreConfig
    ai_kw = {}
    if use_ai:
        from qa_agent.llm_explorer import AIExploreConfig, LLMExplorer
        Cfg = AIExploreConfig
        ai_kw = dict(model=a.model or "", ai_steps=a.ai_steps, ai_page_text=a.ai_page_text)
        if a.ai_goal:
            ai_kw["ai_goal"] = a.ai_goal
    cfg = Cfg(apps=apps, base_urls=base_urls, env=env, roles=tuple(a.role or ()), seed_paths=tuple(a.seed or ()),
                        **ai_kw,
                        max_pages=a.max_pages, max_minutes=a.max_minutes, headless=not a.headed, browser=a.browser,
                        screenshots=a.screenshots, artifacts_dir=ART, run_id=os.getenv("RUN_ID", ""),
                        anonymous_check=not a.no_anonymous_check, cross_role_check=not a.no_cross_role_check,
                        allow_read_post=tuple(a.allow_read_post or ()), login_retries=a.login_retries,
                        fill=a.fill, video=a.video, confirm_phrase=os.getenv("STAGING_WRITE_CONFIRMATION", ""))
    if a.fill and not env:
        say("BLOCKED: --fill creates records and needs a declared staging environment: add --env <name> (see config/environments.yml)")
        return 2
    urls_txt = ", ".join(f"{k} ({v})" for k, v in base_urls.items())
    if a.fill:
        say(f"FILL MODE on a staging environment - records marked AUTOTEST_ will be created in: {urls_txt}")
        say("DELETE requests and payment endpoints are blocked. This can take several minutes...")
    else:
        say(f"Autonomous read-only exploration of: {urls_txt}")
    if not a.fill:
        say("Nothing is created, changed or deleted: all write requests are blocked. This can take several minutes...")
    try:
        result = (LLMExplorer(cfg) if use_ai else Explorer(cfg)).run()
    except SafetyBlocked as e:
        say(f"BLOCKED (fail closed): {e}")
        return 2
    except Exception as e:                                   # never end silently: show what stopped the run
        say(f"RUN STOPPED: {type(e).__name__}: {redact(str(e))[:400]}")
        return 5
    try:
        paths = explore_report.write_reports(result, Path(a.report_dir) if a.report_dir else ART / "reports", ART / "reports" / "screenshots")
    except SensitiveDataError as e:
        say(f"REPORT WITHHELD (fail closed): {e}")
        return 3
    head, hint, why = explore_report.verdict(result)
    counts = explore_report._sev_counts(result.findings)
    say(f"Run {result.meta['run_id']}: " + ", ".join(f"{counts[s]} {s}" for s in counts))
    for r in result.roles:
        say(f"  {r.app}/{r.role}: {r.status}, {r.pages} page(s)" + (f" - {r.reason}" if r.reason else ""))
    for f in result.findings[:15]:
        if f.severity in ("High", "Medium"):
            say(f"  {f.id} [{f.severity}] {f.title}")
    say(f"Reports: {paths['html']}")
    say(f"VERDICT: {head}")
    if hint == "blocked":
        say("Set the test-account variables in .env (see .env.example): e.g. RECEPTION_EMAIL / RECEPTION_PASSWORD for the hospital app.")
        return 4
    gate = SEV_RANK[a.fail_on.capitalize()]
    return 1 if any(SEV_RANK[f.severity] <= gate for f in result.findings) else 0


def cmd_login_check(a):
    """Diagnose a login that fails or times out: shows what the agent finds on the page, what it clicks, and every network call."""
    import config
    from urllib.parse import urlsplit
    from playwright.sync_api import sync_playwright
    from qa_agent.explorer import ExplorerGuard
    from qa_agent.safety import normalize_origin
    from pages.login_page import EMAIL_SEL, PASS_SEL, SUBMIT_SEL
    try:
        env = _make_env(a)
    except SafetyBlocked as e:
        say(f"BLOCKED (fail closed): {e}")
        return 2
    base = (env.base_urls.get(a.app) if env else None) or config.APPS[a.app].base_url
    role = config.ROLES.get(a.role) or config.AGENT_ROLES.get(a.role)
    if not role or not role.email or not role.password:
        say(f"No credentials for role '{a.role}': set its email and password variables in .env")
        return 4
    from qa_agent.redaction import register_secret
    register_secret(role.password)
    url = base.rstrip("/") + config.APPS[a.app].login_path
    say(f"Opening {url} as role '{a.role}' (read-only guard on; environment: {env.name if env else 'none'})")
    with sync_playwright() as pw:
        kw = {}
        if os.getenv("QA_CHROMIUM_PATH"):
            kw["executable_path"] = os.getenv("QA_CHROMIUM_PATH")
        br = pw.chromium.launch(headless=not a.headed, **kw)
        ctx = br.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
        guard = ExplorerGuard(env, normalize_origin(base), writes_enabled=False, auth_any_origin=True)
        guard.attach(ctx)
        pg = ctx.new_page()
        pg.on("request", lambda r: r.method != "GET" and say(f"  -> {r.method} {urlsplit(r.url).netloc}{urlsplit(r.url).path[:70]}"))
        pg.on("response", lambda r: r.request.method != "GET" and say(f"  <- {r.status} {r.request.method} {urlsplit(r.url).netloc}{urlsplit(r.url).path[:70]}"))
        pg.on("requestfailed", lambda r: say(f"  !! request failed: {r.method} {urlsplit(r.url).netloc}{urlsplit(r.url).path[:70]} ({r.failure})"))
        pg.on("console", lambda m: m.type == "error" and say(f"  console error: {m.text[:160]}"))
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(2500)
        say(f"Page title: {pg.title()!r}; final address: {urlsplit(pg.url).path}")
        for name, sel in (("email field", EMAIL_SEL), ("password field", PASS_SEL), ("submit button", SUBMIT_SEL)):
            loc = pg.locator(sel)
            n = loc.count()
            first = ""
            if n:
                try:
                    first = loc.first.evaluate("e => (e.tagName + ' type=' + (e.getAttribute('type')||'-') + ' text=' + (e.innerText||e.value||e.placeholder||'').trim().slice(0,40))")
                except Exception:
                    pass
            say(f"  {name}: {n} match(es); the agent would use the FIRST: {first}")
        try:
            pg.locator(EMAIL_SEL).first.fill(role.email)
            pg.locator(PASS_SEL).first.fill(role.password)
            pg.locator(SUBMIT_SEL).first.click(timeout=5000)
        except Exception as e:
            say(f"  could not fill/click: {type(e).__name__}: {str(e).splitlines()[0][:160]}")
        pg.wait_for_timeout(int(a.wait) * 1000)
        shown = pg.evaluate("() => Array.from(document.querySelectorAll('[role=alert],[class*=error i],[class*=toast i],[class*=alert i]')).map(e => e.innerText.trim()).filter(Boolean).slice(0,4)")
        say(f"Now at: {urlsplit(pg.url).path}; messages on screen: {shown}")
        say("Blocked by the agent's own guard: " + (", ".join(guard.blocked[:5]) if guard.blocked else "nothing"))
        br.close()
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

    sc = sub.add_parser("compile-spec", help="compile hospital or Nurse Connect semantic YAML into agent-executable reviewable drafts")
    sc.add_argument("--spec", required=True, help="app-specific semantic YAML, workflow directory or ZIP")
    sc.add_argument("--app", choices=list(APPS), help="must match the document's application")
    sc.add_argument("--out", help="optional output directory; default workflow_drafts/<application>")
    sc.add_argument("--repo", help="explicit APP=PATH source binding; legacy bare path supported for hospital only")
    sc.add_argument("--refresh", action="store_true", help="archive previous unapproved draft and recompile after document changes")
    sc.add_argument("--model", help="optional AI compiler model, e.g. qwen-plus (needs QA_LLM_API_KEY, optional QA_LLM_BASE_URL, and pip install openai)")
    sc.add_argument("--only", action="append", help="spec id, e.g. OPD-001 (repeatable)")
    sc.add_argument("--inspect", action="store_true", help="inspect staging UI controls with Playwright (never production)")
    sc.add_argument("--env", help="declared staging/local environment name")
    sc.add_argument("--env-file", help="optional environment YAML config path")
    sc.add_argument("--max-pages", type=int, default=12)
    sc.set_defaults(fn=cmd_compile_spec)

    ab = sub.add_parser("approve-batch", help="human batch approval of compiled drafts")
    ab.add_argument("--dir", default=str(DRAFTS_DIR / "hospital"))
    ab.add_argument("--by", required=True)
    ab.add_argument("--reason", required=True)
    ab.add_argument("--i-understand-this-writes", action="store_true")
    ab.add_argument("--include-unverified", action="store_true", help="also approve tests with missing assertions; report NOT VERIFIED")
    ab.set_defaults(fn=cmd_approve_batch)

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

    pf = sub.add_parser("preflight", help="verify staging URL, API URL, payment sandbox, database, credentials and source SHAs")
    pf.add_argument("--env", required=True)
    pf.add_argument("--app", required=True, choices=list(APPS))
    pf.add_argument("--env-file")
    pf.add_argument("--online", action="store_true", help="also run the declared verification probes (GET requests to the staging origins only)")
    pf.add_argument("--source", action="append", help="NAME=PATH of a checked-out repo (frontend, backend...); or QA_SOURCE_<NAME> env var")
    pf.add_argument("--json", action="store_true")
    pf.set_defaults(fn=cmd_preflight)

    ve = sub.add_parser("verify-env", help="check an environment definition (and its probe)")
    ve.add_argument("--env", required=True)
    ve.add_argument("--app", required=True, choices=list(APPS))
    ve.add_argument("--env-file")
    ve.set_defaults(fn=cmd_verify_env)

    au = sub.add_parser("auto", help="AUTONOMOUS read-only exploration: logs in as every configured role, explores, tests, writes a bug report")
    au.add_argument("--app", choices=list(APPS) + ["all"], default="all")
    au.add_argument("--role", action="append", help="only this role (repeatable); default: every role whose credentials are in .env / the environment")
    au.add_argument("--env", help="environment from config/environments.yml (or TEST_ENV); optional")
    au.add_argument("--env-file")
    au.add_argument("--base-url", action="append", help="APP=URL override")
    au.add_argument("--seed", action="append", help="extra start path to visit, e.g. /hospital-dashboard/admin/addstaff (repeatable)")
    au.add_argument("--max-pages", type=int, default=40, help="pages per role (default 40)")
    au.add_argument("--max-minutes", type=float, default=10.0, help="time budget per role (default 10)")
    au.add_argument("--headed", action="store_true", help="show the browser")
    au.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    au.add_argument("--screenshots", choices=["auto", "off", "masked"], default="auto",
                    help="auto = masked on declared staging/local, off otherwise (patient-data policy); masked = force masked screenshots")
    au.add_argument("--fail-on", choices=["high", "medium", "low", "info"], default="high", help="exit 1 when a finding of this severity or worse exists (default high)")
    au.add_argument("--no-anonymous-check", action="store_true", help="skip the 'can a logged-out visitor open this page' check")
    au.add_argument("--no-cross-role-check", action="store_true", help="skip the 'can another role open this page' check")
    au.add_argument("--allow-read-post", action="append", metavar="REGEX",
                    help="path regex of POST endpoints YOU know are read-only (list/search). Default: every non-GET request is blocked. Repeatable")
    au.add_argument("--video", action="store_true",
                    help="record a video of each role's session (staging/local only); saved next to the report in a 'videos' folder")
    au.add_argument("--fill", action="store_true",
                    help="STAGING ONLY: also fill forms with synthetic AUTOTEST_ data and save them. Needs --env <staging>, STAGING_WRITE_CONFIRMATION, and passes the same write gate as `run --allow-write`")
    au.add_argument("--login-retries", type=int, default=1, help="extra login attempts after an HTTP 5xx (default 1; 0 = none). Rejected passwords are never retried")
    au.add_argument("--report-dir")
    au.add_argument("--ai", action="store_true",
                    help="AI-DRIVEN: a model (Qwen etc.) decides what to click/type next; code validates every action and judges the results. "
                         "Needs --env <staging/local>, QA_LLM_API_KEY, --model (or QA_LLM_MODEL) and pip install openai")
    au.add_argument("--model", help="model name for --ai, e.g. qwen-plus (host from QA_LLM_BASE_URL)")
    au.add_argument("--ai-steps", type=int, default=40, help="model decisions per role (default 40)")
    au.add_argument("--ai-goal", help="what the AI tester should focus on, in plain English")
    au.add_argument("--ai-page-text", action="store_true", help="also send a short redacted text snippet of each page to the model (default: labels and headings only)")
    au.set_defaults(fn=cmd_auto)

    lc = sub.add_parser("login-check", help="diagnose a failing login: shows the fields/button the agent finds and every network call")
    lc.add_argument("--app", choices=list(APPS), required=True)
    lc.add_argument("--role", required=True, help="e.g. nurse or consumer")
    lc.add_argument("--env")
    lc.add_argument("--env-file")
    lc.add_argument("--headed", action="store_true")
    lc.add_argument("--wait", type=int, default=15, help="seconds to watch after clicking Login")
    lc.set_defaults(fn=cmd_login_check)

    r = sub.add_parser("run", help="run APPROVED workflows")
    r.add_argument("--app", choices=list(APPS) + ["all"], default="all")
    r.add_argument("--env", help="environment from config/environments.yml (or TEST_ENV)")
    r.add_argument("--env-file")
    r.add_argument("--workflow", action="append", help="run only this id (repeatable)")
    r.add_argument("--file", action="append", help="use these workflow files instead of workflows.yml + workflows_approved/")
    r.add_argument("--spec", help="app-specific semantic ZIP/YAML/folder; run ONLY matching current approved workflows")
    r.add_argument("--base-url", action="append", help="APP=URL override")
    r.add_argument("--dry-run", action="store_true", help="evaluate every gate, launch no browser")
    r.add_argument("--read-only", action="store_true", help="skip every workflow that writes")
    r.add_argument("--allow-write", action="store_true", help="needed (with the other gates) for workflows that create test records")
    r.add_argument("--confirm-write", action="append", help="workflow id explicitly confirmed for writing (repeatable)")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--browser", choices=["chromium", "firefox", "webkit"], default="chromium")
    r.add_argument("--no-session-reuse", action="store_true", help="log in separately for every workflow (default: one login per role per run)")
    r.add_argument("--source", action="append", help="NAME=PATH of a checked-out repo whose git commit SHA is recorded in the report (repeatable; or QA_SOURCE_<NAME> env var)")
    r.add_argument("--fail-on-blocked", action="store_true")
    r.add_argument("--fail-on-incomplete", action="store_true", help="exit nonzero when NOT VERIFIED or NOT TESTED workflows remain")
    r.add_argument("--ignore-blocked", default="", help="comma list of block kinds --fail-on-blocked tolerates: credentials,unapproved,policy,other (default: none)")
    r.add_argument("--fail-on-critical-unverified", action="store_true", help="exit 6 when a critical workflow (config/critical_workflows.yml or risk high) is NOT VERIFIED")
    r.add_argument("--placeholders-as-gaps", action="store_true",
                   help="workflows whose only non-PASS step is a 'require_verified' placeholder (compiled from a spec with no selectors) are reported as a COVERAGE GAP "
                        "instead of failing the critical gate. They still verify nothing - use `agent.py auto` for real coverage")
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