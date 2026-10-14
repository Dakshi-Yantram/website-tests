"""Phase 5: form validation & safe negative tests.
All non-GET requests are BLOCKED while a form is tested, so nothing can be created on the server."""
import re
import pytest

from config import DANGEROUS
from conftest import ROLE_NAMES
from pages.app_page import AppPage, path_of
from utils.recorder import REC, NotTested

T = "05_forms"
SUBMIT = 'button[type=submit], input[type=submit], button:has-text("Save"), button:has-text("Submit"), button:has-text("Register"), button:has-text("Add"), button:has-text("Create"), button:has-text("Book")'


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_forms(role, need_session, inventories):
    s = need_session(role, T)
    page, mon = s.page, s.mon
    app = AppPage(page)
    pages = [p for p in inventories(role) if p.get("forms") and not p.get("error")]
    if not pages:
        REC.add(T, "Forms on discovered pages", role, "Forms found to test", "NOT TESTED", "No <form> elements found on discovered pages (modals opened by buttons are covered by the workflow tests)")
        return

    for info in pages:
        url = info["url"]
        app.goto(url)
        forms = page.locator("form:visible")
        for i in range(min(forms.count(), 5)):
            form = forms.nth(i)
            name = f"{path_of(url)} form#{i+1}"
            if form.locator('input[type=password]').count():
                REC.add(T, f"{name}: validation", role, "-", "NOT TESTED", "Contains a password field (login / credentials form) - skipped for safety")
                continue
            btn = form.locator(SUBMIT).first
            label = (btn.inner_text() if btn.count() else "").strip()
            attempted = []

            def guard(route, request):
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    attempted.append(f"{request.method} {request.url[:120]}")
                    route.abort("blockedbyclient")
                else:
                    route.continue_()

            with REC.step(T, f"{name}: empty submit is rejected", role,
                          "Required fields block submit (validation message) and nothing is sent to server", page, mon, "Medium", shot=True) as st:
                if not btn.count():
                    raise NotTested("No recognisable submit button")
                if DANGEROUS.search(label):
                    raise NotTested(f"Submit button '{label}' looks destructive")
                required = form.locator("[required]").count()
                if required == 0:
                    raise NotTested("No required fields declared - expected behaviour unknown; add an expectation manually")
                page.route("**/*", guard)
                try:
                    btn.click(); page.wait_for_timeout(700)
                finally:
                    page.unroute("**/*", guard)
                invalid = form.evaluate("f => f.querySelectorAll(':invalid').length")
                err = page.locator('[role=alert], [class*=error i], [class*=invalid i]').count()
                st.actual = f"required={required}, invalid fields={invalid}, error elements={err}, mutating requests attempted={attempted}"
                assert not attempted, "Empty form was submitted to the server"
                assert invalid > 0 or err > 0, "No validation feedback"

            email = form.locator('input[type=email]')
            if email.count() and email.first.is_visible():
                with REC.step(T, f"{name}: invalid email rejected", role, "Malformed email blocked, nothing sent", page, mon, "Medium") as st:
                    if DANGEROUS.search(label) or not btn.count():
                        raise NotTested("No safe submit button")
                    email.first.fill("not-an-email")
                    page.route("**/*", guard)
                    try:
                        btn.click(); page.wait_for_timeout(700)
                    finally:
                        page.unroute("**/*", guard)
                    bad = email.first.evaluate("e => !e.validity.valid")
                    st.actual = f"email invalid flag={bad}; attempted={attempted}"
                    assert bad and not attempted, "Malformed email was accepted / sent"
