# NurseConnect QA Testing Agent (Python + Playwright + Pytest + YAML workflows)

Tests **NurseConnect** (nurse, consumer) and **Hospital Dashboard** (reception, doctor). It is your own code - no AI model is
called, and no application source code or data leaves the machine.

There are two ways to run tests. Both are read-only unless a *verified staging* environment says otherwise.

| | What | Command |
|---|---|---|
| **A. YAML workflow engine** (new) | Approved, hash-checked workflows from `workflows.yml` | `python agent.py run ...` |
| **B. Pytest phases** (original) | health, login, discovery, pages, forms, permissions, workflows, OPD, details | `python run.py --phase ...` |

> Status words used everywhere: **PASS** (assertions ran and held) - **FAIL** - **BLOCKED** (a gate refused it, credentials missing, or
> feature not found) - **NOT VERIFIED** (ran, result untrustworthy) - **NOT TESTED** (intentionally not executed: dry-run, read-only mode, draft...).

## 1. Install
```
setup_windows.bat            # Windows: creates .venv, installs requirements.txt + Chromium, copies .env.example to .env
notepad .env                 # fill the 4 *_PASSWORD values (test accounts only; .env is git-ignored)
```
Linux/macOS: `python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt && playwright install chromium && cp .env.example .env`

## 2. The workflow lifecycle: discover -> draft -> approve -> execute -> report
```
python agent.py validate --environments        # offline: YAML schema, environments, approval audit chain
python agent.py discover --app nurseconnect=apps/nurseconnect --app hospital=apps/hospital      # read-only, static
python agent.py draft                          # writes workflow_drafts/<app>/*.yml  (status: draft - CANNOT run)
python agent.py inventory                      # draft / unapproved / approved / changed
python agent.py approve --draft workflow_drafts/nurseconnect/<file>.yml --by "Your Name" --reason "reviewed steps, read-only"
python agent.py approve --id nurse_login_form --by "Your Name" --reason "reviewed"       # a workflow already in workflows.yml
python agent.py run --env production --dry-run # evaluate every gate, launch no browser
python agent.py run --env production --read-only --app nurseconnect
```
* Reports land in `artifacts/reports/`: `agent_report_<run>.html|json|md` and `inventory_<run>.md`;
  discovery docs in `artifacts/discovery/<app>/` (`routes.md`, `forms.md`, `discovery.json`).
* **Nothing runs until a human approves it.** Details: [docs/DRAFTS_AND_APPROVAL.md](docs/DRAFTS_AND_APPROVAL.md).
* Writing your own workflows: [docs/WORKFLOW_SCHEMA.md](docs/WORKFLOW_SCHEMA.md). Samples (login, navigation, form validation, booking) are in `workflows.yml`.
* The two private app repos are checked out by CI with a short-lived read-only GitHub App token - [docs/GITHUB_APP_SETUP.md](docs/GITHUB_APP_SETUP.md).
  Locally you can point `discover` at any folder you cloned yourself.
* Writes (creating test records) are possible **only** on a declared staging environment - [docs/STAGING_AND_SECURITY.md](docs/STAGING_AND_SECURITY.md).

## 3. Original pytest phases (unchanged behaviour)
```
run_tests.bat                       # everything, headless
run_tests.bat --headed              # watch the browser
run_tests.bat --phase health        # health | login | discovery | pages | forms | permissions | workflows | opd | details
run_tests.bat --write               # STAGING ONLY - and only effective on a verified staging environment (see security doc)
```
Output in `artifacts/`: `reports/report_<id>.html|json`, `screenshots/` (masked), `inventory/inventory_<role>.json`.
What changed: the flows of phase 7 now live in `legacy_workflows.yml` (data, validated on load); screenshots are masked; non-GET requests are blocked by a
network guard unless writes were verified; `ALLOW_WRITE_TESTS=true` is only a *request* (see security doc).

| Phase | File | What it does |
|---|---|---|
| 1 Health | test_01_health.py | URL up, content, login form, console errors, HTTPS |
| 2 Login | test_02_login.py | roles log in; invalid + empty login using a FAKE account |
| 3 Discovery | test_03_discovery.py | read-only crawl of the logged-in UI |
| 4 Pages | test_04_pages.py | load, redirects, console/network errors, reload, search, tabs |
| 5 Forms | test_05_forms.py | empty/invalid submit; all POST/PUT/DELETE blocked |
| 6 Permissions | test_06_permissions.py | anonymous access, cross-role access |
| 7 Workflows | test_07_workflows.py | flows from `legacy_workflows.yml` |
| 8/9 | test_08_*, test_09_* | OPD registration, patient details (writes only on verified staging) |

## 4. Tests of the agent itself
```
python -m pytest agent_tests -q       # schema, approvals, safety gates, redaction, discovery, drafts, token script, CI lint, reports,
                                      # real-Chromium end-to-end runs against a local synthetic server, legacy-suite compatibility
```
No network, no real credentials, no patient data: everything uses a local HTTP server and synthetic mock repositories.

## 5. Safety rules built in
* Approval is enforced **in the Python runner** (hash of the executable content) and again by `agent.py check-approvals` in CI.
* Production is read-only. Writes need: declared staging env + exact origin allowlist + optional server probe + `ALLOW_WRITE_TESTS` + confirmation phrase + per-workflow `--confirm-write`.
  A URL containing "staging" proves nothing. Unknown or unreadable configuration => read-only.
* Never clicked/submitted by any workflow: logout, delete, remove, discharge, cancel, pay, refund, messaging... (schema + runtime check).
* Secrets and patient identifiers are redacted from logs, reports and discovery output; if something sensitive survives redaction the report is **withheld**.
* Screenshots are masked (inputs, tables, grids, images). Treat `artifacts/` as sensitive anyway.
* Synthetic data only: `AUTOTEST_<runid>`, `autotest+...@example.invalid`, generated numbers - unique per run and workflow.

## 6. Project map
```
agent.py                 CLI (validate | discover | draft | approve | revoke | inventory | run | verify-env | check-approvals | scan-artifacts)
workflows.yml            approved-workflow definitions (data only)       workflows_approved/   workflows promoted from drafts
workflow_drafts/         generated drafts (never executed)               approvals/            approval records + hash-chained audit log
config/environments.yml  declared environments (production / staging)   legacy_workflows.yml  data of the old phase-7 flows
qa_agent/                schema, workflows, approvals, safety, redaction, discovery, drafts, runner, reporting
tools/github_app_token.py  mint/revoke the short-lived read-only GitHub App token
agent_tests/             tests of the agent          tests/  original Playwright phases          docs/  guides
```
