"""Crawls the logged-in UI (read-only) and builds a page inventory per role."""
import json
from collections import deque
from urllib.parse import urlparse

from config import DANGEROUS, INV, MAX_PAGES
from pages.app_page import AppPage, norm

SKIP_EXT = (".pdf", ".png", ".jpg", ".jpeg", ".svg", ".zip", ".csv", ".xlsx", ".doc", ".docx", ".mp4")


def _ok_link(href, text, origin):
    if not href or href.startswith(("javascript:", "mailto:", "tel:")):
        return False
    u = urlparse(href)
    if u.netloc != origin or u.path.lower().endswith(SKIP_EXT):
        return False
    return not (DANGEROUS.search(text or "") or DANGEROUS.search(href))


def click_discover(app: AppPage, start_url: str, limit=30):
    """SPA menus that are buttons (no href): click each once and record where we land."""
    found = []
    app.goto(start_url)
    items = [i for i in app.inspect()["sidebar"] if not i["href"] and i["text"] and not DANGEROUS.search(i["text"])]
    for it in items[:limit]:
        try:
            app.goto(start_url)
            app.page.get_by_text(it["text"], exact=True).first.click(timeout=3000)
            app.settle()
            if norm(app.page.url) != norm(start_url):
                found.append(app.page.url)
        except Exception:
            continue
    return found


def crawl(page, role_name: str, start_url: str, max_pages=MAX_PAGES):
    app = AppPage(page)
    origin = urlparse(start_url).netloc
    seen, queue = {}, deque([start_url])
    for u in click_discover(app, start_url):
        queue.append(u)
    while queue and len(seen) < max_pages:
        url = queue.popleft()
        if norm(url) in seen:
            continue
        try:
            status, final = app.goto(url)
            info = app.inspect()
            info.update(url=final, requested=url, status=status, login_redirect=app.is_login())
        except Exception as e:
            seen[norm(url)] = {"url": url, "requested": url, "error": str(e)[:200], "status": None}
            continue
        seen[norm(final)] = seen[norm(url)] = info
        for l in info["sidebar"] + info["links"]:
            if _ok_link(l["href"], l["text"], origin) and norm(l["href"]) not in seen:
                queue.append(l["href"])
    pages = list({id(v): v for v in seen.values()}.values())
    (INV / f"inventory_{role_name}.json").write_text(json.dumps(pages, indent=2, ensure_ascii=False), encoding="utf-8")
    return pages
