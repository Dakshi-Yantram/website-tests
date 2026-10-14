"""Phase 7: role-based access & unauthenticated access."""
import itertools
import pytest

from config import APPS, ROLES
from conftest import APP_NAMES
from pages.app_page import AppPage, path_of
from utils.monitor import PageMonitor
from utils.recorder import REC, Blocked

T = "06_permissions"


def _accessible(app: AppPage, status, requested, final) -> bool:
    txt = app.body_text()
    return ((status or 0) < 400 and not app.is_login()
            and path_of(final) == path_of(requested) and len(txt.strip()) > 50)


@pytest.mark.parametrize("app_key", APP_NAMES)
def test_unauthenticated_access(app_key, new_context, inventories, sessions):
    roles = [r for r, v in ROLES.items() if v.app == app_key]
    urls = {}
    for r in roles:
        if sessions(r).blocked:
            continue
        for p in inventories(r):
            if not p.get("error"):
                urls[path_of(p["url"])] = p["url"]
    if not urls:
        REC.add(T, f"{app_key}: anonymous access to protected pages", "anonymous", "Protected pages redirect to login",
                "BLOCKED", "No authenticated inventory available (no role could log in)")
        pytest.skip("no inventory")
    page = new_context().new_page()
    mon = PageMonitor(page)
    app = AppPage(page)
    for path, url in list(urls.items())[:40]:
        with REC.step(T, f"{app_key}: anonymous cannot open {path}", "anonymous",
                      "Redirect to login or HTTP 401/403/404; no private content", page, mon, "High") as st:
            status, final = app.goto(url)
            acc = _accessible(app, status, url, final)
            st.actual = f"HTTP {status}, final={final}, login_form={app.is_login()}"
            assert not acc, "Protected page is readable WITHOUT logging in"
    page.context.close()


@pytest.mark.parametrize("app_key", APP_NAMES)
def test_cross_role_access(app_key, sessions, inventories):
    roles = [r for r, v in ROLES.items() if v.app == app_key]
    for a, b in itertools.permutations(roles, 2):
        sa, sb = sessions(a), sessions(b)
        if sa.blocked or sb.blocked:
            REC.add(T, f"{b} opening {a}-only pages", b, "Restricted pages denied", "BLOCKED",
                    f"{a if sa.blocked else b}: {(sa.blocked or sb.blocked)}")
            continue
        paths_b = {path_of(p["url"]) for p in inventories(b) if not p.get("error")}
        only_a = [p for p in inventories(a) if not p.get("error") and path_of(p["url"]) not in paths_b]
        if not only_a:
            REC.add(T, f"{b} vs {a}-only pages", b, "n/a", "NOT TESTED", f"No pages seen exclusively by {a}")
            continue
        app = AppPage(sb.page)
        for p in only_a[:25]:
            with REC.step(T, f"{b} cannot open {a}-only page {path_of(p['url'])}", b,
                          "Access denied / redirected (page is absent from this role's menu)", sb.page, sb.mon, "Medium", shot=True) as st:
                status, final = app.goto(p["url"])
                acc = _accessible(app, status, p["url"], final)
                st.actual = (f"HTTP {status}, final={final}. If this page is meant to be shared, ignore - "
                             "the rule used is 'pages missing from a role's menu must be denied'.")
                assert not acc, f"{b} can open a page only {a} sees in its menu - possible broken access control (verify manually)"
