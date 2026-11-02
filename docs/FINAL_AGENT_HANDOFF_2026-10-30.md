# Final QA Agent Handoff — 30 October 2026

**Deliverable:** full `website-tests-main` source tree, including the HIMS semantic workflow compiler added earlier and this reporting/evidence patch. Merge the ZIP into Dakshi's repository after taking a branch/backup. No credentials or deployed application data are included.

## What is implemented

1. **HIMS YAML compiler/executor compatibility:** Existing `compile-spec` imports the 70 semantic HIMS specs and creates reviewable agent-schema drafts. Optional AI model plus source and staging UI evidence can propose concrete selectors. Strict approval and staging-write gates remain. Without AI/selector evidence, missing business assertions stay `NOT VERIFIED`; drafts are not falsely considered complete.
2. **Automatic browser failure evidence:** Captures screenshots for failed login and `FAIL`, `BLOCKED`, and `NOT VERIFIED` browser steps when a verified local/staging session is available. Records error, expected/actual, duration, timestamp, URL, console and failed network requests. Preflight gates (no browser) properly have no screenshot.
3. **Self-contained report artifact:** Every report directory includes its own `screenshots/` folder with PNG evidence; HTML provides clickable thumbnails and links; JSON and Markdown link to the same files. Thus the GitHub artifact downloaded from `artifacts/reports/` includes images.
4. **Issue diagnosis:** Automatic categorization, suggested investigation, grouping repeated *symptoms*, and severity, while explicitly marking proposed causes unverified. A separate timeline JSON contains only redacted step metadata. No raw Playwright trace/video is collected because those may contain patient data, sessions, and passwords.
5. **Optional AI investigation:** `QA_AI_DIAGNOSIS=true` plus `OPENAI_API_KEY` and `QA_DIAGNOSIS_MODEL` allow a model to suggest possible causes **only for staging/local runs** and using redacted metadata only. AI is disabled by default; `pip install openai` is optional. AI guesses are not presented as confirmed fixes.
6. **Safety and correctness fixes:** A workflow containing an unexecuted required step cannot be `PASS`. Unexpected browser crashes generate `BLOCKED` or `FAIL` results. Screenshot generation defaults **off** for unknown/production environments and **masked** for local/staging; GitHub CI always masks, even if `full` is requested. No sensitive-screenshot artifacts should be uploaded from production.
7. **Fail-closed GitHub Actions upload:** The artifact upload runs only if the text privacy scan succeeds. On both agent read-only and staging-write jobs, it uploads `artifacts/reports/` containing reports **and bundled screenshots**. Legacy jobs still upload their original artifact tree after scan.
8. **Environment tests:** Updated a stale safety test to check separation of production and already-declared staging origins, rather than incorrectly assuming staging doesn't exist.
9. **Example:** `examples/synthetic_failure_report/reports/agent_report_synthetic_demo.html` with entirely fictional screenshots and findings. The example is not evidence of a live HIMS run.

## Exact setup commands

```bash
python -m pip install -r requirements.txt -r requirements-ci.txt
python -m playwright install chromium
python agent.py validate --environments
python -m pytest agent_tests/test_reporting_upgrade.py agent_tests/test_schema_and_loader.py agent_tests/test_spec_compiler.py agent_tests/test_safety.py -q
```

Read `docs/HIMS_AUTOMATIC_COMPILER.md` for the complete `compile-spec`, `approve-batch`, and `run --spec` sequence. Source repo and verified **HIMS staging** URL are required for a real test, and synthetic staging-role accounts must be configured. Do not point clinical write tests to production.

After an approved read-only run:

```bash
python agent.py run --app hospital --env <VERIFIED_HIMS_STAGING_ENV_NAME> --read-only
python agent.py scan-artifacts artifacts --fix
```

For a synthetic patient registration workflow, use the repository's existing explicit `staging-write` workflow, verify `STAGING_WRITE_CONFIRMATION`, and explicitly confirm workflow IDs. Read-only mode will not run a registration write test. The `agent-write` workflow is manually dispatched and review-gated.

The `agent-tests` and `agent-write` GitHub Action artifacts are named `agent-report-hospital` / `agent-report-nurseconnect` and `agent-write-report` respectively. After the job, download the artifact, unzip, open `agent_report_*.html`, and click the screenshot thumbnails. A `BLOCKED` preflight test has a textual cause but no screenshot, because no browser was opened.

## Remaining configuration / independent validation

- HIMS staging instance, accounts, network allowlist and environment declaration must be provided by Dakshi; Nurse Connect staging exists in the repo but **does not prove HIMS staging is configured**.
- Production/real patient scenarios must not use screenshot evidence; synthetic local/staging records only.
- Playwright full browser E2E tests were **not completed in this execution environment** (local test server/browser access limits). Unit tests and synthetic-report generation have been performed; test browser execution and capture in GitHub Actions is required before claiming live QA operational readiness.
- The previous offline compiler produced partial smoke tests; **70 complete HIMS business tests have not been verified end-to-end**. Missing assertions remain `NOT VERIFIED` pending AI/source/staging discovery and review. Neither automatic compiler success nor a valid YAML file proves a clinical workflow passed.
- Existing tests outside the reporting patch include some historical environment/fixture assumptions; CI should run the full suite and Dakshi must resolve any remaining actual failures after merging. Do not disable tests or override approval gates to obtain a green result.

## Suggested acceptance demonstration

1. Use an approved synthetic HIMS staging workflow that deliberately fails an `expect_visible` assertion.
2. Confirm the workflow is `FAIL`, a screenshot PNG exists, and the HTML includes a working thumbnail and error details.
3. Confirm the GitHub artifact includes HTML, JSON, Markdown, timeline and `screenshots/*.png`.
4. Confirm the same workflow reports `BLOCKED` rather than falsely passing when login/permissions are missing.
5. Confirm a workflow with an unverified business assertion is not counted as PASS.
6. Confirm no production screenshots are produced and artifacts do not upload if privacy scans fail.
7. Re-enable a real synthetic OPD workflow and demonstrate register → save → reload → search → verify persistence.

## Implementation constraints

Keep sensitive-data masking in place. Do not replace unverified assertions with `expect_visible body` and mark success. Source-linked AI diagnostics are a further optional project; the current evidence triage does not autonomously inspect application repositories for confirmed bug locations. This limits LLM API costs and reduces patient-data exposure.
