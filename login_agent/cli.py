import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
try:                                              # .env is optional: without python-dotenv, use real environment variables
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass

from . import __version__                                                # noqa: E402
from .errors import SafetyBlocked, SensitiveDataError                    # noqa: E402
from .redaction import redact                                            # noqa: E402
from .plan import PlanError, load_plan                                   # noqa: E402
from .report import exit_code, summarize, write_reports                  # noqa: E402
from .runner import gate, resolve_target, run_suite                      # noqa: E402


def _args(argv):
    p = argparse.ArgumentParser(prog="python -m login_agent", description="Run the NurseConnect login test plan with Playwright.")
    p.add_argument("--env", default=os.getenv("LOGIN_AGENT_ENV", "production"),
                   help="environment name from config/environments.yml (default: production = read-only, anonymous tests only)")
    p.add_argument("--env-file", default=os.getenv("QA_ENV_FILE") or None, help="alternative environments.yml")
    p.add_argument("--plan", default=None, help="test plan YAML (default login_tests/login-test-plan.yaml)")
    p.add_argument("--fake-id", default=None, help="folder with test-identities.yaml and test-data.yaml (default fake-id/)")
    p.add_argument("--base-url", default="", help="override the environment's base URL (must still belong to that environment)")
    p.add_argument("--only", default="", help="comma list of test ids, e.g. LOGIN-001,LOGIN-003")
    p.add_argument("--skip", default="", help="comma list of test ids to skip")
    p.add_argument("--tags", default="", help="comma list of tags, e.g. smoke")
    p.add_argument("--headed", action="store_true", default=os.getenv("HEADLESS", "true").lower() in ("0", "false", "no"),
                   help="show the browser window")
    p.add_argument("--slow-mo", type=int, default=int(os.getenv("SLOW_MO_MS", "0")))
    p.add_argument("--screenshots", choices=("masked", "off", "full"), default=os.getenv("LOGIN_SCREENSHOT_MODE", "masked"),
                   help="failed-test screenshots: masked (inputs hidden, default) | off | full (staging/local only, never in CI)")
    p.add_argument("--out", default="", help="output folder (default artifacts/login/<run id>)")
    p.add_argument("--dry-run", action="store_true", help="validate the plan and show what would run; start no browser")
    p.add_argument("--list", action="store_true", help="list the tests and exit")
    p.add_argument("--fail-on", choices=("any", "low", "medium", "high", "critical"), default="any",
                   help="exit 1 only for FAILs of at least this severity (default: any FAIL)")
    p.add_argument("--strict", action="store_true", help="also exit 1 when a test is BLOCKED")
    return p.parse_args(argv)


def _csv(s):
    return [x.strip() for x in s.split(",") if x.strip()]


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    a = _args(argv)
    try:
        plan = load_plan(a.plan, a.fake_id)
        if a.list:
            for t in plan.tests:
                print(f"{t.id}  [{t.severity:<8}] {t.requires:<9} {t.title}")
            return 0
        tgt = resolve_target(a.env, a.env_file, a.base_url)
    except (PlanError, SafetyBlocked) as e:
        print(f"CONFIG/SAFETY ERROR: {redact(str(e))}", file=sys.stderr)
        return 2

    run_id = os.getenv("RUN_ID") or datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(a.out) if a.out else Path(os.getenv("ARTIFACTS_DIR", ROOT / "artifacts")) / "login" / run_id
    print(f"NurseConnect login agent {__version__} | environment {tgt.env_name} ({tgt.env_kind}) | target {tgt.base_origin}")
    if tgt.env_kind == "production":
        print("PRODUCTION: read-only. Every non-GET request is blocked; login/credential tests are reported NOT TESTED.")
    started = datetime.now().isoformat(timespec="seconds")
    log = lambda m: print(redact(m))                                      # noqa: E731
    results = run_suite(plan, tgt, only=_csv(a.only), tags=_csv(a.tags), skip=_csv(a.skip), headed=a.headed, slow_mo=a.slow_mo,
                        screenshot_mode=a.screenshots, out_dir=out, dry_run=a.dry_run, log=log)
    if a.dry_run:
        for r in results:
            print(f"{'WILL RUN' if not r.reason or r.reason == 'dry run' else r.status:<10} {r.id}  {r.title}" +
                  (f"\n           -> {r.reason}" if r.reason and r.reason != "dry run" else ""))
        print("Dry run: no browser was started, nothing was sent.")
        return 0
    for r in results:                       # tests that never reached the browser are listed too, with the reason
        if r.status in ("NOT TESTED", "BLOCKED") and not r.duration_ms:
            print(redact(f"{r.status:<10} {r.id}  {r.title}\n           -> {r.reason}"))
    s = summarize(results)
    print("\nSUMMARY: " + ", ".join(f"{s[k]} {k}" for k in ("PASS", "FAIL", "BLOCKED", "NOT TESTED") if s[k]) + f"  (of {s['total']})")
    meta = {"suite": plan.suite, "environment": tgt.env_name, "env_kind": tgt.env_kind, "base_origin": tgt.base_origin,
            "run_id": run_id, "started": started, "agent_version": __version__}
    try:
        paths = write_reports(results, meta, out)
    except SensitiveDataError as e:
        print(f"REPORT WITHHELD: {e}", file=sys.stderr)
        return 3
    print(f"Reports: {paths['html']}\n         {paths['md']}\n         {paths['junit']}")
    return exit_code(results, a.fail_on, a.strict)
