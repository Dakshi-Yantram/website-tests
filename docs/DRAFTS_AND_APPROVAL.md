# Draft generation and approval

## Where things live
| Place | Contents | Executable? |
|---|---|---|
| `workflow_drafts/<app>/*.yml` | machine-generated candidates (`status: draft`) + `INDEX.md` (what was generated, what was skipped and why) | **never** |
| `workflows.yml`, `workflows_approved/*.yml` | workflows that may run | only with a valid approval record |
| `approvals/approvals.json` | per workflow: sha256 of its executable content, approver, reason, time | - |
| `approvals/audit.jsonl` | append-only, hash-chained log of approve / re-approve / revoke | - |

## Generation (`python agent.py draft`)
Deterministic, local, no AI model, no network. From the discovery result it creates, per concrete route, a navigation check; per form
(without a password field) an empty-submit validation check (`guarded_submit`, all writes blocked); and for booking-like forms a
"fill with synthetic data, do NOT submit" flow. Every draft has: description, preconditions, steps, expected outcomes, risk, `sources`
(file + line), `status: draft`, `writes: false`. It is validated with the strict draft rules **before** it is saved; drafts that fail are
listed under "Skipped" in `INDEX.md`. Forms with destructive/payment/messaging buttons, credential forms and dynamic routes are skipped with a reason.
`role:` in a draft is a heuristic **guess** - confirm it. Re-running `draft` never touches approved workflows.

## Approval
1. Read the draft (and its `sources`). Edit it if needed (changing it is fine - the hash is taken at approval time).
2. `python agent.py approve --draft workflow_drafts/<app>/<file>.yml --by "Name" --reason "why this is safe"`
   Re-validates, scans for secrets/personal data, copies it to `workflows_approved/<id>.yml` with `status: approved`, records the hash and appends an audit line.
   (`--i-understand-this-writes` is additionally required for `writes: true` workflows; generated drafts can never write.)
3. Commit `workflows_approved/`, `approvals/` through a pull request so a second person can review.
4. **Material change => re-approval.** Any change to steps, targets, values, app, risk, role or writes changes the hash; the runner then reports
   `BLOCKED ... changed since approval`. (Changing only `status`/`generated`/`approval` metadata does not.)
5. `python agent.py revoke --id <id> --by ... --reason ...` removes approval.

Approval is refused when `CI=true` (it is a human decision), `python agent.py validate` verifies the audit chain, and `python agent.py check-approvals`
(used by CI) fails if any workflow claims `status: approved` without a matching record.

## Tamper resistance (and its limit)
An approval counts only if `approvals.json` **and** the hash-chained `audit.jsonl` agree (a hand-edited record, a restored revoked record, or a
broken chain all make the workflow `unapproved`). This catches accidents and partial edits. Someone who can push to the repository could still rewrite
both files consistently, so protect the branch: require pull-request review and add a CODEOWNERS rule for `approvals/` and `workflows*`
(Settings -> Branches -> branch protection, "Require review from Code Owners").

## What discovery and drafts will never do
Read `.env*`, keys, dumps, `node_modules`, symlinks leaving the repo, files over 300 KB, or files containing credential-like strings (skipped whole,
reported by name only). Upload anything anywhere. Generate payments, deletes, messages or any write action.
