"""Phase 6: end-to-end workflows, discovered from the real UI.
A stage that cannot be found in the UI is recorded BLOCKED (never invented).
Stages that create data only run when ALLOW_WRITE_TESTS=true (use staging!). Payments are never automated."""
import re
import pytest

from config import ALLOW_WRITE, TEST_MARKER
from pages.app_page import AppPage
from utils.recorder import REC, Blocked

T = "07_workflows"

WORKFLOWS = [
    dict(id="reception_patient_registration", role="reception",
         title="Reception: register patient and find them again",
         nav=[r"OPD", r"patient|registration|register"], create=r"register|new patient|add patient|create|\+", write=True),
    dict(id="reception_appointment", role="reception",
         title="Reception: create appointment / OPD encounter",
         nav=[r"OPD", r"appointment|encounter|visit|queue"], create=r"new appointment|book|schedule|create|add|\+", write=True),
    dict(id="doctor_open_record", role="doctor",
         title="Doctor: view patients and open a record, then reopen",
         nav=[r"OPD", r"appointment|patient|queue|consult"], create=None, write=False, open_first_row=True),
    dict(id="nurse_assignments_visits_docs", role="nurse",
         title="Nurse: Workspace -> Assignments -> Visits -> Documentation",
         nav=[r"Assignments", r"Visits", r"Documentation"], create=None, write=False, each_nav_is_page=True),
    dict(id="consumer_booking", role="consumer",
         title="Consumer: browse care packages and start a booking",
         nav=[r"care package|package|service"], create=r"book|select|add to cart|enrol|subscribe|choose", write=True, payment=True),
]


@pytest.mark.parametrize("wf", WORKFLOWS, ids=[w["id"] for w in WORKFLOWS])
def test_workflow(wf, need_session):
    role, test = wf["role"], f"{T}:{wf['id']}"
    s = need_session(role, test)
    page, mon = s.page, s.mon
    app = AppPage(page)
    app.goto(s.landing_url)

    reached = True
    for n, pattern in enumerate(wf["nav"], 1):
        with REC.step(test, f"Open section /{pattern}/", role, "Menu item exists and page loads without login redirect", page, mon, "High", shot=True) as st:
            if not reached:
                raise Blocked("Previous navigation stage was blocked")
            if not app.open_section(pattern):
                reached = False
                raise Blocked(f"No visible menu/link/button matching /{pattern}/ - feature not found in UI for this role")
            txt = app.body_text()
            st.actual = f"Now at {page.url}, {len(txt)} chars"
            assert not app.is_login() and len(txt.strip()) > 0
            bad = [f for f in st.new_failed() if f[:3] in ("500", "502", "503", "404", "403", "401")]
            assert not bad, f"Section opened but server returned errors: {bad[:2]}"
    mon.clear()

    if wf.get("open_first_row"):
        with REC.step(test, "Open first listed record", role, "A record/detail view opens", page, mon, "High", shot=True) as st:
            if not reached:
                raise Blocked("List page not reached")
            before = page.url
            row = page.locator("table tbody tr, [role=row], a[href*=patient i], a[href*=appointment i]").first
            if not row.count():
                raise Blocked("No rows/records found to open (empty state or unusual markup)")
            row.click(); app.settle()
            st.actual = f"before={before} after={page.url}"
            assert page.url != before or page.locator("[role=dialog], .modal").count() > 0, "Nothing opened"
        with REC.step(test, "Return to list - data still present", role, "List page still shows content after going back", page, mon, "Medium") as st:
            if not reached:
                raise Blocked("List page not reached")
            page.go_back(); app.settle()
            st.actual = f"url={page.url}"
            assert len(app.body_text().strip()) > 0
        with REC.step(test, "Save clinical data using test patient, reopen & verify", role, "Saved data visible after reopening", page, mon) as st:
            raise Blocked("Clinical save needs role-specific field selectors - add them after reviewing artifacts/inventory/inventory_doctor.json")

    if wf.get("create"):
        with REC.step(test, "Open create form / dialog", role, "A form or dialog appears", page, mon, "High", shot=True) as st:
            if not reached:
                raise Blocked("Section not reached")
            rx = re.compile(wf["create"], re.I)
            btn = page.get_by_role("button", name=rx).first
            if not btn.count():
                btn = page.get_by_role("link", name=rx).first
            if not btn.count():
                raise Blocked(f"No button/link matching /{wf['create']}/ on this page")
            btn.click(); app.settle()
            n_fields = page.locator("form:visible input:visible, [role=dialog] input:visible, .modal input:visible").count()
            st.actual = f"{n_fields} visible input fields"
            if n_fields == 0:
                # The button was picked by a text heuristic; if no form opened we cannot tell app bug from wrong button.
                raise Blocked(f"Clicked '{btn.inner_text()[:40].strip()}' but no form/dialog appeared - "
                              "set the right button/selector for this workflow")
        with REC.step(test, f"Submit with safe test data ({TEST_MARKER})", role, "Success message or record appears", page, mon, "High", shot=True) as st:
            if not ALLOW_WRITE:
                raise Blocked("ALLOW_WRITE_TESTS=false (set true only on staging/test data)")
            container = page.locator("[role=dialog]:visible, .modal:visible, form:visible").first
            filled = app.fill_form_safely(container)
            sub = container.locator('button[type=submit], button:has-text("Save"), button:has-text("Submit"), button:has-text("Register"), button:has-text("Create"), button:has-text("Add")').first
            if not sub.count():
                raise Blocked("No submit button found in the form")
            sub.click(); app.settle()
            ok = app.success_message_visible() or TEST_MARKER in app.body_text()
            st.actual = f"filled {filled} fields; success indicator={ok}"
            assert ok, "No success message and test marker not visible after submit"
        with REC.step(test, "Find the created record afterwards", role, f"{TEST_MARKER} findable via search / list", page, mon, "High", shot=True) as st:
            if not ALLOW_WRITE:
                raise Blocked("Write tests disabled, nothing was created")
            box = page.locator('input[type=search],input[placeholder*=search i]').first
            if box.count():
                box.fill(TEST_MARKER); box.press("Enter"); app.settle()
            st.actual = "marker visible" if TEST_MARKER in app.body_text() else "marker NOT visible"
            assert TEST_MARKER in app.body_text(), "Created record not found"

    if wf.get("payment"):
        with REC.step(test, "Payment in safe test mode", role, "Payment completes in gateway TEST mode", page, mon) as st:
            raise Blocked("Never automated: real payments are excluded. Configure a gateway test mode and add this step manually.")
    if wf.get("each_nav_is_page"):
        with REC.step(test, "Start/complete a visit & documentation", role, "Visit state changes and notes persist", page, mon) as st:
            raise Blocked("Changes real assignment state - add after you provide a dedicated test assignment")