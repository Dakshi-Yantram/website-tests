# QA Agent – Existing Capabilities (audit)

Audit date: 2026-11-09. Source: `website-tests-main.zip` as uploaded. No code was changed.
Status labels: **VERIFIED** = I ran or read it in this audit. **READ ONLY** = I read the code but did not execute it. **NOT TESTED** = not checked.

## 1. Baseline

| Check | Result |
|---|---|
| `python -m pytest agent_tests -q -p no:cacheprovider` | **VERIFIED: 212 passed** (offline, 46 s, Python 3.13, pytest 9.1.1) |
| `tests/` (legacy phases 01–09) | NOT TESTED (need a live site and credentials) |
| Any run against NurseConnect staging | NOT TESTED (no credentials or network here) |
| Live AI compiler call | NOT TESTED (no API key; `openai` package is not installed here) |
| Windows | NOT TESTED (audit ran on Linux) |

## 2. Architecture

```
agent.py (CLI)
 ├─ qa_agent/discovery.py      static scan of a checked-out repo -> routes, forms, controls, validation messages; optional GET-only browser inspect
 ├─ qa_agent/drafts.py         discovery -> DRAFT workflows (status: draft, never runnable)
 ├─ qa_agent/spec_compiler.py  semantic spec YAML -> executable draft YAML (deterministic fallback, or AI via OpenAI)
 ├─ qa_agent/spec_evidence.py  source hints (from ONE repo) + staging UI control inventory (Playwright)
 ├─ qa_agent/schema.py         workflow schema + ACTIONS whitelist + DANGEROUS_RX
 ├─ qa_agent/approvals.py      hash-based human approvals + hash-chained audit log (approvals/)
 ├─ qa_agent/safety.py         environments, origin allowlist, write gates, RequestGuard (blocks non-GET at network level)
 ├─ qa_agent/runner.py         Playwright execution of APPROVED workflows only
 ├─ qa_agent/reporting.py      JSON/HTML/MD reports + inventory
 ├─ qa_agent/diagnostics.py    findings from failed steps (optional AI enrichment)
 ├─ qa_agent/redaction.py      secret/PII redaction; reports are withheld (fail closed) if anything sensitive survives
 └─ config.py, config/environments.yml, pages/*, tests/* (legacy pytest phases), workflows.yml, workflows_approved/, workflow_drafts/
```

Pipeline today: `discover` → `draft` (or `compile-spec`) → human `approve` / `approve-batch` → `run`.

## 3. CLI commands (from `agent.py`, READ ONLY unless stated)

| Command | Purpose |
|---|---|
| `validate` | Offline validation of workflows, approvals, audit chain (`--environments` also checks `config/environments.yml`) |
| `discover --app APP=PATH [--inspect --base-url APP=URL --env E]` | Static repo analysis; optional GET-only browser inspection |
| `draft` | Drafts from discovery output |
| `compile-spec --spec X [--app --repo --model --env --env-file --inspect --refresh --only --out --max-pages]` | Semantic YAML → draft workflows |
| `approve` / `approve-batch` / `revoke` | Human approval, recorded in `approvals/` |
| `inventory`, `check-approvals` | Approval state, CI gate |
| `verify-env --env E --app A` | Check an environment declaration and its probe |
| `run [--env --app --workflow --spec --dry-run --read-only --allow-write --confirm-write --headed --fail-on-blocked --fail-on-incomplete]` | Run APPROVED workflows |
| `scan-artifacts DIR [--fix]` | Redact and re-scan reports before upload |

Exit codes: 0 ok, 1 FAIL, 2 invalid input or blocked, 3 sensitive data withheld a report, 4 blocked (with flag), 5 incomplete (with flag).

### `compile-spec` flag check (your section 6)
All seven flags you listed **exist** in the parser: `--spec`, `--app`, `--repo`, `--model`, `--env`, `--inspect`, `--refresh`. Behaviour found in the code:
- `--repo` for NurseConnect must be `nurseconnect=PATH`; a bare path works for `hospital` only. If omitted it falls back to `apps/<app>`, which does not exist in this zip.
- `--inspect` requires `--env` naming a `staging` or `local` environment; production is refused.
- `--refresh` archives the old unapproved draft to `_archive/` and recompiles. Without it, a changed draft gives state CONFLICT.
- `--model` requires `OPENAI_API_KEY` and `pip install openai`. **The AI adapter is OpenAI-only.** It does not call Claude.
- Only these flags are checked by reading. Their live behaviour with a real model is NOT TESTED.

## 4. What exists (relevant to NurseConnect)

