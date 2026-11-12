# NurseConnect end-to-end, edge-case and security test plan

Machine-readable file the agent executes: `workflow_drafts/nurseconnect/e2e_edge_security.yml` (42 workflows, all `status: draft`).
Nothing in it runs until you review and approve it. This document is the human-readable plan for the same file.

## 1. The journeys

```
Customer --books--> Operations --assigns--> Nurse --documents--> Reviewer --approves--> back to Customer
                                              ^                      |
                                              +---- changes requested +
Admin: sees the nurse and all of the activity above
```
One workflow per role "leg". All legs of one run share `{{SYNTH_NAME}}` / `{{MARKER}}`, so each role opens the same record the previous role created.
Legs run in file order inside one `agent.py run`. Do not run them in parallel or one at a time on different days.

### A. Customer - Nurse - Operations - Reviewer - Admin chain (16 workflows)
| ID | Role | What it checks | Type |
|---|---|---|---|
| `e2e_01_customer_books_care_package` | consumer | Customer: choose a care package and request a booking (stops before payment) | writes |
| `e2e_02_customer_sees_booking_after_reload` | consumer | Customer: the booking is still there after a full reload | read-only |
| `e2e_03_operations_sees_new_booking` | operations | Operations: the customer's new booking appears in the operations queue | read-only |
| `e2e_04_operations_assigns_nurse` | operations | Operations: open the booking and assign the test nurse | writes |
| `e2e_05_nurse_sees_assignment` | nurse | Nurse: the assignment appears under Workspace > Assignments | read-only |
| `e2e_06_nurse_accepts_assignment` | nurse | Nurse: accept the assignment | writes |
| `e2e_07_nurse_documents_visit` | nurse | Nurse: Visits > open the visit > save documentation, then reopen | writes |
| `e2e_08_nurse_submits_documentation_for_review` | nurse | Nurse: submit the documentation to the reviewer | writes |
| `e2e_09_reviewer_sees_submission` | reviewer | Reviewer: the submitted documentation appears in the review queue | read-only |
| `e2e_10_reviewer_requests_changes` | reviewer | Reviewer: request changes with a comment | writes |
| `e2e_11_nurse_sees_review_feedback` | nurse | Nurse: the reviewer's feedback is visible and the note is editable again | read-only |
| `e2e_12_nurse_resubmits_and_reviewer_approves` | nurse | Nurse: add vitals and resubmit | writes |
| `e2e_13_reviewer_approves_documentation` | reviewer | Reviewer: approve the resubmitted documentation | writes |
| `e2e_14_customer_sees_completed_visit_summary` | consumer | Customer: the booking now reflects the nurse's approved work | read-only |
| `e2e_15_admin_sees_nurse_and_activity` | admin | Admin: nurse record and the booking activity are visible to the admin | read-only |
| `e2e_16_nurse_access_matches_admin_status` | nurse | Nurse: an active, approved nurse can open the workspace (admin-controlled status takes effect) | read-only |

### B. Functional edge cases (11 workflows)
| ID | Role | What it checks | Type |
|---|---|---|---|
| `edge_01_booking_empty_required_fields` | consumer | Edge: booking form refuses empty required fields (no request is sent) | read-only |
| `edge_02_booking_malformed_phone` | consumer | Edge: malformed phone numbers are rejected | read-only |
| `edge_03_booking_very_long_text` | consumer | Edge: very long text in the notes field is limited or rejected, not crashed on | read-only |
| `edge_04_booking_html_is_escaped` | consumer | Edge/Security: HTML typed into the notes field is shown as text, not rendered | writes |
| `edge_05_booking_double_click_creates_one` | consumer | Edge: a double click on the submit button creates only one booking | writes |
| `edge_06_booking_reload_does_not_resubmit` | consumer | Edge: reloading after a booking does not create a second one | writes |
| `edge_07_booking_unicode_and_apostrophe_name` | consumer | Edge: names with an apostrophe, accents and a hyphen are accepted and shown unchanged | writes |
| `edge_08_nurse_empty_documentation_rejected` | nurse | Edge: nurse cannot save an empty documentation note | read-only |
| `edge_09_nurse_session_survives_reload` | nurse | Edge: the nurse session survives reloads and a new navigation | read-only |
| `edge_10_nurse_unknown_visit_id` | nurse | Edge: opening a visit id that does not exist shows a clean 'not found', no crash | read-only |
| `edge_11_consumer_unknown_route` | consumer | Edge: an unknown customer page shows a clean 404, not a blank page or stack trace | read-only |

