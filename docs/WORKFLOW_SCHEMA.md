# Workflow YAML schema (`workflows.yml`)

Workflows are **data**. The engine (`qa_agent/runner.py`) only knows the actions below; there is no `eval`, no script, no shell action and
YAML is parsed with `yaml.safe_load` (Python object tags are rejected). Validate any time with `python agent.py validate`.

```yaml
version: 1
workflows:
  - id: hospital_opd_registration_validation     # a-z 0-9 _ -, 3-64 chars, unique across all files
    app: hospital                                # nurseconnect | hospital
    title: ...                                   # required
    description: ...                             # required
    status: approved                             # draft | approved   (declares intent; the approval record is the authority)
    risk: low                                    # low | medium | high   (high may not write)
    writes: false                                # explicit; must match the steps (a `submit` step needs true)
    role: reception                              # optional: log in first with that role's *_EMAIL/*_PASSWORD env vars
    preconditions: [..]   expected_outcome: [..] sources: [{file: src/App.jsx, line: 12}]   # sources/preconditions are required for generated drafts
    timeout_ms: 30000                            # default for every step (500..120000)
    steps:
      - action: goto
        path: /hospital-dashboard/reception/addPatient/opd
        expected: Form page loads                # text shown in the report
        timeout_ms: 20000                        # optional per-step override
        severity: Medium                         # Critical|High|Medium|Low - used when the step FAILs
        optional: false                          # true: a failing step becomes NOT VERIFIED instead of FAIL
```

## Targets (selectors)
Exactly one of: `role` (+ optional `name`, `exact`), `label`, `placeholder`, `text`, `test_id`, `css`; optional `nth`.
Prefer `role`/`label` (accessible). `xpath` is not supported.
```yaml
target: {role: button, name: Save Patient}     target: {label: First Name}     target: {css: 'input[type="password"]'}
```

## Actions
| Action | Parameters | Notes |
|---|---|---|
| `goto` | `path` | site-relative only (`/x`); no scheme/host/`..` |
| `click` | `target` | refuses elements whose text looks destructive/payment/messaging (also at runtime) |
| `fill` | `target`, `value` **or** `value_env` | `value` may only use templates; `value_env` names an env var (never the secret) |
| `select_option`, `check`, `press` | `target`/`value`/`key` | |
| `wait_for` | `ms` (<=10000) **or** `target` | |
| `open_section` | `pattern` (regex) | same menu-finding logic as the legacy suite; BLOCKED if not found |
| `login_submit` | `target` | click; the login/session POST to the app's own origin is allowed |
| `guarded_submit` | `target` | click while **every** non-GET request is blocked at the network layer |
| `submit` | `target` | **real write** - staging only, needs `writes: true` + all confirmations |
| `expect_visible`, `expect_hidden` | `target` | |
| `expect_text`, `expect_url`, `expect_title` | `contains` **or** `matches` (+ optional `target` for text) | retried until timeout |
| `expect_count` | `target`, `min`/`max`/`equals` | |
| `expect_authenticated` | - | no password field visible and not on a login URL |
| `expect_no_writes_sent` | - | nothing non-GET (other than login) reached the server |
| `screenshot` | `name` | masked per `SCREENSHOT_MODE` |

## Synthetic-data templates (only these are allowed in `value` / `contains` / `path`)
`{{RUN_ID}}` `{{MARKER}}` (`AUTOTEST_<run>`) `{{WF_ID}}` `{{SYNTH_PHONE}}` `{{SYNTH_EMAIL}}` (`...@example.invalid`) `{{SYNTH_NAME}}`.
Values are unique per run and workflow. Literal phone numbers, e-mails, IDs or token-like strings in `value` are rejected.

## Errors you will see
`file not found` - `not valid YAML at line N` - `unknown action 'clik' (did you mean 'click'?)` - `step 3 (click): missing required key(s) ['target']` -
`path must be a site-relative path` - `target looks destructive / payment / messaging related` - `writes is false but contains a write action` -
at run time: `Element not found within N ms: role='button', name='...'`, `Timeout after N ms`, `Navigation error: ...`, `HTTP 404 for /x`.
