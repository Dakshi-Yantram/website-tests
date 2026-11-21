# Login agent (`login_agent/`)

Reads `login_tests/login-test-plan.yaml`, drives Chromium with Playwright, prints PASS/FAIL per test and writes reports + failure screenshots.
It uses the project's stack (Python, Playwright, PyYAML, python-dotenv, `config/environments.yml`, `.env`) but is **self-contained**: it imports nothing from `qa_agent/`,
so it keeps working when `qa_agent/` is missing or changes. It is independent of the approval-gated `agent.py` workflows and the pytest phases; neither is changed.
The only shared file is `config/environments.yml` (override with `QA_ENV_FILE`); if it is missing the agent stops with exit code 2 and says so.

## Files
| Path | Purpose |
|---|---|
| `login_tests/login-test-plan.yaml` | 19 step-by-step tests + the selector table (the only place selectors live) |
| `fake-id/` | Fictional identities (`test-identities.yaml`), test data and redirect payloads (`test-data.yaml`), README |
| `login_agent/` | The agent: `plan.py` (YAML loader/validator), `actions.py` (step executor), `guard.py` (network safety), `runner.py` (gates), `report.py`, `cli.py`, plus its own `errors.py`, `redaction.py` (secrets, OTPs, tokens, e-mail, phones), `safety.py` (environments, origins), `artifacts.py` (masked screenshots) |
| `agent_tests/login_mock.py`, `agent_tests/test_login_agent.py`, `agent_tests/test_login_agent_selfcontained.py` | Offline tests: the agent against a local mock of the login page, plus tests of the self-contained safety modules |
| `.env.example` | New variables (below) |

## Setup (Windows PowerShell, from the `website-tests` folder)
```powershell
.\setup_windows.bat                 # once: creates .venv, installs requirements + Chromium, copies .env.example to .env
.\.venv\Scripts\Activate.ps1
notepad .env                        # only needed for staging runs (see "Credentials")
```
No new packages are needed (Playwright, PyYAML and python-dotenv are already in `requirements.txt`).

## Run
```powershell
python -m login_agent --list                       # show the tests
python -m login_agent --dry-run                    # check the plan + safety gates, start no browser
python -m login_agent                              # PRODUCTION (default): read-only, anonymous tests only
python -m login_agent --env staging                # staging: also invalid login, valid login, post-login redirect tests
python -m login_agent --env staging --only LOGIN-006,LOGIN-017
python -m login_agent --headed --slow-mo 300       # watch the browser
python -m login_agent --tags smoke
```
Useful flags: `--fail-on medium` (exit 1 only for Medium+ failures), `--strict` (BLOCKED also exits 1), `--screenshots off|masked|full`, `--out DIR`.
Exit codes: `0` ok, `1` a test FAILED, `2` plan/safety/config error (nothing ran), `3` report withheld because it still contained something sensitive.

## What runs where
| Environment | Tests that run | Network behaviour |
|---|---|---|
| `production` (default) | the 13 `anonymous` tests: page load, typing, password toggle, empty / malformed fields, forgot-password, register + phone-OTP navigation, `?redirect=` attacks, keyboard, accessibility | GET only. **Every POST is aborted inside the browser**, so even a click on "Login" sends nothing |
| `staging` / `local` | all 19 | The only POST that may leave the browser is the login request of a test marked `login_post: true`, to an allowed origin, max 14 per run |

Tests that need staging are reported **NOT TESTED** on production. Tests whose credentials are missing are **BLOCKED**, never FAIL.
The agent never registers, edits or deletes anything and never clicks "Send OTP", "Create account" or any reset button.

## Coverage of your list
| Requirement | Tests |
|---|---|
| Valid login | LOGIN-006 consumer, LOGIN-014 care professional (staging) |
| Invalid login | LOGIN-005 (staging): rejected, safe message, no account-enumeration wording, no session |
| Empty fields | LOGIN-004 both, LOGIN-011 email empty, LOGIN-012 password empty, LOGIN-013 malformed email |
| Password visibility | LOGIN-003 (+ LOGIN-002 masking) |
| Forgot password | LOGIN-007 |
| Registration navigation | LOGIN-008 (+ LOGIN-015 phone OTP tab) |
| Redirect validation | LOGIN-009 external/malformed `?redirect=` (anonymous), LOGIN-016 same after login, LOGIN-017 internal redirect honoured, LOGIN-018 other role's portal ignored |
| Extra | LOGIN-001 page load, LOGIN-010 keyboard order + focus ring, LOGIN-019 accessible names |

