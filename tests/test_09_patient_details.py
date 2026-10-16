"""Phase 9: Doctor (OPD) opens the patient created by test_08 and fills ALL the detail forms.

Order:  open patient -> Symptoms -> Vitals -> Prescription -> Medical History -> Reports (upload)
Each form is filled with fake data. Saving needs --write / ALLOW_WRITE_TESTS=true (without it the
forms are filled but NOT submitted). The server response (2xx) is the proof that a save worked.
Not automated on purpose: Transfer Patient and Handshake Patient (they hand the patient to someone else).

Run test_08 first (python run.py --phase opd --write) so there is an AUTOTEST patient; its name is read
from artifacts/last_patient.json. Without that file the newest patient named AUTOTEST is used.
"""
import base64
import datetime
import json
import re

import pytest

from config import ALLOW_WRITE, APPS, ART, ROLES
from pages.app_page import AppPage
from utils.recorder import REC, Blocked
from utils.ui_helpers import (alerts_text, button_names, click_and_capture, field, fill_checked, fill_number_inputs, first_visible,
                              get_dialog, open_tab, pick_option, rx, type_and_pick)

T = "09_patient_details"
ROLE = "doctor"
PNG_1PX = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def last_patient():
    try:
        return json.loads((ART / "last_patient.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def fill_empty_text_fields(root):
    """Fill every other visible, empty text/number/date field with fake data (the bottom of long forms is unknown)."""
    done = []
    inputs = root.locator("input:visible, textarea:visible")
    for i in range(inputs.count()):
        el = inputs.nth(i)
        try:
            kind = (el.get_attribute("type") or "text").lower()
            cls = el.get_attribute("class") or ""
            if kind in ("hidden", "password", "file", "checkbox", "radio", "submit", "button", "search", "time"):
                continue
            if el.input_value() or el.is_disabled() or el.get_attribute("readonly") is not None:
                continue
            if el.get_attribute("role") == "combobox" or el.get_attribute("aria-haspopup") or el.get_attribute("aria-hidden") == "true" \
                    or "nativeInput" in cls or el.get_attribute("tabindex") == "-1":
                continue
            lbl = el.evaluate("e => (e.closest('.MuiFormControl-root, .MuiTextField-root')?.querySelector('label')?.innerText || '').trim()")
            if kind == "date":
                el.fill(datetime.date.today().isoformat())
            elif kind in ("number", "tel") or re.search(r"mobile|phone|age|weight|height", lbl, re.I):
                el.fill("9000000000" if re.search(r"mobile|phone", lbl, re.I) else "30")
            else:
                el.fill("AUTOTEST data")
            done.append(lbl or kind)
        except Exception:
            continue
    return done


def save_step(page, st, btn, what):
    """Click the save button (only with --write) and assert the server answered 2xx."""
    if not ALLOW_WRITE:
        raise Blocked(f"{what}: form filled but NOT saved (ALLOW_WRITE_TESTS=false, run with --write)")
    status, where, body = click_and_capture(page, btn)
    page.wait_for_timeout(700)
    alerts = alerts_text(page)
    st.actual += f" | server={status} ({where}); alerts='{alerts}'"
    assert 200 <= status < 300, f"{what}: save failed {where} -> {status}; alerts='{alerts}' body={body[:150]}"


@pytest.mark.skipif(ROLE not in ROLES, reason="doctor role not active (APP_ONLY)")
def test_patient_details(need_session):
    s = need_session(ROLE, T)
    page, mon = s.page, s.mon
    app = AppPage(page)
    info = last_patient()
    marker = info.get("last_name") or "AUTOTEST"
    state = {"url": ""}

    def back_to_patient():
        app.goto(state["url"])
        app.settle()
        page.wait_for_timeout(800)

    # 1 ---------------------------------------------------------------
    with REC.step(T, f"Open the new patient ({marker}) from Patients List", ROLE,
                  "Patient profile (OPD) opens at /opd/list/<id>", page, mon, "High", shot=True) as st:
        opened = app.open_section(r"^\s*Patients? List\s*$")
        if not opened:                                    # doctor lands on the main dashboard: enter the OPD section first
            if app.open_section(r"^\s*OPD\s*$"):
                app.settle()
                opened = app.open_section(r"^\s*Patients? List\s*$")
        if not opened:                                    # last resort: the list URL (profile is /opd/list/<id>)
            app.goto(APPS[ROLES[ROLE].app].base.rstrip("/") + "/hospital-dashboard/opd/list")
        app.settle()
        page.wait_for_timeout(1500)
        cell = first_visible(page.get_by_text(marker, exact=False))
        if not cell.count():
            cell = first_visible(page.get_by_text(rx(r"AUTOTEST")))
        assert cell.count(), f"No patient named {marker} / AUTOTEST on the list (first page): {re.sub(chr(92)+'s+',' ',app.body_text())[:250]}"
        cell.click()
        app.settle()
        if not re.search(r"/opd/list/\d+", page.url):         # the row itself may need a View / link click
            row = cell.locator("xpath=ancestor::tr[1] | ancestor::*[@role='row'][1]").first
            target = row.locator("a, button").first if row.count() else None
            if target is not None and target.count():
                target.click()
                app.settle()
        page.wait_for_timeout(1000)
        st.actual = f"url={page.url}"
        assert re.search(r"/opd/list/\d+", page.url), "Patient profile did not open"
        state["url"] = re.sub(r"/edit/.*$", "", page.url)

    def need_patient():
        if not state["url"]:
            raise Blocked("Patient profile was not opened")

    # 2 Symptoms -------------------------------------------------------
    with REC.step(T, "Symptoms: add a symptom", ROLE, "Symptom saved (2xx)", page, mon, "High", shot=True) as st:
        need_patient()
        back_to_patient()
        open_tab(page, r"^\s*symptoms?\s*$")
        first_visible(page.get_by_role("button", name=rx(r"add symptoms?"))).click()
        dlg = get_dialog(page)
        sym = type_and_pick(page, dlg, r"^Symptom")          # needs 2 letters typed before options appear
        fill_checked(dlg, r"^Duration(?!\s*Type)", "3")
        first_visible(dlg.get_by_role("button", name=rx(r"^\s*add\b"))).click()          # ADD + (adds the row)
        page.wait_for_timeout(600)
        st.actual = f"symptom={sym}, duration=3 days"
        save_step(page, st, first_visible(dlg.get_by_role("button", name=rx(r"^\s*submit"))), "Symptoms")

    # 3 Vitals ---------------------------------------------------------
    with REC.step(T, "Vitals: add vitals", ROLE, "Vitals saved (2xx)", page, mon, "High", shot=True) as st:
        need_patient()
        back_to_patient()
        open_tab(page, r"^\s*vitals?\s*$")
        first_visible(page.get_by_role("button", name=rx(r"add vitals"))).click()
        dlg = get_dialog(page)
        # realistic adult values, in the order of the form: Oxygen %, Respiratory rate, BP high, BP low, Pulse, Temp (C)
        got = fill_number_inputs(dlg, ["98", "16", "120", "80", "72", "37"])
        times = dlg.locator("input[type=time]")
        for i in range(times.count()):
            if times.nth(i).is_visible():
                times.nth(i).fill("08:00")
        st.actual = f"values in form order (oxygen, resp, BP high, BP low, pulse, temp) = {got}; times 08:00"
        save_step(page, st, first_visible(dlg.get_by_role("button", name=re.compile(r"^\s*add\s*$", re.I))), "Vitals")

    # 4 Prescription ---------------------------------------------------
    with REC.step(T, "Prescription: add medicine + test", ROLE, "Prescription saved (2xx)", page, mon, "High", shot=True) as st:
        need_patient()
        back_to_patient()
        open_tab(page, r"^\s*prescriptions?\s*$")
        btn = first_visible(page.get_by_role("button", name=rx(r"add prescription")))
        btn.scroll_into_view_if_needed()
        btn.click()
        dlg = get_dialog(page)
        mtype = pick_option(page, dlg, r"^Medicine Type", 1)                    # a real medicine type, not 'No medicine'
        fill_checked(dlg, r"^Medicine Name", "AUTOTEST Paracetamol 500")
        fill_checked(dlg, r"^Dosage(?!\s*Unit)", "1")
        unit = pick_option(page, dlg, r"^Dosage Unit", 0)
        fill_checked(dlg, r"^Frequency", "1")
        first_visible(dlg.get_by_text(rx(r"BEFORE\s*BREAKFAST"))).click()       # time-of-medication chip
        fill_checked(dlg, r"^Days", "3")
        fill_checked(dlg, r"^Medicine Start Date", datetime.date.today().isoformat())
        log = [f"type={mtype}", f"unit={unit}"]
        try:                                                                      # optional lab test
            log.append(f"test={pick_option(page, dlg, r'^Test', 0)}")
            first_visible(dlg.get_by_role("button", name=re.compile(r"^\s*add\s*$", re.I))).click()
            page.wait_for_timeout(600)
        except Exception as e:
            log.append(f"test skipped ({str(e)[:60]})")
        st.actual = ", ".join(log)
        final = dlg.get_by_role("button", name=rx(r"^\s*(submit|save|prescribe|done|confirm|finish)"))
        if not final.count():
            raise AssertionError(f"No final submit button in the prescription dialog. Buttons: {button_names(dlg)}")
        save_step(page, st, first_visible(final), "Prescription")

    # 5 Medical history ------------------------------------------------
    with REC.step(T, "Medical History: fill and save", ROLE, "Medical history saved (2xx)", page, mon, "High", shot=True) as st:
        need_patient()
        app.goto(state["url"] + "/edit/medicalHistory")
        app.settle()
        page.wait_for_timeout(1200)
        fill_checked(page, r"^History Given By", "AUTOTEST Relative")
        fill_checked(page, r"^Mobile Number", "9000000000")
        rel = pick_option(page, page, r"^Relationship", 0)
        first_visible(page.get_by_role("button", name=re.compile(r"^\s*A\+\s*$"))).click()      # blood group
        first_visible(page.get_by_role("button", name=re.compile(r"^\s*Yes\s*$", re.I))).click()  # blood pressure = yes
        try:
            page.get_by_label(rx(r"^Diabetes")).first.check(timeout=3000)
        except Exception:
            pass
        extra = fill_empty_text_fields(page)
        st.actual = f"relationship={rel}; blood group A+; BP yes; other fields filled={extra}"
        btn = page.get_by_role("button", name=rx(r"^\s*(save|submit|update)"))
        if not btn.count():
            raise AssertionError(f"No Save/Submit button found. Buttons: {button_names(page)}")
        first_visible(btn).scroll_into_view_if_needed()
        save_step(page, st, first_visible(btn), "Medical history")

    # 6 Reports --------------------------------------------------------
    with REC.step(T, "Reports: upload a test image in EVERY report category", ROLE,
                  "Each category accepts an upload (2xx)", page, mon, "Medium", shot=True) as st:
        need_patient()
        back_to_patient()
        open_tab(page, r"^\s*reports?\s*$")
        png = ART / "autotest_report.png"
        png.write_bytes(PNG_1PX)

        def add_buttons():
            all_b = page.get_by_role("button", name=rx(r"add report"))
            return [all_b.nth(i) for i in range(all_b.count()) if all_b.nth(i).is_visible()]

        n = len(add_buttons())
        assert n, "No 'ADD REPORT' button found on the Reports tab"
        done, errors = [], []
        for i in range(n):
            btn = add_buttons()[i]
            try:
                title = re.sub(r"\s+", " ", btn.locator("xpath=../..").inner_text())[:30]
            except Exception:
                title = f"category {i + 1}"
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
                dlg = get_dialog(page)
                dlg.locator("input[type=file]").first.set_input_files(str(png))
                page.wait_for_timeout(800)
                sub = first_visible(dlg.get_by_role("button", name=rx(r"^\s*submit")))
                if not ALLOW_WRITE:
                    first_visible(dlg.get_by_role("button", name=rx(r"^\s*cancel"))).click()
                    done.append(f"{title}: filled (not saved)")
                    page.wait_for_timeout(500)
                    continue
                status, where, body = click_and_capture(page, sub)
                try:
                    dlg.wait_for(state="hidden", timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(600)
                done.append(f"{title}: {status}")
                if not 200 <= status < 300:
                    errors.append(f"{title}: {where} -> {status} alerts='{alerts_text(page)}'")
            except Exception as e:
                errors.append(f"{title}: {str(e).splitlines()[0][:150]}")
                page.keyboard.press("Escape")
        st.actual = f"{n} categories: " + "; ".join(done)
        if not ALLOW_WRITE:
            raise Blocked(f"Reports attached but NOT saved (ALLOW_WRITE_TESTS=false). {st.actual}")
        assert not errors, "Some uploads failed: " + " | ".join(errors)