### C. Security (15 workflows)
| ID | Role | What it checks | Type |
|---|---|---|---|
| `sec_01_anonymous_cannot_open_nurse_area` | none (anonymous) | Security: an anonymous visitor cannot open the nurse area | read-only |
| `sec_02_anonymous_cannot_open_customer_area` | none (anonymous) | Security: an anonymous visitor cannot open the customer area | read-only |
| `sec_03_anonymous_cannot_open_staff_areas` | none (anonymous) | Security: an anonymous visitor cannot open operations / reviewer / admin areas | read-only |
| `sec_04_customer_cannot_open_nurse_area` | consumer | Security: a consumer cannot open /partner (broken access control) | read-only |
| `sec_05_nurse_cannot_open_customer_area` | nurse | Security: a nurse cannot open /consumer (broken access control) | read-only |
| `sec_06_customer_cannot_open_staff_areas` | consumer | Security: a consumer cannot open operations / reviewer / admin areas | read-only |
| `sec_07_nurse_cannot_open_staff_areas` | nurse | Security: a nurse cannot open operations / reviewer / admin areas | read-only |
| `sec_08_reviewer_cannot_open_admin_area` | reviewer | Security: reviewer cannot open the admin area | read-only |
| `sec_09_operations_cannot_open_admin_area` | operations | Security: operations cannot open the admin area | read-only |
| `sec_10_login_sql_injection_strings` | none (anonymous) | Security: SQL-injection style input does not log in and does not leak errors | read-only |
| `sec_11_login_html_script_input` | none (anonymous) | Security: HTML in the login fields is not rendered or reflected | read-only |
| `sec_12_login_error_does_not_reveal_account_existence` | none (anonymous) | Security: the login error for an unknown account is generic | read-only |
| `sec_13_nurse_login_has_no_credentials_in_url` | nurse | Security: after login the URL contains no query string (no token / session id in the address bar) | read-only |
| `sec_14_brute_force_unknown_account_is_rate_limited` | none (anonymous) | Security: 10 rapid wrong logins from one browser are throttled (brute-force protection) | read-only, staging-only |
| `sec_15_brute_force_real_account_is_locked_out` | none (anonymous) | Security: 8 wrong passwords lock a real account, even for the correct password afterwards | read-only, staging-only |

## 2. Edge cases covered
- Empty required fields; malformed phone (letters, too short); very long text (about 1,400 characters).
- HTML typed into a text field must be shown as text (stored-XSS check).
- Double click on submit creates one booking; reload after submit does not create a second.
- Apostrophe, accents and a hyphen in a name survive the round trip.
- Nurse cannot save an empty documentation note.
- Reviewer asks for changes, the nurse sees the comment, resubmits, the reviewer approves; the final status shows to the customer.
- Persistence is always checked by reloading the page, never by a toast alone.
- Session survives reloads; unknown ids and unknown routes give a clean "not found", not a crash.

## 3. Security cases covered
- Anonymous visitors cannot open the nurse, customer, operations, reviewer or admin areas.
- Broken access control between roles: customer to nurse area, nurse to customer area, customer / nurse to staff areas, reviewer / operations to admin.
- SQL-injection strings and HTML in the login form: no login, no database error text, nothing reflected as markup.
- Login error for an unknown account is generic (no "user not found").
- No token or session id in the URL after login.
- **Brute force:** 10 rapid wrong logins must be throttled (`sec_14`); 8 wrong passwords must lock a dedicated staging account even for the correct password (`sec_15`). Both are `staging_only` and are BLOCKED on production or an undeclared environment.

