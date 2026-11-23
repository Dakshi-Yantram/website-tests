# GitHub App setup (read-only access to the two private application repos)

**Status: NOT VERIFIED against real GitHub.** The workflow, `tools/github_app_token.py` and their tests were written and tested with a mocked
GitHub API and a throw-away key. Nothing was installed or tested with your real App - follow the steps below and do the checks in step 6.

## 1. Create the App (GitHub -> Settings -> Developer settings -> GitHub Apps -> New)
* Name e.g. `nurseconnect-qa-reader`. Homepage URL: your testing repo URL. **Uncheck** Webhook ("Active").
* Repository permissions: **Contents: Read-only**, **Metadata: Read-only** (Metadata is mandatory and read-only). **Everything else: No access.**
  No organization or account permissions.
* "Where can this GitHub App be installed?": *Only on this account*.
* Create it, note the **App ID**, then *Generate a private key* (downloads a `.pem`).

## 2. Install it on exactly two repositories
App page -> *Install App* -> your account/org -> **Only select repositories** -> choose the NurseConnect application repo and the Hospital
application repo (not the testing repo, not "All repositories"). After installing, the URL ends with `/installations/<NUMBER>` - that is the **installation ID**.

## 3. Store secrets and variables in the *testing* repository (Settings -> Secrets and variables -> Actions)
| Kind | Name | Value |
|---|---|---|
| Secret | `GH_APP_ID` | the App ID |
| Secret | `GH_APP_INSTALLATION_ID` | the installation ID |
| Secret | `GH_APP_PRIVATE_KEY` | full contents of the `.pem` (including the BEGIN/END lines) |
| Variable | `NURSECONNECT_APP_REPO` | `owner/nurseconnect-app-repo` |
| Variable | `HOSPITAL_APP_REPO` | `owner/hospital-app-repo` |
Then delete the downloaded `.pem` from your computer (or keep it in a password manager). Never paste it in chat, issues or code.

## 4. What the workflow does (`discover` job, manual: Actions -> Run workflow -> *run_discovery*)
1. Fails immediately, with a clear message, if any secret/variable above is missing (nothing is checked out).
2. `tools/github_app_token.py mint` signs a 10-minute JWT and requests an installation token restricted to **only the two named repositories**
   and **only `contents:read` + `metadata:read`**. If GitHub returns anything broader the token is revoked and the job fails.
   The token is masked (`::add-mask::`) and passed only as a step output - it is never printed.
3. Two `actions/checkout` steps use that token to fetch `apps/nurseconnect/` and `apps/hospital/` with `persist-credentials: false`
   (the token is not stored in `.git/config`).
4. The token is **revoked right after checkout** (`if: always()`); installation tokens also expire after 1 hour regardless.
5. Static discovery and draft generation run with no token at all. Output is uploaded as the `discovery-and-drafts` artifact for human review.

## 5. Why the agent cannot change the app repos
The token has no write permission (GitHub enforces this server-side), the checkouts hold no credentials afterwards, the job has
`permissions: contents: read`, and the discovery code never executes application code or runs `git`.

## 6. Verify once, for real
* After the first run: the token step log shows `Issued read-only token for owner/a, owner/b ... (value hidden)`.
* In the App settings -> *Advanced* the request log shows the token creation; the permissions listed are only contents/metadata read.
* Try (locally, with a token you mint by hand) `git push` to an app repo - it must be refused (HTTP 403).
* Rotate the private key (Generate new, delete old) at least yearly or whenever someone who had access leaves.

## Pinning actions
Workflows reference actions by exact release (`actions/checkout@v4.2.2`, `actions/setup-python@v5.3.0`, `actions/upload-artifact@v4.4.3`).
These release numbers were written from memory and **not verified**; before relying on CI, check each tag exists and replace it with the full
40-character commit SHA of that release (`git ls-remote https://github.com/actions/checkout refs/tags/v4.2.2`), keeping the version as a comment.
A test (`test_ci_actions_are_pinned_to_exact_versions_or_shas`) fails if any action is left on a floating tag such as `@v4`.
Optionally enable Dependabot for `github-actions` to get update PRs.

## Troubleshooting
`401` wrong App ID/private key - `404` wrong installation ID or App not installed on those repos - `403` App suspended / no access -
`422` a listed repo is not part of the installation. The script prints only these hints, never secret values.
