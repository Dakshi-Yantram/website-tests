p = 'utils/ui_helpers.py'
s = open(p, encoding='utf-8').read()
i = s.index('def click_and_capture')
j = s.index('def form_diag')
new = """def click_and_capture(page, btn, timeout=15000):
    \"\"\"Click a save button; return (status, 'METHOD url', body). If the normal click sends nothing, retry with other click styles.\"\"\"
    seen, failed, errs, tried = [], [], [], []
    on_req = lambda r: seen.append(f"{r.method} {r.url[-45:]}")
    on_fail = lambda r: failed.append(f"{r.method} {r.url[-45:]} {r.failure}")
    on_msg = lambda m: errs.append(m.text[:90]) if m.type == "error" else None
    page.on("request", on_req)
    page.on("requestfailed", on_fail)
    page.on("console", on_msg)

    def blur_then(fn):
        def run():
            try:
                page.keyboard.press("Tab")
            except Exception:
                pass
            fn()
        return run

    def slow_click():
        try:
            btn.scroll_into_view_if_needed()
            btn.hover()
        except Exception:
            pass
        page.wait_for_timeout(500)
        btn.click(delay=150)

    def key_enter():
        btn.focus()
        page.keyboard.press("Enter")

    strategies = [("click", btn.click), ("slow-click", blur_then(slow_click)),
                  ("keyboard-enter", key_enter), ("dom-click", lambda: btn.evaluate("e => e.click()"))]
    why = ""
    try:
        for n, (name, act) in enumerate(strategies):
            tried.append(name)
            try:
                with page.expect_response(lambda r: r.request.method in ("POST", "PUT", "PATCH"),
                                          timeout=timeout if n == 0 else 6000) as ri:
                    act()
                r = ri.value
                try:
                    body = r.text()[:300]
                except Exception:
                    body = ""
                return r.status, f"{r.request.method} {r.url[-80:]} (via {name})", body
            except Exception as e:
                why = (str(e).strip().splitlines() or [type(e).__name__])[0][:140]
                if any(not x.startswith("GET") for x in seen):
                    break
        info = (f"no POST/PUT/PATCH response after {tried} ({why}); requests seen={seen[-4:]}; "
                f"failed={failed[-3:]}; console errors={errs[-3:]}")
        return 0, info, ""
    finally:
        for ev, fn in (("request", on_req), ("requestfailed", on_fail), ("console", on_msg)):
            try:
                page.remove_listener(ev, fn)
            except Exception:
                pass


"""
s = s[:i] + new + s[j:]
open(p, 'w', encoding='utf-8').write(s)
print('patched:', 'strategies = [' in s)
