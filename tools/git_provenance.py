"""Read-only git runner for commit provenance.

Kept OUTSIDE qa_agent/ on purpose: the workflow engine in qa_agent/ must never spawn processes
(tests assert that). This helper is only called by the operator-facing CLI with fixed argument lists,
a path the operator supplied, no shell, and a short timeout. Workflow YAML can never reach it.
"""
import os
import subprocess
from pathlib import Path

TIMEOUT_S = 10
READ_ONLY = {"rev-parse", "status", "config"}


def run_git(path: Path, *args: str) -> str:
    """Run one read-only git subcommand. Raises RuntimeError with a short message on any failure."""
    if not args or args[0] not in READ_ONLY:
        raise RuntimeError(f"git subcommand {args[:1]} is not permitted")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        out = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=TIMEOUT_S, env=env, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError(f"git could not be run ({type(e).__name__})") from e
    if out.returncode != 0:
        msg = (out.stderr or out.stdout).strip().splitlines()
        raise RuntimeError(msg[0][:160] if msg else "git failed")
    return out.stdout.strip()
