"""Phase 8: Reception -> Add patient -> "Patient Information - OPD Registration" (real form, filled by the agent).

Steps
  1. Open the form (sidebar "Add patient", falls back to the direct URL)
  2. Empty submit is rejected (all POST/PUT/PATCH/DELETE are blocked during this step)
  3. Agent FILLS the whole form with fake data (nothing is sent to the server)
  4. Agent SUBMITS the form            -> only with --write / ALLOW_WRITE_TESTS=true
  5. Agent finds the new patient by mobile number in the top search box -> only with --write
Fake patient: first name AUTOTEST, last name AUTOTEST_<runid>, mobile 9xxxxxxxxx (derived from the run id).
"""
import json
import re
from urllib.parse import urljoin

import pytest
from playwright.sync_api import expect

from config import ALLOW_WRITE, APPS, ART, ROLES, RUN_ID, TEST_MARKER
from pages.app_page import AppPage
from utils.recorder import REC, Blocked, NotTested

T = "08_opd_registration"
ROLE = "reception"
FORM_PATH = "/hospital-dashboard/reception/addPatient/opd"      # from your screenshot

FIRST = "AUTOTEST"
LAST = TEST_MARKER
MOBILE = "9" + str(int(RUN_ID, 16) % 10**9).zfill(9)            # 10 digits, unique per run
SUBMIT_RX = re.compile(r"^\s*(register|submit|save|add patient|create|book|confirm)", re.I)


def rx(text):
    return re.compile(text, re.I)


_ROOT = {"loc": None}          # the ONE live copy of the form (the page renders the form twice)


def scope(page):
    return _ROOT["loc"] if _ROOT["loc"] is not None else page


_LIVE_JS = """() => {
  document.querySelectorAll('[data-autotest-live]').forEach(e => e.removeAttribute('data-autotest-live'));
  const inputs = [...document.querySelectorAll('input')].filter(i => {
    const fc = i.closest('.MuiFormControl-root, .MuiTextField-root'); const l = fc && fc.querySelector('label');
    return l && /^\\s*First Name/i.test(l.innerText); });
  const info = [];
  inputs.forEach((inp, idx) => {
    let root = null, e = inp;
    while ((e = e.parentElement)) { if ([...e.querySelectorAll('button')].some(b => /save patient/i.test(b.innerText))) { root = e; break; } }
    inp.scrollIntoView({block: 'center'});
    const r = inp.getBoundingClientRect();
    const top = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
    const live = !!(root && top && root.contains(top));
    const cs = getComputedStyle(root || inp);
    info.push({idx, live, hasRoot: !!root, opacity: cs.opacity, visibility: cs.visibility, x: Math.round(r.x), y: Math.round(r.y)});
    if (live) root.setAttribute('data-autotest-live', '1');
  });
  return info;
}"""


def lock_live_form(page):
    """The page contains 2 copies of the form. Find the one that is really on top and use only that."""
    _ROOT["loc"] = None
    try:
        info = page.evaluate(_LIVE_JS)
    except Exception as e:
        return f"live-form detection failed: {str(e)[:100]}"
    live = [i for i in info if i["live"]]
    if len(live) == 1:
        _ROOT["loc"] = page.locator('[data-autotest-live="1"]').first
    return f"form copies={len(info)}, live copies={len(live)}, details={info}"


SLOT_BASE = int(RUN_ID, 16) % 48


def slot(n=0):
    """A different appointment time per run (08:00 - 19:45, 15-minute steps) so doctor slots don't collide."""
    i = (SLOT_BASE + n) % 48
    return f"{8 + i // 4:02d}:{(i % 4) * 15:02d}"


def first_visible(loc):
    """The page can contain duplicate (hidden) copies of an element: return the first visible one."""
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return loc.first


def field(page, label):
    """Find an input by its (floating) label text, e.g. 'First Name' matches 'First Name *'."""
    sc = scope(page)
    for loc in (sc.get_by_label(rx(rf"^{label}")),
                sc.get_by_role("combobox", name=rx(rf"^{label}")),
                sc.get_by_role("textbox", name=rx(rf"^{label}"))):
        if loc.count():
            return first_visible(loc)
    return sc.get_by_label(rx(rf"^{label}")).first


