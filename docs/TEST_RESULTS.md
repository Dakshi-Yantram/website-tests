# Test results (actual output of the run that produced this package)

Command: `python -m pytest agent_tests -v -p no:cacheprovider`
Environment: Linux, Python 3.13.16, pytest 9.1.1, Playwright 1.56.0, Chromium 141.0.7390.37 (headless), local synthetic HTTP server, no internet.
Result: **174 passed, 0 failed, 0 skipped in 35.89 s**

| File | Tests | Covers |
|---|---|---|
| test_schema_and_loader.py | 44 | valid/invalid `workflows.yml`, unsupported actions, no eval/exec/shell, YAML tag safety, templates, destructive targets, drafts rules, hashing |
| test_redaction.py | 18 | secrets, patient data, registered/env secrets, fail-closed `assert_clean`, legacy `utils.redact` |
| test_safety.py | 23 | environment validation, production blocked, unknown origin blocked, "staging in the name proves nothing", probe failure fail-closed, request guard |
| test_approvals.py | 15 | approval enforcement, re-approval on change, audit chain, hand-edited records, CI refusal, draft promotion |
| test_discovery_and_drafts.py | 17 | mock-repo discovery (routes, protection, forms, validation), sensitive-file exclusion, symlinks, draft safety and determinism |
| test_runner_e2e.py | 29 | real Chromium: gates, PASS/FAIL/BLOCKED/NOT VERIFIED/NOT TESTED, guarded submit (server saw no POST), staging write (exactly one POST), reports, masked screenshots, withheld report |
| test_github_app_token.py | 21 | token request least-privilege (mocked GitHub), JWT validity, absent secrets, over-broad token revoked, never printed; CI workflow lint |
| test_legacy_compat.py | 7 | YAML-migrated legacy flows, all 31 legacy tests still collect, legacy phases 1 and 7 run against the local server, `ALLOW_WRITE` gating, CLI flows |

`python -m pytest tests --collect-only` -> 31 tests collected (the original phases import and collect).

## NOT run / NOT verified
* The original pytest phases against the real NurseConnect / Hospital sites (no network access or test-account credentials here).
* The GitHub App flow and the GitHub Actions workflow on GitHub (only linted statically and the token script tested against a mock).
* Windows. Real staging environments (none are declared).
