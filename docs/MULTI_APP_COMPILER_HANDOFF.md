# Multi-application workflow compiler — handoff for Dakshi

## Verified scope

The **previous ZIP** supported both applications in the *approved Playwright executor*, but its semantic compiler only supported HIMS. This patched ZIP extends YAML **compilation** to both `hospital` and `nurseconnect`, routes source inspection by application, adds mismatch tests, and keeps approval/write gates and screenshot reports. Offline tests do **not** prove actual website login, selector discovery, business assertions, or native mobile execution.

## What goes inside a workflow document

Use a `.yaml` or `.yml` document (or a ZIP containing them), with a unique app-specific prefix for each workflow ID. A DOCX/PDF description is **not** a directly executable specification. If drafting in prose, convert to this semantic YAML format before asking the compiler to use it.

Nurse Connect document header:

```yaml
schema_version: nurseconnect.agent.workflow.v1
application: nurseconnect
workflows:
  - id: NC-BKG-001
    role: consumer
    priority: P1
    objective: "Customer sees available care-package booking options"
    write_test: false
    source_anchors: [/consumer]
    preconditions: ["Synthetic consumer account on staging"]
    steps:
      - action: navigate_or_call
        description: "Open care packages and verify scheduling controls"
    assertions:
      - description: "Care packages and booking controls appear"
    negative_and_boundary_tests:
      - "Empty availability displays an appropriate message"
```

HIMS header remains `schema_version: hims.agent.workflow.v1` and `application: hospital`. Generalized `schema_version: qa.agent.workflow.v1` also works with either **explicit** `application` value. The original HIMS SHA hashes remain compatible with its earlier documents.

**Roles:** Nurse Connect `consumer`, `nurse`, `operations`, `reviewer`, `admin`, or `anonymous`. HIMS `doctor`, `reception`, `lab`, `radiology`, `pharmacy`, `finance`, `hr`, `nurse_hospital`, `hospital_admin`, `super_admin`, or `anonymous`; old `admin` in HIMS semantic documents maps to `hospital_admin`. Cross-app roles are rejected before generating any draft.

Every scenario needs a stable `id`, `objective`, `preconditions`, `steps`, and explicit `assertions`. Specify `write_test: true` only if the scenario intentionally makes staging-only record changes, and explain the synthetic fixtures. Keep `source_anchors` limited to verified web routes; the compiler will not invent a path for a dynamically parameterized screen. Descriptive `navigate_or_call` actions are compilation inputs, **not** executable Playwright actions.

## Source routing is automatic when GitHub App checkout is present

The GitHub App checkout already uses:

- `apps/nurseconnect` for **Nurse Connect**
- `apps/hospital` for **HIMS**

The compiler chooses `apps/<document.application>` automatically if the folder is present, or accepts a safe explicit binding `--repo nurseconnect=apps/nurseconnect`. Any mismatch between `--app`, the document's `application`, and the `--repo APP=PATH` binding causes an error **before source inspection**. It does **not** verify that a user has maliciously placed Hospital content inside a directory named `apps/nurseconnect`; repository identity and permissions remain operational controls. Keep those checkout paths correct.

## Commands: compiling a NEW Nurse Connect workflow

```bash
python -m pip install -r requirements-ai.txt
python -m playwright install chromium
python agent.py compile-spec --spec examples/nurseconnect_semantic_template.yaml \
  --app nurseconnect --repo nurseconnect=apps/nurseconnect \
  --model "$QA_COMPILER_MODEL"
```

For browser DOM hints add `--env staging --inspect` after verifying Nurse Connect staging is configured and role credentials are available. Do not use `--inspect` on production. Staging URL is defined in `config/environments.yml`; it is NOT inferred from the source repo.

The command produces `workflow_drafts/nurseconnect/spec_nc-bkg-001.yml` and `compilation_report.json`. `require_verified` means the compiler could not construct a real assertion; do not treat navigation-only drafts as functional test coverage. Review the generated actions, then approve once:

```bash
python agent.py approve --draft workflow_drafts/nurseconnect/spec_nc-bkg-001.yml \
  --by "QA reviewer" --reason "Reviewed against Nurse Connect staging"
python agent.py run --app nurseconnect --env staging \
  --spec examples/nurseconnect_semantic_template.yaml \
  --workflow NC-BKG-001 --read-only --fail-on-blocked --fail-on-incomplete
```

This sample scenario is **not guaranteed ready for approval**: without the model and verified DOM evidence it is a partial `NOT VERIFIED` draft. `approve-batch` deliberately excludes those by default.

## When you change a workflow tomorrow

1. Edit the existing YAML scenario, retaining `application: nurseconnect` and its workflow ID.
2. Recompile using the same command plus **`--refresh`**. This archives the previous draft under `workflow_drafts/nurseconnect/_archive` and writes a new DRAFT; it does not carry over approval.
3. Review/approve the new draft. The old compiled test no longer matches the updated document SHA.
4. Run `python agent.py run --app nurseconnect --spec <YOUR_UPDATED_YAML> --env staging --read-only --fail-on-blocked --fail-on-incomplete`. It cannot use the old compiled test silently. For tests creating synthetic records, use the separate protected staging-write flow and explicit confirmations.
5. Inspect `artifacts/reports/agent_report_*.html` and linked PNG screenshots.

**GitHub Actions integration:** Set repository variable `NURSECONNECT_WORKFLOW_SPEC` to the committed YAML path, e.g. `specifications/nurseconnect_workflows.yml`, and `HOSPITAL_WORKFLOW_SPEC` to the HIMS YAML/ZIP path. The read-only `agent-tests` CI job then checks that each approved test matches the current specification. `QA_COMPILER_MODEL` (optional) plus secret `OPENAI_API_KEY` enable AI compilation on the manually triggered `discover` GitHub job; that job checks out both source repos and uploads drafts, but **does not approve or execute changed drafts**. After approval, the daily job executes the current test version. No approval bypass or unattended clinical writes have been added.

`--fail-on-incomplete` makes the CLI return nonzero for `NOT VERIFIED` or `NOT TESTED` in specification-bound runs. This avoids green CI for partial workflows.

## Limits and requirements before you claim fully working

- Playwright tests **web pages** (website, browser-based Nurse Connect portals). For React Native/Expo Android/iOS applications, use Maestro, Detox or Appium; this ZIP does **not** include a mobile automation executor.
- The source extractor is regex/AST-like **heuristic** discovery. Expo file-system routes, dynamic selectors and runtime controls may not be inferred; a hosted web staging app and optional model are recommended. The compiler can legitimately output `NOT VERIFIED`.
- No AI model guarantees end-to-end selection of every control or strong assertion accuracy. Initial reviewer approval remains mandatory. Each changed source document triggers a fresh draft and approval.
- Nurse Connect staging is declared; **HIMS staging is not**. Configure a separate real HIMS staging deployment before HIMS write tests.
- Native OTP/SMS, payment/refund, deletion, real patient records and irreversible clinical actions remain excluded/guarded. Screenshot masking stays enabled.
- Current code was verified offline; it has **not** been validated using live Nurse Connect or HIMS staging accounts. Existing repository E2E tests require local browser/server connectivity and include legacy fixture assumptions.
