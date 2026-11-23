# HIMS semantic-document compiler: installation and runbook

## What this patch adds

The previous agent was **already a Playwright workflow executor**. It could read the HIMS specification's YAML but could not execute its `navigate_or_call` or `business_assertion` descriptors. The patch adds:

- `qa_agent/spec_compiler.py`: imports the original HIMS ZIP/YAML, creates the agent's `version: 1` YAML, optionally uses an AI compiler to propose explicit Playwright steps, and validates every result.
- `qa_agent/spec_evidence.py`: optionally extracts source-derived UI controls and reads static control metadata from **declared staging** with a read-only Playwright browser.
- `python agent.py compile-spec`: one command to compile the entire pack or one scenario.
- `python agent.py approve-batch`: one human approval action for a group of *validated* compiled drafts; by default it **excludes** incomplete tests.
- `python agent.py run --spec ...`: read the original document and execute only **matching, approved, current-version** tests. If any source scenario is missing or has changed since approval, this command stops rather than silently using stale tests.
- New supported actions `reload` and `require_verified`. The latter reports `NOT VERIFIED` and **never PASS** when a business assertion has no verified executable implementation.
- Hospital-specific agent role definitions in `config.py`, separate from legacy tests.

### What is automatic, and what is not

**Automatic:** YAML ingestion, scenario discovery, source and DOM evidence collection, model-driven action/selector proposals, YAML schema checks, draft generation, stable test execution, evidence/reporting, and automatic re-runs of previously approved matching tests.

**Not automatic:** creating the verified staging environment, supplying test credentials, enabling third-party API billing, initial human approval of new test steps, or guaranteeing that a model can correctly discover a long clinical workflow with no UI/fixture evidence. Compiling from source alone yields navigation checks and explicit `NOT VERIFIED` markers for uncovered business assertions. Never treat those as passed clinical tests.

## 1. Installation

Install Python dependencies (inside a virtual environment) and the Playwright browser:

```bash
python -m pip install -r requirements-ai.txt
python -m playwright install chromium
```

Place the original `HIMS_Agent_Test_Pack_2026-10-26.zip` in the testing repo, e.g. `specifications/HIMS_Agent_Test_Pack_2026-10-26.zip`. Extract/checkout the HospitalDashboard frontend repo to a local directory such as `apps/hospital` (the existing GitHub App checkout workflow can provide this read-only).

## 2. Register a REAL HIMS staging URL, not production

Edit `config/environments.yml`; add an **actual verified staging deployment**:

```yaml
  hims_staging:
    kind: staging
    allowed_origins:
      - https://YOUR-ACTUAL-HIMS-STAGING-HOST
      # Include an API origin only if it is also dedicated to synthetic staging tests.
    apps:
      hospital:
        base_url: https://YOUR-ACTUAL-HIMS-STAGING-HOST
    # Strongly recommended: configure a staging-verification endpoint before permitting writes.
    # verify:
    #   path: /qa/environment
    #   json_key: environment
    #   equals: staging
```

Do not copy your existing production HIMS Elastic Beanstalk URL into `hims_staging`. Production writes remain forbidden.

Configure role credentials in environment variables/secrets for existing `DOCTOR_EMAIL`, `DOCTOR_PASSWORD`, `RECEPTION_EMAIL`, `RECEPTION_PASSWORD` and other HIMS roles such as `HIMS_LAB_EMAIL`, `HIMS_LAB_PASSWORD`, `HIMS_PHARMACY_EMAIL`, `HIMS_PHARMACY_PASSWORD`, etc. See `config.AGENT_ROLES`. Use **synthetic test accounts**, not actual patient/employee credentials. Never commit `.env`.

## 3. Compile ONE workflow first (recommended)

Use an API model enabled on your OpenAI API account (this is separate from a ChatGPT subscription). Keep the key in an environment secret and select a cost-effective supported model with `--model`.

```bash
export OPENAI_API_KEY='YOUR_KEY_SET_LOCALLY_OR_IN_SECRETS'
python agent.py compile-spec \
  --spec specifications/HIMS_Agent_Test_Pack_2026-10-26.zip \
  --repo apps/hospital \
  --model "$QA_COMPILER_MODEL" \
  --env hims_staging --inspect \
  --only OPD-001
```

The result is `workflow_drafts/hospital/spec_opd-001.yml` and `compilation_report.json`. Inspect the generated steps and required assertions. Missing selectors/assertions produce `require_verified` steps and a non-PASS status; fix the actual UI evidence/configuration and recompile. No one needs to manually write Python or manually map selector tables.

