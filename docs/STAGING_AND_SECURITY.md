# Staging-only writes, environment verification and sensitive-data protection

## 1. Environments are declared, not guessed
`config/environments.yml` is the only source of truth. It ships with **one production environment** (both current URLs, treated as production
because nothing proves otherwise - the Hospital Elastic Beanstalk URL is *assumed* production until you declare it staging).
No staging exists until you add one (template in the file). The word "staging" in a name or URL is ignored.

A `staging` / `local` environment must list explicit `allowed_origins` (exact `scheme://host[:port]`, no wildcards); its app `base_url`s must be inside
that list; none may equal or share a hostname with a production origin; plain http (non-localhost) needs `allow_insecure: true`.
Optional `verify:` makes the server prove what it is (`GET /api/health` must return `{"environment": "<expected>"}`); if declared it must match.
`auth_origins:` lists extra hosts (e.g. a separate API host) that may receive **login/session** POSTs.

## 2. All rules that must hold before any write (anything unknown or unreadable => read-only)
1. Active environment (`--env` / `TEST_ENV`) exists, loads cleanly and is `kind: staging|local` (never `production`).
2. App base URL origin is in that environment's allowlist and not a production origin/host.
3. The `verify` probe (if declared) succeeds and matches.
4. `ALLOW_WRITE_TESTS=true` or `--allow-write` (this is only a *request*).
5. `STAGING_WRITE_CONFIRMATION=I-CONFIRM-STAGING-WRITES-ON-<environment name>`.
6. The workflow id is named in `--confirm-write <id>` **and** the workflow is approved with `--i-understand-this-writes`.
7. Only one write run per environment+app at a time (lock file) so parallel runs never share mutable test data.
Enforced in `qa_agent/safety.py` + `qa_agent/runner.py` (YAML engine) and `config.py` (`ALLOW_WRITE` for the legacy phases). Also enforced
**at the network layer**: a `RequestGuard` aborts every non-GET request that rules 1-3 do not allow, whatever the page script tries; `guarded_submit`
blocks all non-GET requests (even login) for that click. Tests prove blocked writes never reach the server.

## 3. Read-only login exception
Logging in sends a POST. Login/session-style requests (`/auth`, `/login`, `/signin`, `/token`, `/session`) to the app's own origin (or an `auth_origins`
host) are allowed so authenticated read-only tests are possible. Everything else non-GET is blocked unless rules above allow it.
If your login API uses another path name it will be blocked: add its host to `auth_origins` or use a role login (legacy `LoginPage`).

## 4. CI
Write tests run only from the manual `agent-write` job: needs `allow_write` input, approval by reviewers of the GitHub Environment **`staging-write`**
(create it: Settings -> Environments -> required reviewers), repo variable `STAGING_ENV_NAME`, secret `STAGING_WRITE_CONFIRMATION`, staging account secrets
(`STAGING_*_EMAIL/PASSWORD`) and `confirm_write_ids`. Missing any => the job stops before running anything.

## 5. Sensitive data
* Never read or reported: `.env*`, keys/certs, SQL dumps, DB files, tfstate, `node_modules`, symlinks, oversized files, any source file containing a
  credential-like literal (skipped whole and listed by name). Only allow-listed source extensions (`.js .jsx .ts .tsx .vue .html .svelte`) are analysed.
* Discovery stores structured facts only (route, label, line) - never raw code.
* `qa_agent/redaction.py` masks JWTs, GitHub/AWS/Slack/Google keys, private keys, bearer tokens, cookies, DB URLs, `password=`-style pairs, registered/env secrets, and
  personal identifiers (phone, e-mail, Aadhaar, PAN, UHID/MRN/patient id, patient-name/DOB/diagnosis fields). It is **heuristic**: it cannot prove free text has no personal data.
* Fail closed: `write_reports` / `write_discovery` re-scan the redacted output and **write nothing** if anything sensitive remains; `agent.py scan-artifacts --fix`
  does the same for every text artifact before CI uploads it.
* Screenshots: `SCREENSHOT_MODE=masked` (default) masks inputs, tables, grids, images; `off` disables; `full` is honoured only on staging/local. Pixels cannot be
  scanned afterwards, so treat uploaded screenshots as sensitive and use synthetic data on any environment you test.
* No AI service is called by this project. If you ever add one, send only output that passed `qa_agent.redaction.safe_text()` and never source files.
* `.gitignore` excludes `.env*`, keys, `artifacts/`, `apps/`, locks and temp data. `.env.example` now uses placeholder addresses (`*.example.invalid`).

## 6. Known limits
Redaction and screenshot masking are best-effort. Static discovery misses dynamically built routes and cannot know a route is public. Selectors in the sample
workflows are unverified against the live sites. Login-by-form needs the right field/button names for each site.
