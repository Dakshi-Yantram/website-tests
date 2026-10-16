"""Single entry point:  python run.py [--headed] [--write] [--phase NAME] [--stop-on-fail]"""
import argparse
import os
import sys

PHASES = {
    "health": "tests/test_01_health.py", "login": "tests/test_02_login.py",
    "discovery": "tests/test_03_discovery.py", "pages": "tests/test_04_pages.py",
    "forms": "tests/test_05_forms.py", "permissions": "tests/test_06_permissions.py",
    "workflows": "tests/test_07_workflows.py",
    "opd": "tests/test_08_opd_registration.py",
    "details": "tests/test_09_patient_details.py",
}

ap = argparse.ArgumentParser()
ap.add_argument("--phase", choices=list(PHASES) + ["all"], default="all")
ap.add_argument("--headed", action="store_true", help="show the browser")
ap.add_argument("--write", action="store_true", help="allow workflows to create AUTOTEST_ records (staging only)")
ap.add_argument("--max-pages", type=int)
a = ap.parse_args()

if a.headed:
    os.environ["HEADLESS"] = "false"
if a.write:
    os.environ["ALLOW_WRITE_TESTS"] = "true"
if a.max_pages:
    os.environ["MAX_PAGES_PER_ROLE"] = str(a.max_pages)

import pytest  # noqa: E402  (after env is set)
targets = list(PHASES.values()) if a.phase == "all" else [PHASES[a.phase]]
sys.exit(pytest.main(targets))