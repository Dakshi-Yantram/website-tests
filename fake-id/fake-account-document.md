# NurseConnect Fake Account Document

## Purpose
These fictional identities are QA fixtures only. They are not real people or accounts created on NurseConnect.

## Fake identities

| Fixture ID | Display name | Email | Phone placeholder | Use | Seeded |
|---|---|---|---|---|---|
| `consumer_valid` | Test Consumer | `consumer.qa@example.invalid` | `+919000000001` | Positive login after staging setup | No |
| `consumer_invalid_password` | Invalid Login Fixture | `invalid-login.qa@example.invalid` | `+919000000002` | Negative login test | No |
| `worker_valid` | Test Care Professional | `worker.qa@example.invalid` | `+919000000003` | Worker test after staging setup | No |

## Important notes
- These identities are not seeded in any database and will not log in as-is.
- `.invalid` emails are not deliverable.
- Create matching users only in an isolated staging/test database, never production.
- For valid-login tests, inject passwords using `NC_TEST_CONSUMER_PASSWORD` or `NC_TEST_WORKER_PASSWORD`; do not commit secrets.
- `WrongPass123` is only a dummy incorrect password for negative testing.
- No accounts have been created and no tests have been executed as part of generating this document.

## Staging setup checklist
1. Confirm staging is isolated from production.
2. Seed test users through the approved test setup.
3. Configure required roles and verification status.
4. Set valid test passwords as environment secrets.
5. Run tests against staging and inspect sanitized PASS/FAIL reports.
