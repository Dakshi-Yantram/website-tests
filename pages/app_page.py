"""Page abstraction for any logged-in page: inspection, navigation, safe form filling."""
import re
from urllib.parse import urlparse

from config import DANGEROUS, NAV_TIMEOUT, TEST_MARKER
from pages.login_page import PASS_SEL

INSPECT_JS = r"""() => {
 const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g,' ').slice(0,80);
 const q = s => [...document.querySelectorAll(s)].filter(vis);
 return {
  title: document.title,
  headings: q('h1,h2,h3').map(txt).filter(Boolean).slice(0,15),
  links: q('a[href]').map(a => ({text: txt(a), href: a.href})).slice(0,300),
  sidebar: q('aside a, nav a, [role=navigation] a, aside button, nav button, [role=menuitem]')
            .map(a => ({text: txt(a), href: a.href || ''})).slice(0,120),
  buttons: q('button,[role=button],input[type=submit]').map(txt).filter(Boolean).slice(0,60),
  tabs: q('[role=tab]').map(txt),
  inputs: q('input,textarea,select').map(i => ({type: i.type || i.tagName.toLowerCase(), name: i.name || i.id || '',
          placeholder: i.placeholder || '', required: !!i.required})).slice(0,80),
  search: q('input[type=search],input[placeholder*=search i],input[name*=search i]').length,
  forms: q('form').length, tables: q('table').length,
  textLen: (document.body.innerText || '').length
 };
}"""


def path_of(url: str) -> str:
    u = urlparse(url)
    p = u.path.rstrip("/") or "/"
    if u.fragment.startswith("/"):
        p += "#" + u.fragment
    return p


def norm(url: str) -> str:
    u = urlparse(url)
    q = f"?{u.query}" if u.query else ""
    return f"{u.netloc}{path_of(url)}{q}"


class AppPage:
    def __init__(self, page):
        self.page = page

    # ---- navigation -------------------------------------------------
    def settle(self):
        try:
            self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        self.page.wait_for_timeout(300)

    def goto(self, url):
        resp = self.page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        self.settle()
        return (resp.status if resp else None), self.page.url

    def is_login(self) -> bool:
        try:
            return self.page.locator(PASS_SEL).first.is_visible()
        except Exception:
            return False

    def body_text(self) -> str:
        try:
            return self.page.inner_text("body")
        except Exception:
            return ""

    def inspect(self) -> dict:
        return self.page.evaluate(INSPECT_JS)

    # ---- clicking sections by visible name ---------------------------
    def open_section(self, pattern: str) -> bool:
        rx = re.compile(pattern, re.I)
        pg = self.page
        for role in ("link", "menuitem", "tab", "button"):
            loc = pg.get_by_role(role, name=rx)
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                try:
                    label = el.inner_text(timeout=1500)
                except Exception:
                    continue
                if el.is_visible() and not DANGEROUS.search(label):
                    el.click()
                    self.settle()
                    return True
        txt = pg.get_by_text(rx)
        for i in range(min(txt.count(), 4)):
            el = txt.nth(i)
            try:
                if el.is_visible() and not DANGEROUS.search(el.inner_text(timeout=1500)):
                    el.click()
                    self.settle()
                    return True
            except Exception:
                continue
        return False

    # ---- safe data entry ---------------------------------------------
    @staticmethod
    def safe_value(kind: str, hint: str) -> str:
        h = hint.lower()
        if kind == "email" or "email" in h:
            return f"autotest.{TEST_MARKER.lower()}@example.com"
        if kind == "tel" or any(k in h for k in ("phone", "mobile", "contact")):
            return "9000000000"
        if kind == "number" or "age" in h:
            return "30"
        if kind == "date":
            return "1990-01-01" if "birth" in h or "dob" in h else "2030-01-15"
        if kind == "time":
            return "10:00"
        return TEST_MARKER

    def fill_form_safely(self, container) -> int:
        """Fill visible fields in `container` (Locator) with obviously-fake data. Returns #fields filled."""
        filled = 0
        fields = container.locator("input:visible, textarea:visible, select:visible")
        for i in range(fields.count()):
            el = fields.nth(i)
            try:
                tag = el.evaluate("e => e.tagName.toLowerCase()")
                kind = (el.get_attribute("type") or tag).lower()
                if kind in ("hidden", "password", "file", "checkbox", "radio", "submit", "button", "reset"):
                    continue
                hint = " ".join(filter(None, [el.get_attribute("name"), el.get_attribute("id"),
                                              el.get_attribute("placeholder"), el.get_attribute("aria-label")]))
                if tag == "select":
                    if el.locator("option").count() > 1:
                        el.select_option(index=1)
                        filled += 1
                else:
                    el.fill(self.safe_value(kind, hint))
                    filled += 1
            except Exception:
                continue
        return filled

    def success_message_visible(self) -> bool:
        rx = re.compile(r"success|created|saved|registered|added|booked|submitted|updated", re.I)
        try:
            alerts = self.page.locator('[role="alert"], [class*="toast" i], [class*="success" i], [class*="notif" i]')
            return any(rx.search(t) for t in alerts.all_inner_texts())
        except Exception:
            return False
