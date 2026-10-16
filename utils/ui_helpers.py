"""Small Playwright helpers shared by the MUI-form tests (dropdowns, dialogs, save + server response)."""
import re

from playwright.sync_api import expect


def rx(pattern):
    return re.compile(pattern, re.I)


def first_visible(loc):
    """The page can hold duplicate copies of an element: return the first visible one."""
    for i in range(loc.count()):
        if loc.nth(i).is_visible():
            return loc.nth(i)
    return loc.first


def field(root, pattern):
    """Find an input by its label (regex) inside `root` (a Page or a Locator such as a dialog)."""
    for loc in (root.get_by_label(rx(pattern)),
                root.get_by_role("textbox", name=rx(pattern)),
                root.get_by_role("spinbutton", name=rx(pattern)),
                root.get_by_role("combobox", name=rx(pattern)),
                root.get_by_placeholder(rx(pattern))):
        if loc.count():
            return first_visible(loc)
    raise AssertionError(f"Field not found: /{pattern}/")


def get_dialog(page):
    dlg = page.get_by_role("dialog")
    try:
        dlg.first.wait_for(timeout=10000)
    except Exception:
        dlg = page.locator(".MuiDialog-paper, .MuiModal-root")
        dlg.first.wait_for(timeout=5000)
    return first_visible(dlg)


def dropdown_box(root, pattern):
    controls = root.locator(".MuiFormControl-root, .MuiAutocomplete-root, .MuiTextField-root").filter(has_text=rx(pattern))
    for i in range(controls.count()):
        el = controls.nth(i)
        if not el.is_visible():
            continue
        inner = el.locator('[role=combobox], .MuiSelect-select, input:not([aria-hidden=true]):not([type=hidden])')
        for j in range(inner.count()):
            if inner.nth(j).is_visible():
                return inner.nth(j), el
        return el, el
    raise AssertionError(f"Dropdown not found: /{pattern}/")


def pick_option(page, root, pattern, index=0):
    """Open an MUI Select/Autocomplete and choose real option #index (placeholders like 'Select ...' are skipped).
    If there are fewer options than index+1, the last real option is used."""
    box, container = dropdown_box(root, pattern)
    try:
        expect(box).to_be_enabled(timeout=15000)
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
            if txt and not re.match(r"^\s*(select|choose|--)", txt, re.I):
                real.append((o, txt))
        if not real:
            page.keyboard.press("Escape")
            raise AssertionError("dropdown has no real options")
        opt, chosen = real[min(index, len(real) - 1)]
        opt.click()
        page.wait_for_timeout(500)
        return chosen
    except Exception as e:
        try:
            html = container.evaluate("e => e.outerHTML")[:500]
        except Exception:
            html = "(no html)"
        raise AssertionError(f"Could not pick option for /{pattern}/: {str(e).splitlines()[0][:200]} | HTML: {html}")


def click_and_capture(page, btn, timeout=15000):
    """Click a save button and return (status, 'METHOD url', response_text) of the POST/PUT/PATCH it triggers."""
    try:
        with page.expect_response(lambda r: r.request.method in ("POST", "PUT", "PATCH"), timeout=timeout) as ri:
            btn.click()
        r = ri.value
        try:
            body = r.text()[:300]
        except Exception:
            body = ""
        return r.status, f"{r.request.method} {r.url[-80:]}", body
    except Exception:
        return 0, "no POST/PUT/PATCH request was sent", ""


def alerts_text(page):
    texts = [a.strip() for a in page.locator('[role="alert"]').all_inner_texts() if a.strip()]
    return " / ".join(dict.fromkeys(texts))[:200]


def button_names(root):
    try:
        return [t.strip() for t in root.get_by_role("button").all_inner_texts() if t.strip()][:25]
    except Exception:
        return []


def type_and_pick(page, root, pattern, texts=("fe", "co", "he", "pa", "ab")):
    """For search-as-you-type dropdowns (e.g. Symptom: 'Enter 2 letters'): type, wait for options, pick the first."""
    box = field(root, pattern)
    tried = []
    for txt in texts:
        box.click()
        try:
            box.fill("")
        except Exception:
            pass
        box.press_sequentially(txt, delay=120)
        opts = page.get_by_role("option")
        try:
            opts.first.wait_for(timeout=6000)
        except Exception:
            tried.append(txt)
            continue
        for i in range(opts.count()):
            o = opts.nth(i)
            if o.is_visible():
                name = o.inner_text().strip()
                if name and not re.match(r"^\s*(no options|loading)", name, re.I):
                    o.click()
                    page.wait_for_timeout(500)
                    return name
        tried.append(txt)
    raise AssertionError(f"No options appeared for /{pattern}/ after typing {tried}")


def open_tab(page, pattern):
    """Switch to a tab of the patient profile (SYMPTOMS / VITALS / PRESCRIPTION / REPORTS ...)."""
    for loc in (page.get_by_role("tab", name=rx(pattern)),
                page.get_by_role("button", name=rx(pattern)),
                page.get_by_text(rx(pattern))):
        el = first_visible(loc)
        if loc.count() and el.is_visible():
            el.scroll_into_view_if_needed()
            el.click()
            page.wait_for_timeout(900)
            return
    raise AssertionError(f"Tab not found: /{pattern}/")


def _fill_value(el, value):
    """Fill an input; for number inputs keep the value inside the field's own min/max. Returns True if it stuck."""
    v = str(value)
    try:
        if (el.get_attribute("type") or "").lower() == "number":
            f = float(v)
            mn, mx = el.get_attribute("min"), el.get_attribute("max")
            if mn not in (None, "") and f < float(mn):
                v = mn
            if mx not in (None, "") and f > float(mx):
                v = mx
    except Exception:
        pass
    el.fill(v)
    return el.input_value() == v


def fill_number_inputs(root, values):
    """Fill the visible number inputs of `root` in DOM order (Oxygen, Respiratory, BP high, BP low, ...)."""
    inputs = root.locator("input[type=number]")
    nums = [inputs.nth(i) for i in range(inputs.count()) if inputs.nth(i).is_visible()]
    if len(nums) < len(values):
        raise AssertionError(f"Expected {len(values)} number inputs, found {len(nums)}")
    for el, v in zip(nums, values):
        if not _fill_value(el, v):
            raise AssertionError(f"Value {v} did not stay in the field")
    got = [n.input_value() for n in nums[:len(values)]]
    return got


def fill_checked(root, pattern, value):
    """Fill a labelled field and verify it kept the value. Looks inside the field's own MUI container first
    (labels can be wired to the wrong input), then falls back to the label lookup."""
    controls = root.locator(".MuiFormControl-root, .MuiTextField-root").filter(has_text=rx(pattern))
    for i in range(controls.count()):
        inp = controls.nth(i).locator("input:not([aria-hidden=true]):not([type=hidden]), textarea").first
        try:
            if inp.count() and inp.is_visible() and not inp.is_disabled() and _fill_value(inp, value):
                return
        except Exception:
            continue
    el = field(root, pattern)
    if not _fill_value(el, value):
        raise AssertionError(f"Could not fill /{pattern}/ with {value}")