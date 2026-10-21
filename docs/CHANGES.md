# What changed, what is done, what is left

## Completed (implemented AND covered by automated tests - see test results in the final report)
YAML workflow engine with schema validation - hash-based approvals with audit chain - draft generation from static discovery - staging-only write gate with origin
allowlist, probe, confirmation phrase and per-workflow confirmation - network-level request guard - secret/patient-data redaction with fail-closed reports -
masked screenshots - dry-run / read-only modes - HTML/JSON/Markdown reports + workflow inventory - discovery docs (routes, forms, controls, validation messages) -
GitHub App token script (mint/revoke, least-privilege check) - hardened CI workflow - legacy suite kept working (test_07 data moved to YAML).

## Files added
`agent.py agent.bat` - `qa_agent/*` - `config/environments.yml` - `workflows.yml legacy_workflows.yml` - `approvals/*` - `workflows_approved/` `workflow_drafts/` -
`tools/github_app_token.py` - `requirements-ci.txt` - `agent_tests/*` - `docs/*`

## Files changed
`.github/workflows/website-tests.yml` (rewritten, original two pytest jobs kept) - `config.py` (ALLOW_WRITE now needs verified staging) - `conftest.py` (write guard,
masked screenshots, no empty report) - `tests/test_07_workflows.py` (definitions moved to YAML) - `utils/redact.py` (delegates to qa_agent) - `tools/find_login.py`
(masked screenshots) - `.gitignore` - `.env.example` (placeholder e-mails, new variables) - `requirements.txt` (+PyYAML) - `README.md`.
Untouched: all other tests, `pages/`, `utils/` (except redact), `run.py`, `patch*.py`, `apply_fix.py`, `tests/test_03_discovery.py.bak` (leftover files from your history).

## Behaviour changes you should know about
* `ALLOW_WRITE_TESTS=true` alone no longer enables writes for `run.py --write` / phases 8-9: it needs `TEST_ENV` + a declared staging environment + `STAGING_WRITE_CONFIRMATION`.
  Until you declare a staging environment, all legacy write tests are NOT executed (they record BLOCKED, as with `ALLOW_WRITE_TESTS=false` before).
* Legacy test contexts now abort non-GET requests that are not login/session calls (production safety net). If a read-only phase relied on some other POST it will now show a failed request.
* The shipped `workflows.yml` entries have **no approval records**, so `agent.py run` blocks them and the CI approval gate fails until you review and approve them (by design).

## Remaining limitations / not verified
* GitHub App flow, real-site selectors, the Hospital URL being production vs test, and pinned GitHub Action versions/SHAs are **not verified** (no credentials/network access here).
* The unverified Action versions in the workflow must be replaced by commit SHAs (see GitHub App guide).
* Static discovery is regex-based: React/Next/Vue conventions only; nested relative routes are not joined; "protected: unknown" means unknown.
* Redaction/masking are heuristic; patient data in free text or pixels cannot be guaranteed removed - use synthetic data on any environment.
* The booking write flow in `workflows.yml` is representative only (required fields beyond name/mobile are not covered; the full proven flow is `tests/test_08_opd_registration.py`).
* Optional AI processing is not implemented (the task preferred deterministic local analysis).
* Tests ran on Linux with Python 3.13 and Chromium 141; not run on Windows.

## Manual setup checklist
1. Create/install the GitHub App and add 3 secrets + 2 variables ([GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md)); pin action SHAs.
2. Declare your real staging environment(s) in `config/environments.yml` (or leave none to stay read-only); add a `verify` probe if the apps can expose one.
3. Create the GitHub Environment `staging-write` with required reviewers; add `STAGING_ENV_NAME`, `STAGING_WRITE_CONFIRMATION`, `STAGING_*` account secrets.
4. Review and approve the workflows you want in CI: `python agent.py approve --id <id> --by "<you>" --reason "<why>"`, commit `approvals/`.
5. Put real test-account e-mails in your local `.env` (the template now has placeholders) and the repository secrets used by the existing jobs.