## Credentials (`.env`, git-ignored)
```
LOGIN_AGENT_ENV=production            # default environment; staging runs: --env staging
NC_TEST_CONSUMER_PASSWORD=...         # staging consumer account
NC_TEST_WORKER_PASSWORD=...           # staging care-professional account
NC_TEST_CONSUMER_EMAIL=               # optional: use when the seeded staging e-mail differs from fake-id/test-identities.yaml
NC_TEST_WORKER_EMAIL=
NC_TEST_INVALID_EMAIL=                # optional: a well-formed e-mail with NO account (exercises HTTP 401 instead of 422)
LOGIN_TEST_API_ORIGIN=                # optional: staging API origin that may receive the login POST (must be in allowed_origins)
LOGIN_SCREENSHOT_MODE=masked          # masked | off | full (full only on staging/local, never in CI)
```
Create the staging accounts yourself, on staging only (see `fake-id/README.md`). Passwords never appear in YAML, logs or reports.

## Staging origin setup
`config/environments.yml` already declares a `staging` environment. The login POST goes to the **API** origin, not the website, so that origin must be allowed:
either list it under `auth_origins:` of the staging environment, or set `LOGIN_TEST_API_ORIGIN` (it must also be in `allowed_origins`).
If you forget, tests fail with `blocked ... is not an allowed login origin` - nothing is sent. A production origin in `auth_origins` is refused at start-up.
If the `staging` environment declares a `verify:` probe it is checked before anything runs.

## Output (`artifacts/login/<run id>/`)
`login_report.html` (open this), `login_report.md`, `login_report.json`, `junit.xml` (for CI), `screenshots/<TEST-ID>.png` for failed tests only.
Screenshots hide inputs, tables and images (`masked`). Reports are redacted (passwords, tokens, e-mails, phones); if anything sensitive survives, nothing is written.
Production note: the existing suite turns screenshots off on production; this agent keeps *masked* failure screenshots there because the pre-login page holds no patient data. Use `--screenshots off` to disable.

## Writing or changing tests
Steps are plain YAML (`{action: click, target: login_button}`); unknown actions, unknown selectors and misspelled parameters stop the run with exit code 2.
Actions: `goto fill clear click wait` and `expect_visible expect_hidden expect_disabled expect_attr expect_value expect_invalid_field expect_url expect_text expect_feedback expect_login_request expect_session expect_no_session expect_no_request_to expect_no_writes tab_through expect_tab_order expect_focus_visible expect_accessible_name`.
`only_env_kinds: [staging, local]` on a step skips it elsewhere. `foreach: redirect_payloads` repeats a test per payload from `fake-id/test-data.yaml`.

## Running the agent's own tests
```powershell
python -m pytest agent_tests/test_login_agent.py agent_tests/test_login_agent_selfcontained.py -q
```
If `qa_agent/` is missing, the project's root `conftest.py` fails on import before any test runs; add `--noconftest` for these two files (they use no shared fixtures).
Inside pytest the root `conftest.py` also installs a process-wide `Page.screenshot` guard that suppresses captures unless `TEST_ENV` names a staging/local environment,
so failure screenshots are written by the CLI (`python -m login_agent`), not inside pytest; the report then carries a note. The screenshot tests therefore run the CLI in a subprocess.

## Known limits and honest status
* **Not yet run against the live site.** Selectors come from the frontend source. Run `python -m login_agent --only LOGIN-001` from your machine first.
* Expected FAIL today: **LOGIN-019** (Low). In the source, `<label>` has no `htmlFor`, the password-toggle button has no accessible name, and the page has no `required` attributes. Fix is small (`htmlFor`/`id`, `aria-label="Show password"`).
* Production cannot test server-side validation: the empty-form request is blocked, so LOGIN-004/011/012 only prove "no session, still on login" there. Run them on staging for the 422 path.
* `.invalid` fixture e-mails are probably rejected by the backend's `EmailStr`; use the `NC_TEST_*_EMAIL` overrides.
* Cloudflare/WAF bot rules, if enabled on the site, may block headless Chromium; the agent then reports BLOCKED or FAIL on LOGIN-001.
* Staging login limit is 20 per 15 min per IP (backend); a full staging run uses about 12. Wait before repeating a full run.
