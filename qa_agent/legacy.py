"""Loader for legacy_workflows.yml (the data that used to live in tests/test_07_workflows.py)."""
import re
from pathlib import Path

from .errors import SchemaError, WorkflowFileError
from .workflows import ROOT, load_yaml_file

LEGACY_FILE = ROOT / "legacy_workflows.yml"
KEYS = {"id", "role", "title", "nav", "create", "write", "open_first_row", "each_nav_is_page", "payment"}


def load_legacy_workflows(path=LEGACY_FILE) -> list:
    doc = load_yaml_file(path)
    items = doc.get("legacy_workflows") if isinstance(doc, dict) else None
    if not isinstance(items, list) or not items:
        raise SchemaError([f"{path}: 'legacy_workflows' must be a non-empty list"])
    problems, out = [], []
    for w in items:
        if not isinstance(w, dict):
            problems.append(f"{path}: every entry must be a mapping")
            continue
        wid = w.get("id", "<no id>")
        if set(w) - KEYS:
            problems.append(f"{wid}: unknown key(s) {sorted(set(w) - KEYS)}")
        for k in ("id", "role", "title"):
            if not isinstance(w.get(k), str) or not w[k]:
                problems.append(f"{wid}: {k} is required")
        if not isinstance(w.get("nav"), list) or not w["nav"] or not all(isinstance(x, str) for x in w["nav"]):
            problems.append(f"{wid}: nav must be a non-empty list of regex strings")
        for rx in list(w.get("nav") or []) + ([w["create"]] if w.get("create") else []):
            try:
                re.compile(rx)
            except (re.error, TypeError) as e:
                problems.append(f"{wid}: invalid regex {rx!r}: {e}")
        w = dict(w)
        w.setdefault("create", None)
        w.setdefault("write", False)
        out.append(w)
    if problems:
        raise SchemaError(problems)
    return out
