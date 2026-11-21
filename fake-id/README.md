# fake-id/ - fictional identities and test data

Everything in this folder is **synthetic**. No file here is a real person, a real account or a secret.

| File | What it is |
|---|---|
| `test-identities.yaml` | Three fictional identities (valid consumer, invalid-login fixture, valid care professional). Passwords come from environment variables. |
| `test-data.yaml` | Dummy typed values, redirect-attack payloads, expected error patterns, limits (timeouts, login-POST budget). |
| `fake-account-document.md` | Human-readable description of the fixtures (from the agent test kit). |

## Rules
1. **Nothing here exists on the live site.** The agent never registers, creates or edits accounts anywhere.
2. **Authenticated tests run on STAGING only.** `--env production` (the default) sends no login request at all: the agent blocks every non-GET request in the browser.
3. **Passwords live only in `.env`** (git-ignored): `NC_TEST_CONSUMER_PASSWORD`, `NC_TEST_WORKER_PASSWORD`. The one literal password in the YAML (`WrongPass123`) is a dummy wrong value for the negative test.
4. Passwords, tokens and e-mail addresses are redacted from every report. If something sensitive-looking survives redaction the report is not written.

## Seeding the valid accounts on STAGING (you do this, the agent never does)
Create one consumer and one care-professional account on the **staging** backend/database only (staging registration flow, or your approved staging seed script), verify the e-mail, and put their passwords in `.env`.

> **E-mail domain caveat (unverified guess):** the backend validates e-mail with pydantic `EmailStr` (email-validator 2.3.0), which normally rejects reserved TLDs such as `.invalid`. If staging refuses `consumer.qa@example.invalid`, seed the account with an address you control and set `NC_TEST_CONSUMER_EMAIL` / `NC_TEST_WORKER_EMAIL` in `.env`. The fixture e-mail is then ignored.

The invalid-login fixture must **not** exist. Because it also uses `.invalid`, staging may answer HTTP 422 (bad e-mail format) instead of 401 (wrong password). Both are accepted as "rejected"; the report shows which one happened. To exercise the 401 path, set `NC_TEST_INVALID_EMAIL` to any well-formed address that has no account.
