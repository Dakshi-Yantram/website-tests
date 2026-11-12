"""READ-ONLY helper: find out where an app's login form really is. It never guesses routes and never types anything.

Usage (from the project folder, with the virtual environment active):
    python -m tools.find_login                       # look at the hospital start page and list what it offers
    python -m tools.find_login --click "Staff Login" # click ONE visible link/button you chose, then check for a password field
    python -m tools.find_login --app nurseconnect    # same tool for the other app
    python -m tools.find_login --probe-https         # also check whether the https:// version of the site answers

Output: a table on screen + artifacts/inventory/login_probe_<app>.json + a screenshot.
"""
import argparse
import json
import re
import ssl
import sys
import urllib.request
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from config import APPS, DANGEROUS, INV, SHOTS, NAV_TIMEOUT
from pages.login_page import PASS_SEL
from qa_agent.artifacts import install_screenshot_guard

install_screenshot_guard()      # screenshots honour SCREENSHOT_MODE (masked by default)

LOGINISH = re.compile(r"log\s?-?in|sign\s?-?in|staff|doctor|reception|portal|continue|get started|access|authenticate", re.I)

LIST_JS = r"""() => {
  const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const txt = e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\s+/g,' ').slice(0,80);
  const q = s => [...document.querySelectorAll(s)].filter(vis);
  return {
    title: document.title, textLen: (document.body.innerText||'').length,
    excerpt: (document.body.innerText||'').trim().replace(/\s+/g,' ').slice(0,500),
    links: q('a[href]').map(a => ({text: txt(a), href: a.getAttribute('href'), abs: a.href})),
    buttons: q('button,[role=button],input[type=submit]').map(b => ({text: txt(b)})).filter(b => b.text),
    inputs: q('input,textarea,select').map(i => ({type: i.type || i.tagName.toLowerCase(), name: i.name || i.id || '', placeholder: i.placeholder || ''})),
    iframes: q('iframe').length
  };
}"""


def route_of(url: str) -> str:
    u = urlparse(url)
    return (u.path or "/") + (f"#{u.fragment}" if u.fragment else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="hospital", choices=list(APPS))
    ap.add_argument("--click", help="exact visible text of ONE link/button to click (must not be a destructive word)")
    ap.add_argument("--probe-https", action="store_true")
    a = ap.parse_args()
    app = APPS[a.app]
    out = {"app": a.app, "start_url": app.base}

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        ctx = b.new_context(viewport={"width": 1440, "height": 900})
        ctx.set_default_timeout(NAV_TIMEOUT)
        page = ctx.new_page()
        page.goto(app.base, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        info = page.evaluate(LIST_JS)
        out.update(final_url=page.url, password_fields=page.locator(PASS_SEL).count(), **info)
        page.screenshot(path=str(SHOTS / f"login_probe_{a.app}_start.png"))

        print(f"\nStart page: {page.url}\nTitle: {info['title']!r}   visible characters: {info['textLen']}")
        print(f"Password fields visible: {out['password_fields']}   iframes: {info['iframes']}")
        print(f"First words on the page: {info['excerpt'][:300]!r}\n")
        print("Links that look like a way in:")
        found = False
        for l in info["links"]:
            if LOGINISH.search(l["text"] or "") or LOGINISH.search(l["href"] or ""):
                print(f"   text={l['text']!r:30} href={l['href']!r}")
                found = True
        for bt in info["buttons"]:
            if LOGINISH.search(bt["text"]):
                print(f"   BUTTON text={bt['text']!r}")
                found = True
        if not found:
            print("   (none found by keyword - see ALL links below)")
        print(f"\nAll visible links ({len(info['links'])}):")
        for l in info["links"][:40]:
            print(f"   {l['text'][:35]!r:38} {l['href']}")
        print(f"\nVisible buttons: {[x['text'] for x in info['buttons']][:25]}")
        print(f"Visible inputs : {info['inputs'][:10]}")

        if a.click:
            if DANGEROUS.search(a.click):
                sys.exit(f"Refusing to click {a.click!r}: it matches the destructive-action list.")
            target = page.get_by_text(a.click, exact=True).first
            if not target.count():
                sys.exit(f"No visible element with exact text {a.click!r}. Check the list above.")
            target.click()
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            pw = page.locator(PASS_SEL).first.is_visible() if page.locator(PASS_SEL).count() else False
            out.update(clicked=a.click, after_click_url=page.url, password_field_after_click=pw)
            page.screenshot(path=str(SHOTS / f"login_probe_{a.app}_after_click.png"))
            print(f"\nAfter clicking {a.click!r}: now at {page.url}   password field visible: {pw}")
            if pw:
                r = route_of(page.url)
                out["suggested_login_path"] = r
                print(f"\n>>> Put this in your .env file:   {a.app.upper()}_LOGIN_PATH={r}")
                print("    (a modal on the same URL means the path stays '/': then the test must click the same link first - tell me.)")
            else:
                print("No password field yet - it may be a second step (e.g. choose a role). Share the screenshot + this output.")
        b.close()

    if a.probe_https:
        host = urlparse(app.base).netloc
        url = f"https://{host}/"
        try:
            urllib.request.urlopen(url, timeout=15, context=ssl.create_default_context())
            out["https_probe"] = "answers with a valid certificate"
        except Exception as e:
            out["https_probe"] = f"{type(e).__name__}: {str(e)[:150]}"
        print(f"\nHTTPS probe for {url}: {out['https_probe']}")

    path = INV / f"login_probe_{a.app}.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved: {path}\nScreenshots are in: {SHOTS}")


if __name__ == "__main__":
    main()