def dropdown_box(page, label):
    """The visible, clickable part of an MUI Select / Autocomplete (not its hidden native input)."""
    controls = scope(page).locator(".MuiFormControl-root, .MuiAutocomplete-root, .MuiTextField-root").filter(has_text=rx(label))
    for i in range(controls.count()):
        el = controls.nth(i)
        if not el.is_visible():
            continue
        inner = el.locator('[role=combobox], .MuiSelect-select, input:not([aria-hidden=true]):not([type=hidden])')
        for j in range(inner.count()):
            if inner.nth(j).is_visible():
                return inner.nth(j), el
        return el, el
    box = field(page, label)
    return box, box


def pick_first_option(page, label, index=0):
    """Open a dropdown (MUI Select / Autocomplete) and choose the first real option."""
    box, container = dropdown_box(page, label)
    try:
        expect(box).to_be_visible(timeout=10000)
        expect(box).to_be_enabled(timeout=15000)      # Doctor stays disabled until Department is chosen
        box.click()
        opts = page.get_by_role("option")
        try:
            opts.first.wait_for(timeout=10000)
        except Exception:
            box.press("ArrowDown")
            opts.first.wait_for(timeout=5000)
        real = []
        for i in range(opts.count()):
            o = opts.nth(i)
            if not o.is_visible():
                continue
            txt = o.inner_text().strip()
            if txt and not re.match(r"^\s*(select|choose|--)", txt, re.I):     # skip placeholders like "Select Doctor"
                real.append((o, txt))
        if index >= len(real):
            page.keyboard.press("Escape")
            raise AssertionError(f"no real option #{index} for '{label}' (real options: {[x[1] for x in real]})")
        opt, chosen = real[index]
        opt.click()
        page.wait_for_timeout(600)
        return chosen
    except Exception as e:
        try:
            html = container.evaluate("e => e.outerHTML")[:700]
        except Exception:
            html = "(could not read HTML)"
        raise AssertionError(f"Could not pick option for '{label}': {str(e).splitlines()[0][:200]} | HTML: {html}")


def pick_department_and_doctor(page):
    """Try departments one by one until the Doctor dropdown becomes enabled, then pick a doctor."""
    tried = []
    for idx in range(8):
        try:
            dept = pick_first_option(page, "Department", idx)
        except AssertionError:
            break                                   # no more departments
        doc_box, _ = dropdown_box(page, "Doctor")
        try:
            expect(doc_box).to_be_enabled(timeout=5000)
            doctor = pick_first_option(page, "Doctor")
            return dept, doctor, tried
        except Exception:
            page.keyboard.press("Escape")
            tried.append(dept)
    raise AssertionError(f"No department with a real doctor found. Departments tried: {tried}")


def submit_button(page):
    btns = scope(page).get_by_role("button", name=SUBMIT_RX)       # buttons only -> never the sidebar "Add patient" link
    for i in range(btns.count()):
        b = btns.nth(i)
        if b.is_visible():
            return b
    return None


def fill_remaining_required(page, container):
    """Fill any other visible, empty, required text-like field (the form continues below the visible area)."""
    extra = []
    inputs = container.locator("input:visible, textarea:visible")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            kind = (el.get_attribute("type") or "text").lower()
            if kind in ("hidden", "password", "file", "checkbox", "radio", "submit", "button", "search"):
                continue
            if el.input_value() or el.is_disabled() or el.get_attribute("readonly") is not None:
                continue
            if el.get_attribute("role") == "combobox" or el.get_attribute("aria-haspopup"):
                continue
            cls = el.get_attribute("class") or ""
            if el.get_attribute("aria-hidden") == "true" or "nativeInput" in cls or el.get_attribute("tabindex") == "-1":
                continue                                   # hidden inputs that belong to MUI dropdowns
            if not (el.get_attribute("required") is not None or el.get_attribute("aria-required") == "true"):
                continue
            hint = " ".join(filter(None, [el.get_attribute("name"), el.get_attribute("id"), el.get_attribute("aria-label")]))
            lbl = el.evaluate("e => (e.closest('.MuiFormControl-root, .MuiTextField-root')?.querySelector('label')?.innerText || '').trim()")
            if re.match(r"^\s*(first name|last name|mobile|appointment time|guardian)", lbl, re.I):
                extra.append(f"SKIPPED {lbl} (possible duplicate copy)")
                continue
            el.fill(AppPage.safe_value(kind, hint))
            extra.append(f"{lbl or '?'} ({kind})")
        except Exception:
            continue
    return extra


