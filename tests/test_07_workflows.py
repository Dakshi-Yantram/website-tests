"""Phase 6: end-to-end workflows, discovered from the real UI.
A stage that cannot be found in the UI is recorded BLOCKED (never invented).
Stages that create data only run when ALLOW_WRITE_TESTS=true (use staging!). Payments are never automated."""
import re
import pytest

from config import ALLOW_WRITE, ROLES, TEST_MARKER
from pages.app_page import AppPage
from qa_agent.legacy import load_legacy_workflows
from utils.recorder import REC, Blocked

T = "07_workflows"

# The flow definitions moved out of Python into legacy_workflows.yml (data only, validated on load).
# New, approval-gated YAML workflows run through `python agent.py run` (see workflows.yml).
WORKFLOWS = load_legacy_workflows()


# CI runs one site per job (APP_ONLY), so ROLES only holds that site's roles.
# Keep only the workflows whose role exists here, otherwise ROLES[role] raises KeyError.
ACTIVE_WORKFLOWS = [w for w in WORKFLOWS if w["role"] in ROLES]


@pytest.mark.parametrize("wf", ACTIVE_WORKFLOWS, ids=[w["id"] for w in ACTIVE_WORKFLOWS])
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
            before_url = page.url
            btn.click(); app.settle()
            n_fields = page.locator("form:visible input:visible, [role=dialog] input:visible, .modal input:visible").count()
            st.actual = f"{n_fields} visible input fields"
            if n_fields == 0 and (page.url != before_url or page.locator("[role=dialog], .modal").count() > 0):
                # The click worked and opened a new page/dialog (e.g. a multi-step booking wizard),
                # it just has no plain input form to fill automatically. That is not an app defect.
                raise Blocked(f"Opened {page.url} but it has no plain input form (multi-step flow?) - add role-specific steps manually")
            assert n_fields > 0, "Nothing that looks like a form appeared"
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
