## Multi-app compiler update

The semantic compiler now supports both applications. **Read `docs/MULTI_APP_COMPILER_HANDOFF.md` first.** Keep document `application` aligned with its source checkout (`apps/nurseconnect` or `apps/hospital`), and use `--refresh` after changes; do not auto-approve generated clinical writes.

# For Dakshi — Final QA Agent Code

Use **this complete repository ZIP** as the merge source; previous AutoCompiler and reporting ZIPs are superseded.

1. Create a branch in the existing `website-tests` repository and compare/merge these files. Do not overwrite active credentials or your team's newer changes.
2. Read `docs/FINAL_AGENT_HANDOFF_2026-10-30.md` for feature-by-feature modifications, tests, and limitations.
3. Install dependencies and run `python agent.py validate --environments` and `python -m pytest agent_tests/test_reporting_upgrade.py agent_tests/test_schema_and_loader.py agent_tests/test_spec_compiler.py agent_tests/test_safety.py -q`.
4. Open `examples/synthetic_failure_report/reports/agent_report_synthetic_demo.html` to inspect a **fictional** failure report and clickable masked screenshot. This is not a live browser-test result.
5. Configure verified HIMS staging and synthetic credentials, compile/approve workflow drafts per `docs/HIMS_AUTOMATIC_COMPILER.md`, then run one approved synthetic HIMS workflow to validate end-to-end screenshots and issue reporting.
6. On GitHub Actions, download `agent-report-hospital` or `agent-write-report` and open HTML *after unzipping*: screenshot links use sibling `screenshots/` files and should resolve offline.

**Security:** The compiler cannot safely auto-approve new write tests. Production screenshots are disabled. Generated or modified workflow specifications require explicit review, while already-approved tests may run repeatedly under existing environment gates. Automatic AI diagnosis is optional and off by default.

**Validation at handoff:** 132 selected offline tests passed; real browser testing against live HIMS staging was not possible in the preparation environment. Do not claim all 70 HIMS clinical scenarios passed until actual Playwright evidence confirms them.