- **Safety model is strong:** production is read-only by definition; writes need staging declaration + `--allow-write` + confirmation phrase + per-workflow `--confirm-write`; write lock per env/app; network RequestGuard; synthetic data templates `{{RUN_ID}} {{MARKER}} {{SYNTH_NAME}} {{SYNTH_EMAIL}} {{SYNTH_PHONE}} {{WF_ID}}`.
- **Statuses:** PASS, FAIL, BLOCKED, **NOT VERIFIED**, NOT TESTED. NOT VERIFIED is an extra status (a `require_verified` step: the check is not yet automated). It is stricter than your four labels and I propose keeping it.
- **NurseConnect roles:** consumer, nurse, operations, reviewer, admin (credentials via `.env`).
- **Specs on disk:**
  - `specifications/nurseconnect_full_business_workflows.yaml` – 295 semantic scenarios, all `SPECIFIED_NOT_EXECUTED`.
  - `specifications/nurseconnect_ci_smoke.yaml` (27) and `nurseconnect_public_smoke.yaml` (4).
  - `specifications/HIMS_Agent_Test_Pack_2026-10-26.zip` (hospital app, not NurseConnect).
  - `workflows_approved/` has 77 files; `approvals.json` holds 84 records (includes hospital).
  - `workflow_drafts/nurseconnect/e2e_edge_security.yml` – 42 draft workflows (customer → ops → nurse → reviewer → admin chain, edge cases, security). See `docs/NURSECONNECT_E2E_TEST_PLAN.md`.
- **Allowed step actions (the only ones):** goto, click, fill, select_option, check, press, wait_for, open_section, login_submit, guarded_submit, submit, expect_visible, expect_hidden, expect_text, expect_url, expect_title, expect_count, expect_authenticated, expect_no_writes_sent, screenshot, reload, require_verified.
- **Semantic spec schema:** `schema_version: nurseconnect.agent.workflow.v1`, `application: nurseconnect`, per-scenario `id, module, role, priority (P0..), objective, write_test, source_anchors, source_files, preconditions, steps, assertions, negative_and_boundary_tests, dependencies, status`.

## 5. Gaps against your master prompt

| # | Your requirement | Finding |
|---|---|---|
| 1 | API / backend tests (IDOR, unauthorized API, payment callbacks, duplicate callbacks, rate limits, concurrency) | **Not possible today.** There is no HTTP/API action; the AI prompt forbids "arbitrary HTTP calls". Browser-only. |
| 2 | Simulated GPS for visit start | **Missing.** No geolocation / browser-permission setup in the runner. |
| 3 | Two nurses accepting at the same moment | **Missing.** One browser context per workflow, run serially, with a write lock. |
| 4 | OTP flows (valid, expired, reused) | **Missing.** No way to read a test OTP; nothing for time control. |
| 5 | Payment, broadcast, notify | `DANGEROUS_RX` blocks clicking `pay`, `checkout`, `broadcast`, `notify`, `send message`, etc., even on staging. Safe by design, but it blocks UI testing of those steps. Needs a sandbox-aware exception that is explicit and gated. |
| 6 | Backend source inspection | `compile-spec` reads **one** repo (`source_hints`). Spec `source_files` lists `backend/...` paths, but backend code is not read. No endpoint, model or state-machine extraction. |
| 7 | Commit SHA of tested source | **Not recorded** in reports or drafts. |
| 8 | Validate AI-suggested selectors against the live DOM | Partial. Schema validation exists; there is **no check that a target exists in the staging DOM** before approval. Missing targets only fail at run time. |
| 9 | Requirements traceability report | **Missing.** No requirement IDs ↔ tests ↔ code ↔ evidence matrix. |
| 10 | Report fields: root-cause hypotheses, missing coverage | `diagnostics.py` produces findings; coverage-gap reporting does not exist. |
| 11 | Per-test fields: severity, cleanup | Step-level `severity` exists; cleanup is not modelled. |
| 12 | `specifications/nurseconnect/` split by module (A–L) | Only flat files. The 295 scenarios are not grouped by your modules A–L. |
| 13 | Regression suite for the 9-step chain | Closest: the 16-step E2E chain in `e2e_edge_security.yml` (draft). Not your payment/broadcast/OTP chain. |
| 14 | AI model | OpenAI only. Using Claude would need a new adapter. |
| 15 | Clutter | `patch2.py … patch7.py`, `apply_fix.py`, `tests/test_03_discovery.py.bak` look like one-off scripts. I have not touched or run them. |

## 6. Known risks seen in the config

- `config/environments.yml` lists a `trycloudflare.com` quick-tunnel host as an allowed staging origin. Quick tunnels change when restarted, so this entry will go stale. It is also in `auth_origins`.
- `verify:` (staging probe) is commented out, so staging is currently verified by allowlist only. Your rule "verify database / payment sandbox / data isolation" is not enforced by code.
- `.env.example` has no payment-sandbox, API base URL or OTP settings.
- `docs/AUTOCOMPILER_VERIFICATION_STATUS.md` says some tests assume no staging config. I did not see failures in `agent_tests` (212 pass), so that note may be outdated.

## 7. Configuration needed to run

`.env` (copy from `.env.example`): `CONSUMER_*`, `NURSE_*`, `OPS_*`, `REVIEWER_*`, `ADMIN_*` credentials (staging values only). For AI: `OPENAI_API_KEY`, `pip install -r requirements-ai.txt`. Browser: `playwright install chromium`. Writes: `--allow-write` + `STAGING_WRITE_CONFIRMATION` + `--confirm-write ID`.

Windows PowerShell example (staging, read-only, no browser):
```powershell
python agent.py validate --environments
python agent.py verify-env --env staging --app nurseconnect
python agent.py run --env staging --app nurseconnect --dry-run
```