**Alternative with no AI credits:** omit `--model`. The compiler converts the pack into safe navigation drafts with explicit `NOT VERIFIED` business assertions. Those are partial smoke tests only.

## 4. Approve once, then run automatically

For a complete pilot draft (no `require_verified` steps):

```bash
python agent.py approve --draft workflow_drafts/hospital/spec_opd-001.yml \
  --by 'QA reviewer' --reason 'Reviewed on synthetic HIMS staging'

python agent.py run --spec specifications/HIMS_Agent_Test_Pack_2026-10-26.zip \
  --app hospital --workflow OPD-001 --env hims_staging --read-only \
  --fail-on-blocked
```

If OPD-001 is compiled as a **write test**, it additionally needs `--i-understand-this-writes` during approval, a verified staging environment, explicit write confirmations and synthetic fixtures. A read-only run will report that write workflow as `NOT TESTED`, correctly.

To compile all scenarios after the pilot:

```bash
python agent.py compile-spec --spec specifications/HIMS_Agent_Test_Pack_2026-10-26.zip \
  --repo apps/hospital --model "$QA_COMPILER_MODEL" \
  --env hims_staging --inspect --max-pages 70
python agent.py approve-batch --dir workflow_drafts/hospital \
  --by 'QA reviewer' --reason 'Reviewed compiled read-only staging workflows'
python agent.py run --spec specifications/HIMS_Agent_Test_Pack_2026-10-26.zip \
  --app hospital --env hims_staging --read-only --fail-on-blocked
```

By default `approve-batch` refuses all drafts with unmet assertions. For deliberate **partial smoke testing only**, `--include-unverified` permits approving those; their reports still show `NOT VERIFIED`. This flag is **not** a bypass of write gates.

### Example controlled write test

If a completed test contains `action: submit` and must create synthetic records in HIMS staging:

```bash
python agent.py approve --draft workflow_drafts/hospital/spec_rec-001.yml \
  --by 'QA reviewer' --reason 'Reviewed synthetic registration actions' \
  --i-understand-this-writes

export ALLOW_WRITE_TESTS=true
export STAGING_WRITE_CONFIRMATION='I-CONFIRM-STAGING-WRITES-ON-hims_staging'
python agent.py run --app hospital --env hims_staging \
  --spec specifications/HIMS_Agent_Test_Pack_2026-10-26.zip \
  --workflow REC-001 --allow-write --confirm-write spec_rec-001 --fail-on-blocked
```

Make sure server API origins are properly allowlisted, with no production origin on the allowlist. Do not execute any irreversible payment, discharge, clinical messaging, deletion, or treatment actions. The executor deliberately blocks those targets.

## 5. Integrate with GitHub Actions

Use your existing GitHub App for read-only source checkout. Trigger `compile-spec` after source or specification changes, upload the **drafts and compilation_report.json** as build artifacts, and obtain an explicit QA approval (via the CLI) before committing approved YAML and approval audit files. Thereafter, your existing scheduled agent test workflow can run approved tests against staging. **Do not put an auto-approve step in CI** and do not use production credentials.

## 6. Meaningful test outcome rules

- `PASS`: the approved Playwright actions and assertions actually ran successfully.
- `FAIL`: an action or assertion failed in the browser.
- `NOT VERIFIED`: the semantic specification could not be mapped to a real checked business assertion, including untested negative/boundary cases.
- `NOT TESTED`: workflow not run, e.g. staging write omitted in read-only mode.
- `BLOCKED`: credentials, environment/approval gate or other required configuration unavailable.

An AI model is not proof that a selector exists, a test fixture is valid, or a clinical expectation is correct. Live browser execution and explicit assertions determine PASS.

## 7. Pilot acceptance checklist

1. `compile-spec --only OPD-001` produces a **valid executable YAML** draft, without `require_verified` for the tested requirements.
2. A reviewer checks the generated steps once and records approval.
3. `run --spec --workflow OPD-001 --env hims_staging` launches the real browser and verifies its actual assertions.
4. Re-running produces an evidence report automatically, with no hand-written selector mapping.
5. Editing the original HIMS specification makes `run --spec` refuse the old approved version until recompiled and approved.
6. Patient-registration writes are only unlocked on dedicated synthetic staging with the existing safety controls.
7. The pilot is actually run against a live staging deployment before asserting that the remaining workflows are working.

## Local validation of this patch

Offline schema/compiler/approval tests were run. Browser execution against real HIMS staging was **not performed** because neither a verified HIMS staging endpoint nor credentials/API key are available in the supplied artifacts. The container's browser security blocks network access to its local synthetic test server; those browser E2E cases cannot be claimed as passed in this environment.