## 4. NOT covered by this agent (and why)
These need an API/HTTP adapter or a header/response inspector, which the agent does not have. They are not in the YAML, so nothing pretends to cover them.
- IDOR / "customer A reads customer B's booking by changing an id" (needs two customer accounts and direct API calls).
- OTP brute force (nurse onboarding Stage 1 OTP), password-reset abuse, API rate limits (needs raw API calls).
- Cookie flags (HttpOnly, Secure, SameSite), security headers, CORS (needs response-header checks).
- Logout and session invalidation: the agent refuses to click "log out" by design.
- Payment, cancellation, deletion, refunds and any message sending: refused by design.
- Admin write actions (approve / suspend a nurse): irreversible, needs a controlled throw-away nurse fixture.
- Inputs over a few KB, file uploads, concurrency between two users at the same moment, accessibility, load.

## 5. Things I could not know (marked `# VERIFY` in the YAML)
I had no NurseConnect application source. Menu names for nurse (Workspace, Assignments, Visits, Documentation) and login fields come from your
earlier screens and the existing suite. Button and field names (Request Booking, Assign, Accept, Submit for Review, Approve, Request Changes,
Notes, Comment) and the operations / reviewer / admin menu names and paths (/operations, /reviewer, /admin, /auth/login) are best guesses.
On the first staging run a wrong guess appears as `FAIL: Element not found ...`. Treat that as a mapping fix in the YAML, not a product bug,
until you have checked the screen. Unknown route probes are `optional` and show as NOT VERIFIED.

## 6. Setup (nothing secret goes in the repo)
1. Declare staging in `config/environments.yml` (copy the template, exact origins, optional `verify:` probe).
2. GitHub Secrets (staging values only): `STAGING_CONSUMER_*`, `STAGING_NURSE_*`, `STAGING_OPS_EMAIL/PASSWORD`, `STAGING_REVIEWER_EMAIL/PASSWORD`,
   `STAGING_ADMIN_EMAIL/PASSWORD`, `STAGING_LOCKOUT_EMAIL/PASSWORD`, `STAGING_WRITE_CONFIRMATION`; variable `STAGING_ENV_NAME`.
3. Create a dedicated test nurse called "Test Nurse" on staging, and a throw-away account for the lockout test.
4. Fix every `# VERIFY` line against the real screens.
5. Review, then approve (this is a human step; CI refuses it):
   `python agent.py validate --file workflow_drafts/nurseconnect/e2e_edge_security.yml`
   `python agent.py approve --draft workflow_drafts/nurseconnect/e2e_edge_security.yml --by "<name>" --reason "<why>" --i-understand-this-writes`
   Approval covers the whole file. To approve only some workflows, copy the file and delete the ones you do not want first.
6. Commit `workflows_approved/` and `approvals/` through a pull request.
7. Run the chain on staging: `python agent.py run --env <staging> --app nurseconnect --allow-write --confirm-write <id> ...` (see docs/STAGING_AND_SECURITY.md),
   or start the `agent-write` job from GitHub Actions with `allow_write` and the ids.

## 7. Engine changes made for this pack
- New agent-only roles `operations`, `reviewer`, `admin` (`config.AGENT_ROLES`; env vars `OPS_*`, `REVIEWER_*`, `ADMIN_*`). They are kept out of `config.ROLES` so the legacy pytest suite and its report are unchanged.
- New workflow key `staging_only: true` (BLOCKED unless the environment is declared staging or local).
- Synthetic templates now work inside target `name`, `label`, `text` and `placeholder`, so a role can open the record an earlier role created.
- 14 new automated tests in `agent_tests/test_e2e_pack.py`.
