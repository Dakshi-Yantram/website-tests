# Autonomous mode: `python agent.py auto`

Give it the site and the test-account passwords (in `.env`). It does the rest. No workflow YAML, no selectors, no approval files.

## Run it (Windows PowerShell)

```powershell
cd C:\Users\Dell\Documents\GitHub\website-tests
.\.venv\Scripts\Activate.ps1
python agent.py auto --app hospital --headed          # watch it work
python agent.py auto --app all                         # both apps, headless
```

Open the report it prints (`artifacts\reports\auto_report_<run>.html`). Bugs are listed first, each with severity, evidence and "how to reproduce".

## What it does by itself

| Step | Detail |
|---|---|
| Login | Finds the login form (tries `/login`, `/auth/login`, ...). Signs in as every role whose `*_EMAIL` / `*_PASSWORD` is set. A server error (HTTP 5xx) is retried twice with a pause; a rejected password (401/403/422) or rate limit (429) is **never** retried. |
| Discover | Sidebar/menu items (links **and** click-only `<div>` menus), links in pages, optional `--seed /path`. |
| Every page | HTTP status, redirect to login, blank page, error text (`undefined`, `NaN`, "something went wrong"...), console errors, failing API calls (5xx = High), slow load, broken images, horizontal scroll, unlabeled fields. |
| Forms | Presses Save/Submit on **empty required forms** and records the message the user sees (for example `Please enter patient name.`). No message = finding. |
| Dialogs, tabs, search | Opens "Add / New / Create ..." dialogs and validates them empty, clicks tabs, searches an unmatched term. |
| Security smoke | Can a logged-out visitor open pages that only a logged-in role reached? Can another role open a page only one role found? |
| Report | HTML + Markdown + JSON. Redacted. Ids in URLs shown as `:id`. |

## Safety

* After login **every non-GET request is blocked** in the browser. Nothing can be created, changed or deleted.
* Controls whose text looks destructive / payment / messaging (`Delete`, `Pay`, `Cancel`, `Discharge`, `Send SMS` ...) are never clicked.
* No data is typed into forms (only the empty submit and one unmatched search term).
* Screenshots follow the patient-data policy: **off** unless the environment is declared staging/local. Force masked ones with `--screenshots masked`.

## Options

| Option | Meaning |
|---|---|
| `--role reception` | only this role (repeatable) |
| `--max-pages 40` / `--max-minutes 10` | limits per role. If hit, the role is marked PARTIAL and the report says why |
| `--seed /some/path` | extra page to visit |
| `--fail-on high\|medium\|low\|info` | exit code 1 when a finding of this severity or worse exists (default `high`) |
| `--allow-read-post REGEX` | your back end uses POST for list/search? Name those paths here (only ones you know are read-only). Default: all blocked |
| `--no-anonymous-check`, `--no-cross-role-check` | skip the access-control smoke checks |

Exit codes: `0` no finding at/above `--fail-on` | `1` bugs found | `3` report withheld (sensitive data) | `4` nothing could be tested (no credentials / no login).

## Read this before trusting a green result

This is **exploration + smoke testing**. "No bugs detected" means: pages load, forms validate, nothing crashed, no obvious access hole.
It does **not** prove business rules (billing totals, clinical logic). Those still need written assertions.

If the report says `Write requests the app tried to send (blocked)` and the list looks like list/search calls, pages may have loaded
without data: re-run with `--allow-read-post "<regex>"`.

## CI

`agent-auto` job in `.github/workflows/website-tests.yml` runs this per app (manual dispatch, same secrets as `agent-tests`).
Repo variables (optional): `AGENT_AUTO_FAIL_ON` (default `high`), `AGENT_AUTO_SCREENSHOTS` (default `auto`), `AGENT_ALLOW_READ_POST`.

### Why `agent-tests (hospital)` failed with exit code 6

Those workflows (`spec_adm-001`, `spec_adm-002`, `spec_fin-001`, `spec_pha-001`, `spec_rad-001`) were compiled from the specification
**without** selectors (no AI key, no staging UI evidence), so their only real step is `require_verified`. They can never pass, and
they are `risk: high`, so the critical gate fails. 67 of the 76 approved workflows are such placeholders.

CI now passes `--placeholders-as-gaps`: placeholders are listed as **COVERAGE GAP** (still not counted as verified) instead of turning
every run red. Set repo variable `AGENT_PLACEHOLDERS_FAIL=true` to get the old strict behaviour back.

## Fill mode (staging only): `auto --fill`

Fills forms with synthetic `AUTOTEST_<run id>` data and saves them, then reports what the user was shown.

PowerShell:

    $env:STAGING_WRITE_CONFIRMATION = "I-CONFIRM-STAGING-WRITES-ON-staging"
    python agent.py auto --app nurseconnect --env staging --fill

Rules enforced in code: the environment must be declared staging/local in `config/environments.yml`, the origin must be on its allowlist, and the confirmation phrase must match. DELETE requests and payment endpoints are always blocked, payment/billing pages and password forms are skipped, and the report lists every record created so you can delete them. The Hospital app stays read-only until a staging copy is declared.

## Video: `auto --video` (staging/local only)

    python agent.py auto --app nurseconnect --env staging --video

Records each role's session. Videos are saved in a `videos` folder next to the report and embedded in the HTML report. Videos show everything on screen, so they are refused on production or unknown environments (the report notes this).
