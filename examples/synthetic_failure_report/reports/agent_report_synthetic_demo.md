# Test run synthetic_demo

- Environment: **local_demo** (local)   Mode: **read-only**
- Started: 2026-10-30T00:00:00Z   Finished: 2026-10-30T00:00:05Z
- Browser: illustrative only   Python: 3.11   Platform: demonstration

| PASS | FAIL | BLOCKED | NOT VERIFIED | NOT TESTED | Total |
|---|---|---|---|---|---|
| 0 | 1 | 0 | 0 | 0 | 1 |

## Workflows

| Workflow | App | Result | Approval | Risk | Writes | Reason / note |
|---|---|---|---|---|---|---|
| `SYNTHETIC-OPD-001` | hospital | **FAIL** |  | high | False |  |

## Failures

- `SYNTHETIC-OPD-001` step 2 (expect_visible) at 2026-10-30T00:00:04Z - expected: Success confirmation after registering synthetic patient; error: Element not found within 1500 ms

## Investigate these issues (hypotheses, not verified root causes)

- **ISSUE-c62372e5165f** (FAIL, High; 1 occurrence(s)): Selector, navigation or rendering - Element not found within 1500 ms. Next: Check the page URL, accessible label, selector stability, load timing and user permissions.; [view screenshot](screenshots/demo_OPD_02_FAIL.png)

## Skipped / not run, with reasons

None.
