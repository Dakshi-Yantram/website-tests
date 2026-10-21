"""Screenshots that are safe to keep: input values, tables and images are masked before the PNG is written.

A screenshot cannot be scanned for secrets/patient data after the fact, so safety is by construction:
  SCREENSHOT_MODE=masked (default)  inputs, textareas, selects, tables, grids, rows, images and [data-private] are masked
  SCREENSHOT_MODE=off               no screenshots at all
  SCREENSHOT_MODE=full              unmasked; ONLY honoured on local/staging environments with synthetic data
Masking is a best-effort control (custom widgets that are not in the list are not masked). Treat the artifacts
folder as sensitive anyway; CI uploads screenshots only when SCREENSHOT_MODE is masked or off.
"""
import os
import re
from pathlib import Path

MASK_SELECTORS = ("input", "textarea", "select", "table", "[role=grid]", "[role=table]", "[role=row]", "[role=cell]",
                  ".MuiDataGrid-root", ".MuiTableContainer-root", "img", "canvas", "video", "[data-private]", "[contenteditable=true]")
MODES = ("off", "masked", "full")


def screenshot_mode(env_kind: str | None = None) -> str:
    m = os.getenv("SCREENSHOT_MODE", "masked").strip().lower()
    if m not in MODES:
        return "masked"
    if m == "full" and env_kind not in ("staging", "local"):
        return "masked"            # unmasked only where data is synthetic
    return m


def safe_name(s: str, limit: int = 60) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:limit]


def safe_screenshot(page, path, mode: str = "masked", full_page: bool = False) -> str:
    """Return the written path, or '' when screenshots are off or fail."""
    if mode == "off":
        return ""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"path": str(path), "full_page": full_page}
        if mode != "full":
            kwargs["mask"] = [page.locator(sel) for sel in MASK_SELECTORS]
            kwargs["mask_color"] = "#444444"
        page.screenshot(**kwargs)
        return str(path)
    except Exception:
        return ""


def _active_env_kind():
    name = os.getenv("TEST_ENV", "").strip()
    if not name:
        return None
    try:
        from .safety import get_environment
        return get_environment(name).kind
    except Exception:
        return None


def install_screenshot_guard() -> None:
    """Make EVERY Page.screenshot() in the process (including the legacy pytest phases and tools) honour SCREENSHOT_MODE."""
    from playwright.sync_api import Page
    if getattr(Page, "_qa_screenshot_guard", False):
        return
    original = Page.screenshot

    def guarded(self, *args, **kwargs):
        mode = screenshot_mode(_active_env_kind())
        if mode == "off":
            return b""
        if mode == "masked":
            kwargs["mask"] = [self.locator(sel) for sel in MASK_SELECTORS] + list(kwargs.get("mask") or [])
            kwargs.setdefault("mask_color", "#444444")
        return original(self, *args, **kwargs)

    Page.screenshot = guarded
    Page._qa_screenshot_guard = True
