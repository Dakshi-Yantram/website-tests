
"""Phase 3: discover & inventory every page reachable through the UI, per role."""
import pytest

from config import EXPECTED_MENU
from conftest import ROLE_NAMES
from utils.recorder import REC

T = "03_discovery"


@pytest.mark.parametrize("role", ROLE_NAMES)
def test_discover_pages(role, need_session, inventories):
    s = need_session(role, T)
    pages = inventories(role)

    with REC.step(
        T, "Crawl finds at least one page", role,
        "Inventory not empty (saved in artifacts/inventory)",
        s.page, s.mon, "High"
    ) as st:
        st.actual = (
            f"{len(pages)} pages discovered: "
            + ", ".join(p.get("url", "?") for p in pages[:15])
        )
        assert pages, "Nothing discovered"

    # Navigation can appear as sidebar items, links, or buttons.
    with REC.step(
        T, "Discovered navigation recorded", role,
        "Navigation items or dashboard controls recorded",
        s.page, s.mon, "Low"
    ) as st:
        items = sorted({
            item.get("text", "").strip()
            for p in pages
            for key in ("sidebar", "links", "buttons")
            for item in (
                p.get(key, [])
                if key != "buttons"
                else [{"text": text} for text in p.get("buttons", [])]
            )
            if item.get("text", "").strip()
        })
        st.actual = "UI items: " + "; ".join(items)[:900]
        assert items, "No navigation links, sidebar items, or buttons found"

    for expected in EXPECTED_MENU.get(role, []):
        with REC.step(
            T, f"Expected menu item present: {expected}", role,
            f"'{expected}' should appear in this role's discovered UI",
            s.page, s.mon, "Medium"
        ) as st:
            haystack_parts = []

            for p in pages:
                for key in ("sidebar", "links"):
                    haystack_parts.extend(
                        item.get("text", "")
                        for item in p.get(key, [])
                    )

                haystack_parts.extend(p.get("headings", []))
                haystack_parts.extend(p.get("buttons", []))

            haystack = " | ".join(haystack_parts)
            found = expected.casefold() in haystack.casefold()

            st.actual = (
                "Found in discovered UI"
                if found
                else "Not found in discovered UI; verify role access and inventory"
            )
            assert found, st.actual

    for p in pages:
        with REC.step(
            T, f"Inventory entry: {p.get('url', '?')}", role,
            "Page reachable with HTTP < 400, content, not login",
            None, None, "Medium"
        ) as st:
            st.actual = (
                f"status={p.get('status')} title='{p.get('title', '')}' "
                f"forms={p.get('forms')} tables={p.get('tables')} "
                f"search={p.get('search')} tabs={len(p.get('tabs', []))} "
                f"buttons={len(p.get('buttons', []))}"
            )
            assert not p.get("error"), p.get("error")
            assert (p.get("status") or 0) < 400, f"HTTP {p.get('status')}"
            assert not p.get("login_redirect"), "Redirected to login"
            assert p.get("textLen", 0) > 0, "Empty page"