@pytest.mark.skipif(ROLE not in ROLES, reason="reception role not active (APP_ONLY)")
def test_opd_registration(need_session):
    s = need_session(ROLE, T)
    page, mon = s.page, s.mon
    app = AppPage(page)
    reached = {"form": False, "filled": False}

    # 1 ---------------------------------------------------------------
    with REC.step(T, "Open Add patient (OPD Registration) form", ROLE,
                  "Form 'Patient Information - OPD Registration' is shown", page, mon, "High", shot=True) as st:
        if not app.open_section(r"^\s*Add patient\s*$"):
            app.goto(urljoin(APPS[ROLES[ROLE].app].base + "/", FORM_PATH.lstrip("/")))
        app.settle()
        # "Add patient" shows a chooser (Emergency / In Patient / OPD): click the OPD card
        opd_card = page.locator('a[href$="/addPatient/opd"]').first
        if opd_card.count() and opd_card.is_visible():
            opd_card.click()
            app.settle()
        st.actual = f"url={page.url}"
        assert not app.is_login(), "Redirected to login (session lost)"
        expect(page.get_by_text(rx(r"OPD Registration")).first).to_be_visible(timeout=10000)
        expect(field(page, "First Name")).to_be_visible(timeout=10000)
        page.wait_for_timeout(1500)                  # let page-transition copies disappear
        st.actual += " | " + lock_live_form(page)
        reached["form"] = True

    # 2 ---------------------------------------------------------------
    attempted = []

    def guard(route, request):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            attempted.append(f"{request.method} {request.url[:120]}")
            route.abort("blockedbyclient")
        else:
            route.continue_()

    with REC.step(T, "Empty submit is rejected", ROLE,
                  "Required fields show an error and nothing is sent to the server", page, mon, "Medium", shot=True) as st:
        if not reached["form"]:
            raise Blocked("Form not reached")
        btn = submit_button(page)
        if btn is None:
            raise Blocked("No submit button (Register/Submit/Save/...) found - check the label and edit SUBMIT_RX")
        btn.scroll_into_view_if_needed()
        page.route("**/*", guard)
        try:
            btn.click()
            page.wait_for_timeout(800)
        finally:
            page.unroute("**/*", guard)
        err = page.locator('[role=alert], .Mui-error, [class*=error i], [aria-invalid="true"], :invalid').count()
        st.actual = f"error/invalid elements={err}; mutating requests attempted={attempted}"
        assert not attempted, "Empty form was sent to the server"
        assert err > 0, "No validation feedback for empty required fields"

    # 3 ---------------------------------------------------------------
    with REC.step(T, "Agent fills the OPD registration form", ROLE,
                  "All fields accept the fake patient data; Doctor list loads after Department", page, mon, "High", shot=True) as st:
        if not reached["form"]:
            raise Blocked("Form not reached")
        page.evaluate("window.scrollTo(0, 0)")
        log = []

        field(page, "First Name").fill(FIRST)
        field(page, "Last Name").fill(LAST)
        field(page, "Date of Birth").fill("1990-01-01")          # <input type=date> wants yyyy-mm-dd
        first_visible(scope(page).get_by_role("button", name=rx(r"^Male$"))).click()   # gender chip
        field(page, "Mobile Number").fill(MOBILE)
        dept, doctor, skipped = pick_department_and_doctor(page)
        log.append(f"department={dept}")
        log.append(f"doctor={doctor}")
        if skipped:
            log.append(f"departments without doctors={skipped}")
        t = field(page, "Appointment Time")
        t.fill(slot(0))
        field(page, "Guardian Name").fill("AUTOTEST Guardian")
        extra = fill_remaining_required(page, scope(page) if _ROOT["loc"] is not None else page.locator("body"))

        # verify the values really stuck
        assert field(page, "First Name").input_value() == FIRST, "First Name did not keep its value"
        assert field(page, "Mobile Number").input_value() == MOBILE, "Mobile Number did not keep its value"
        assert field(page, "Appointment Time").input_value(), "Appointment Time is empty"
        log.append(f"scoped to live form copy={_ROOT['loc'] is not None}")
        st.actual = f"filled; {', '.join(log)}; extra required fields filled={extra}; mobile={MOBILE}"
        reached["filled"] = True
        page.screenshot(path=str(__import__("config").SHOTS / f"{RUN_ID}_opd_form_filled.png"), full_page=True)

    # 4 ---------------------------------------------------------------
    with REC.step(T, f"Submit the form ({TEST_MARKER})", ROLE,
                  "Success message appears / patient is registered", page, mon, "High", shot=True) as st:
        if not ALLOW_WRITE:
            raise Blocked("ALLOW_WRITE_TESTS=false - form was filled but NOT submitted (run with --write to submit)")
        if not reached["filled"]:
            raise Blocked("Form was not filled")
        btn = submit_button(page)
        if btn is None:
            raise Blocked("No submit button found")
        status, where, alerts, used, resp_body = 0, "", "", [], ""
        for attempt in range(8):
            used.append(slot(attempt))
            btn = submit_button(page)
            btn.scroll_into_view_if_needed()
            try:
                with page.expect_response(lambda r: r.request.method in ("POST", "PUT", "PATCH"), timeout=15000) as ri:
                    btn.click()
                resp = ri.value
                status, where = resp.status, f"{resp.request.method} {resp.url[:110]}"
                try:
                    resp_body = resp.text()[:1500]
                except Exception:
                    resp_body = ""
            except Exception:
                status, where = 0, "no POST/PUT request was sent"
            page.wait_for_timeout(700)
            alerts = " / ".join(dict.fromkeys(a.strip() for a in page.locator('[role="alert"]').all_inner_texts() if a.strip()))[:200]
            if status == 409 and "slot" in alerts.lower():          # doctor slot already booked -> try the next slot
                field(page, "Appointment Time").fill(slot(attempt + 1))
                page.wait_for_timeout(500)
                continue
            break
        app.settle()
        st.actual = f"server response={status} ({where}); slots tried={used}; alerts='{alerts}'; url={page.url}"
        assert 200 <= status < 300, f"Save did not succeed: {where} -> {status}; alerts='{alerts}'"
        try:                                           # hand the new patient over to test_09 (patient details)
            (ART / "last_patient.json").write_text(json.dumps(
                {"run_id": RUN_ID, "last_name": LAST, "mobile": MOBILE, "slot": used[-1], "response": resp_body}), encoding="utf-8")
        except Exception:
            pass

    # 5 ---------------------------------------------------------------
    with REC.step(T, "Find the new patient by mobile number", ROLE,
                  f"Searching {MOBILE} in the top search box shows the patient", page, mon, "High", shot=True) as st:
        if not ALLOW_WRITE:
            raise Blocked("Nothing was created (write tests disabled)")
        box = page.get_by_placeholder(rx(r"search patient by mobile")).first
        if not box.count():
            raise Blocked("Top search box not found")
        box.fill(MOBILE)
        box.press("Enter")
        app.settle()
        page.wait_for_timeout(1500)
        found = LAST in app.body_text() or MOBILE in app.body_text()
        where = "top search"
        if not found:
            if app.open_section(r"^\s*Patient list\s*$"):
                app.settle()
                page.wait_for_timeout(1500)
                found = LAST in app.body_text() or MOBILE in app.body_text()
                where = "Patient list page"
        snippet = re.sub(r"\s+", " ", app.body_text())[:350]
        st.actual = (f"patient visible via {where}" if found
                     else f"patient NOT found (searched mobile / {LAST} in top search and Patient list). Page text: {snippet}")
        assert found, "Registered patient not found via mobile search or Patient list"