"""Record exactly which source code a test run or compilation was based on (git commit SHA, branch, dirty state).

Why: when a test fails after a code change you must be able to say *which commit* was running on staging and
which checkout the agent read. Nothing here changes any repository: only read-only `git` commands are run
(see tools/git_provenance.py). Repository paths are NOT written to reports - only the logical
name (frontend, backend, ...), the SHA, the branch and whether the working tree had uncommitted changes.

Sources are named with `--source NAME=PATH` or the environment variables QA_SOURCE_<NAME>
(e.g. QA_SOURCE_FRONTEND, QA_SOURCE_BACKEND, QA_SOURCE_MOBILE).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

NAME_RX = re.compile(r"^[a-z][a-z0-9_-]{0,20}$")
ENV_PREFIX = "QA_SOURCE_"
SHA_RX = re.compile(r"^[0-9a-f]{40}$")


def _git(path: Path, *args: str) -> str:
    from tools.git_provenance import run_git      # process spawning lives outside qa_agent/ (see that module)
    return run_git(path, *args)


def _clean_remote(url: str) -> str:
    """Drop any credentials embedded in a remote URL."""
    if "://" not in url:
        return url[:120]
    u = urlsplit(url)
    host = u.hostname or ""
    return f"{u.scheme}://{host}{u.path}"[:160]


def git_info(name: str, path) -> dict:
    """Return provenance for one checkout. Never raises: problems are reported in `reason`."""
    rec = {"name": name, "available": False, "sha": "", "short": "", "branch": "", "dirty": None, "dirty_files": 0, "remote": "", "reason": ""}
    if not NAME_RX.match(name):
        rec["reason"] = f"invalid source name {name!r}"
        return rec
    p = Path(str(path)).expanduser()
    if not p.is_dir():
        rec["reason"] = "path does not exist or is not a directory"
        return rec
    try:
        sha = _git(p, "rev-parse", "HEAD")
        if not SHA_RX.match(sha):
            raise RuntimeError("unexpected rev-parse output")
        rec["sha"], rec["short"] = sha, sha[:10]
        try:
            rec["branch"] = _git(p, "rev-parse", "--abbrev-ref", "HEAD")
        except RuntimeError:
            rec["branch"] = "(unknown)"
        porcelain = _git(p, "status", "--porcelain", "--untracked-files=normal")
        files = [ln for ln in porcelain.splitlines() if ln.strip()]
        rec["dirty_files"], rec["dirty"] = len(files), bool(files)
        try:
            rec["remote"] = _clean_remote(_git(p, "config", "--get", "remote.origin.url"))
        except RuntimeError:
            rec["remote"] = ""
        rec["available"] = True
    except (RuntimeError, OSError) as e:
        rec["reason"] = f"not a readable git checkout: {str(e)[:120]}"
    return rec


def parse_source_args(items, environ=None) -> dict:
    """Merge `--source NAME=PATH` items with QA_SOURCE_<NAME> env vars (flags win). Returns {name: path}."""
    out = {}
    for k, v in (environ if environ is not None else os.environ).items():
        if k.startswith(ENV_PREFIX) and v.strip():
            out[k[len(ENV_PREFIX):].lower()] = v.strip()
    for it in items or []:
        if "=" not in it:
            raise ValueError(f"--source expects NAME=PATH, got {it!r}")
        n, v = it.split("=", 1)
        n = n.strip().lower()
        if not NAME_RX.match(n):
            raise ValueError(f"--source name {n!r} must match {NAME_RX.pattern}")
        out[n] = v.strip()
    return out


def collect(sources: dict) -> dict:
    """{name: path} -> {name: provenance record}. Empty input gives an empty dict (reports then say 'not recorded')."""
    return {n: git_info(n, p) for n, p in sorted(sources.items())}


def describe(rec: dict) -> str:
    if not rec.get("available"):
        return f"{rec['name']}: UNAVAILABLE ({rec.get('reason') or 'unknown'})"
    state = "UNCOMMITTED CHANGES (" + str(rec["dirty_files"]) + " file(s)) - SHA does not fully describe the code" if rec["dirty"] else "clean"
    return f"{rec['name']}: {rec['sha']} on {rec['branch']} [{state}]"
