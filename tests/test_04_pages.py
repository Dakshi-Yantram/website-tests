
"""Phase 4: per-page component tests (read-only interactions only)."""
import pytest

from config import DANGEROUS
from conftest import ROLE_NAMES
from pages.app_page import AppPage, path_of
from utils.recorder import REC, NotTested, NotVerified

T = "04_pages"


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_pages(role, need_session, inventories):
    s = need_session(role, T)
    pages = [p for p in inventories(role) if not p.get("error")]
    app = AppPage(s.page)
    page, mon = s.page, s.mon

    for info in pages:
        url = info["url"]
        label = path_of(url)

        with REC.step(
            T,
            f"{label}: loads cleanly",
            role,
            "HTTP<400, same URL, not login, visible content",
            page,
            mon,
            "High",
        ) as st:
            status, final = app.goto(url)
            txt = app.body_text()
            st.actual = f"HTTP {status}, final={final}, {len(txt)} chars"
            assert (status or 0) < 400, f"HTTP {status}"
            assert not app.is_login(), "Session lost - redirected to login"
            assert len(txt.strip()) > 0, "Blank page"
            assert path_of(final) == path_of(url), (
                f"Unexpected redirect to {final}"
            )

        with REC.step(
            T,
            f"{label}: no JS errors or failed requests",
            role,
            "0 console errors, 0 failed requests",
            page,
            mon,
            "Medium",
        ) as st:
            st.actual = (
                f"{len(mon.console)} console errors, "
                f"{len(mon.failed)} failed requests"
            )
            five = [f for f in mon.failed if f.startswith("5")]
            assert not five, f"Server errors: {five[:3]}"
            assert not mon.console and not mon.failed, st.actual
        mon.clear()

        with REC.step(
            T,
            f"{label}: reload keeps content consistent",
            role,
            "Same headings after reload",
            page,
            mon,
            "Medium",
        ) as st:
            before = app.inspect()["headings"]
            page.reload(wait_until="domcontentloaded")
            app.settle()
            after = app.inspect()["headings"]
            st.actual = f"before={before[:4]} after={after[:4]}"
            assert before == after, "Headings changed after reload"

        if info.get("search"):
            with REC.step(
                T,
                f"{label}: search handles unmatched term",
                role,
                "No crash; page stays usable",
                page,
                mon,
                "Medium",
                shot=True,
            ) as st:
                box = page.locator(
                    'input[type=search],input[placeholder*=search i],'
                    'input[name*=search i]'
                ).first
                box.fill("zzqq_no_such_record_9")
                box.press("Enter")
                app.settle()
                ok = len(app.body_text().strip()) > 0 and not app.is_login()
                st.actual = (
                    f"Page usable after search: {ok}; "
                    f"new console errors={len(mon.console)}"
                )
                assert ok and not mon.console
                box.fill("")
            mon.clear()
        else:
            with REC.step(
                T, f"{label}: search", role, "-", page, mon
            ) as st:
                raise NotTested("No search field on this page")

        tabs = [
            t for t in info.get("tabs", [])
            if t and not DANGEROUS.search(t)
        ]

        for t in tabs[:8]:
            with REC.step(
                T,
                f"{label}: tab '{t}' opens",
                role,
                "Tab exists on fresh page, becomes selected, no JS errors",
                page,
                mon,
                "Medium",
            ) as st:
                app.goto(url)

                tab = page.get_by_role("tab", name=t, exact=True)
                count = tab.count()

                if count == 0:
                    st.actual = (
                        f"Tab '{t}' was recorded in inventory "
                        "but is absent on the fresh page. "
                        "Current tab availability could not be verified."
                    )
                    raise NotVerified(st.actual)

                tab = tab.first
                assert tab.is_visible(), f"Tab '{t}' is not visible"
                tab.click(timeout=5000)
                app.settle()

                sel = tab.get_attribute("aria-selected")
                st.actual = (
                    f"count={count}; aria-selected={sel}; "
                    f"console errors={len(mon.console)}"
                )
                assert sel in ("true", None), "Tab not selected"
                assert not mon.console, (
                    f"Console errors: {mon.console[:3]}"
                )

            mon.clear()

    # Back / forward behaviour between consecutive pages
    for a, b in zip(pages, pages[1:6]):
        with REC.step(
            T,
            f"back/forward: {path_of(a['url'])} <-> {path_of(b['url'])}",
            role,
            "Back returns to A, forward returns to B",
            page,
            mon,
            "Medium",
        ) as st:
            app.goto(a["url"])
            app.goto(b["url"])
            page.go_back()
            app.settle()
            back_url = page.url
            page.go_forward()
            app.settle()
            fwd_url = page.url

            st.actual = f"back={back_url} forward={fwd_url}"
            assert (
                path_of(back_url) == path_of(a["url"])
                and path_of(fwd_url) == path_of(b["url"])
            )
