# My AI Website Testing Agent (Python + Playwright + Pytest)

Tests **NurseConnect** (nurse, consumer) and **Hospital Dashboard** (reception, doctor).
No Claude/ChatGPT agent is involved - it is your own code, runs locally.

## 1. Setup (Windows, PowerShell or CMD, inside this folder)
```
setup_windows.bat
notepad .env
```
Fill the 4 `*_PASSWORD` values in `.env`. The file is git-ignored. Passwords are never printed or written to reports.

## 2. Run
```
run_tests.bat                       # everything, headless
run_tests.bat --headed              # watch the browser
run_tests.bat --phase health        # health | login | discovery | pages | forms | permissions | workflows
run_tests.bat --write               # let workflows create AUTOTEST_* records  (STAGING ONLY)
```
Start with `--phase health`, then `login`, then `discovery`. Only continue when each is green/understood.

## 3. Output (folder `artifacts/`)
- `reports/report_<id>.html` - summary, prioritised bug list, every step with evidence
- `reports/report_<id>.json` - same data, machine readable
- `screenshots/` - automatic on every FAIL (and key steps)
- `inventory/inventory_<role>.json` - every page/menu/form/button the crawler found per role

## 4. Phases -> files
| Phase | File | What it does |
|---|---|---|
| 1 Health | test_01_health.py | URL up, content, login form, console errors, HTTPS |
| 2 Login | test_02_login.py | 4 roles valid login; invalid + empty login using a FAKE account (cannot lock real users) |
| 3 Discovery | test_03_discovery.py | Read-only crawl; checks the menu items from your screenshots really exist |
| 4 Pages | test_04_pages.py | Load, redirects, console/network errors, reload consistency, search, tabs, back/forward |
| 5 Forms | test_05_forms.py | Empty / invalid submit; **all POST/PUT/DELETE are blocked** during the test |
| 6 Permissions | test_06_permissions.py | Anonymous access, and pages seen only by one role opened by the other |
| 7 Workflows | test_07_workflows.py | Reception / doctor / nurse / consumer flows, found from the real UI |

## 5. Safety rules built in
- Never clicks anything matching logout/delete/remove/discharge/cancel/pay/refund... (`DANGEROUS` in `config.py`).
- Creating data only with `--write`; everything is tagged `AUTOTEST_<runid>`; payments are never automated.
- Status meanings: PASS = assertions ran and held. BLOCKED = feature not found / credentials missing / write disabled. NOT TESTED = intentionally skipped (reason shown).
- Screenshots can show on-screen data: use test patients only, and treat the `artifacts` folder as sensitive.

## 6. Adapting to your real UI
Selectors are generic (heuristics) because they were not verified on your live sites.
If a stage is BLOCKED with "not found", open `inventory_<role>.json`, find the real button/menu text,
and edit the regex in `tests/test_07_workflows.py` (`WORKFLOWS` list). Add real field expectations the same way.

## 7. CI later
After the local run is stable: GitHub Actions with `pip install -r requirements.txt`, `playwright install --with-deps chromium`,
secrets as environment variables, `python run.py`, and upload `artifacts/` as a build artifact.
