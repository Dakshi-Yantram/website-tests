# QA agent: honest CI gating (v3)

This update changes **how CI reports results**. It does not change what the agent tests, the approved workflows,
the approval records or any NurseConnect / Hospital workflow. Nothing here has been committed, pushed or run on GitHub Actions.

## What changed

| File | Status | Purpose |
|---|---|---|
| `qa_agent/verdict.py` | new | Classifies BLOCKED results, finds critical workflows, decides the exit code, prints the verdict, writes the job summary |
| `config/critical_workflows.yml` | new | Which workflows are critical (`risk_levels` + explicit `ids`). Outside the approved YAML, so editing it needs no re-approval |
| `agent.py` | modified | `run` gains `--fail-on-critical-unverified` and `--ignore-blocked`; exit logic moved to `verdict.finish`; `validate` checks the critical list |
| `.github/workflows/website-tests.yml` | modified | Gating flags always applied; HIMS credentials; split report/evidence artifacts; job-summary fallback |
| `agent_tests/test_ci_gating.py` | new | 201 regression tests for all of the above |

Everything else is byte-identical to the previous version (`workflows.yml`, `workflows_approved/`, `approvals/`, `tests/`,
`qa_agent/runner.py`, `schema.py`, `reporting.py`, `approvals.py`, `config.py`, all other `agent_tests/`).

## Behaviour

Exit codes of `python agent.py run`:

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | at least one workflow FAILED |
| 2 | invalid input/config (including an invalid `config/critical_workflows.yml` when `--fail-on-critical-unverified` is used) |
| 4 | a workflow is BLOCKED (`--fail-on-blocked`); kinds listed in `--ignore-blocked` are tolerated **for non-critical workflows only** |
| 5 | any NOT VERIFIED / NOT TESTED remains (`--fail-on-incomplete`) |
| 6 | a **critical** workflow is NOT VERIFIED, NOT TESTED, BLOCKED (any reason, including unapproved) or produced no result (`--fail-on-critical-unverified`) |

* BLOCKED reasons are classified as `credentials`, `unapproved`, `policy` or `other`. Unknown reasons count as `other` (fail closed).
* A workflow is **critical** if its `risk` is in `risk_levels` or its id is in `ids`. `risk_levels` must be values the workflow schema
  supports (`low`, `medium`, `high`); anything else fails closed.
* Every run prints a `VERDICT:` block (PASSED / FAILED / NOT FULLY TESTED) and appends it to the GitHub job summary.
* `--workflow` filters opt out of the "critical workflow never executed" check.

CI (`agent-tests` job) always runs with `--fail-on-blocked --ignore-blocked unapproved --fail-on-critical-unverified`.
`--fail-on-incomplete` is added **only** when the repository variable `AGENT_FAIL_ON_INCOMPLETE` is `true`.

### Expected effect on your current CI
* **Hospital** stays red until the 16 HIMS credentials exist and the 4 reception route failures are fixed.
* **NurseConnect** turns red because `consumer_booking_flow_readonly` was NOT VERIFIED while CI was green. This is deliberate.
  To keep that workflow non-critical, remove its id from `config/critical_workflows.yml`.

## GitHub setup (you do this; no secret value is ever needed by the agent author)

Create these **repository secrets** (synthetic test accounts only), each with `_EMAIL` and `_PASSWORD`:
`HIMS_LAB`, `HIMS_RADIOLOGY`, `HIMS_PHARMACY`, `HIMS_FINANCE`, `HIMS_HR`, `HIMS_NURSE`, `HIMS_HOSPITAL_ADMIN`, `HIMS_SUPER_ADMIN`
(for example `HIMS_LAB_EMAIL`, `HIMS_LAB_PASSWORD`). They are exposed only to two steps of the `agent-tests` job and only on the hospital leg.

Optional repository variable: `AGENT_FAIL_ON_INCOMPLETE` = `true`.

Artifacts: `agent-report-<app>` (HTML, JSON, markdown; 14 days) and `agent-evidence-<app>` (everything else; 3 days).
The GitHub storage quota is already exhausted: delete old artifacts in the repository's Actions settings, usage then recalculates in 6-12 hours.

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-ci.txt
playwright install chromium
```

Dependencies: Python 3.11+ (CI uses 3.11), `playwright>=1.45`, `pytest>=8.0`, `python-dotenv>=1.0`, `PyYAML>=6.0`,
and `PyJWT[crypto]` (only for the discovery job's GitHub App token tool). `requirements-ai.txt` is only needed for AI compilation.

## Running the tests

All 458 agent tests (about 70 seconds; they use a local synthetic server, no real application is contacted):

```powershell
python -m pytest agent_tests -q -p no:cacheprovider
```

Only the new gating tests (201):

```powershell
python -m pytest agent_tests\test_ci_gating.py -q -p no:cacheprovider
```

The same checks CI runs first:

```powershell
python agent.py validate --environments
python agent.py check-approvals
```

The original pytest phases in `tests/` need a live NurseConnect / Hospital site and credentials; they were not run for this change.

## What was and was not verified

Locally verified: the 458 agent tests (Python 3.13, Linux), YAML validity, protected files unchanged, replay of the real hospital and
NurseConnect run results through the new verdict logic, and deliberate regressions being caught by the tests.

**Not verified (needs GitHub Actions):** the workflow run itself, the secrets, artifact upload/exclusion patterns, Python 3.11 and Windows runs, behaviour against real staging.

## Not done yet
* Real selectors/assertions for the 43 NOT VERIFIED hospital specs (needs the application source).
* Why the 4 reception routes fail (`/reception/opd`, `/ipd`, `/emergency`, `/opd/neonate`).
* AI compilation (`OPENAI_API_KEY`, `QA_COMPILER_MODEL`, manual `discover` run).
