"""Safe file names and masked screenshots. A screenshot cannot be scanned for secrets afterwards, so safety is by construction:
  masked (default)  inputs, textareas, selects, tables, grids, images, canvases, video and [data-private] are painted over
  off               nothing is captured
  full              unmasked - the runner only allows it on staging/local and never in CI
"""
import re
from pathlib import Path

MODES = ("off", "masked", "full")
MASK_SELECTORS = ("input", "textarea", "select", "table", "[role=grid]", "[role=table]", "[role=row]", "[role=cell]",
                  "img", "canvas", "video", "[data-private]", "[contenteditable=true]")
MASK_COLOR = "#444444"


def safe_name(s: str, limit: int = 60) -> str:
    """File-name-safe text: letters, digits, dot, dash and underscore only."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s)).strip("._") [:limit] or "file"


def safe_screenshot(page, path, mode: str = "masked", full_page: bool = False) -> str:
    """Write a screenshot and return its path, or '' when screenshots are off or the capture failed.
    An unknown mode is treated as 'masked' (fail closed)."""
    if mode == "off":
        return ""
    if mode not in MODES:
        mode = "masked"
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"path": str(path), "full_page": full_page}
        if mode == "masked":
            kwargs["mask"] = [page.locator(sel) for sel in MASK_SELECTORS]
            kwargs["mask_color"] = MASK_COLOR
        page.screenshot(**kwargs)
    except Exception:
        return ""
    return str(path) if Path(path).is_file() else ""
