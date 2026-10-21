"""Load workflow files, hash them canonically, and expand synthetic-data templates."""
import copy
import hashlib
import json
from pathlib import Path

import yaml

from .errors import SchemaError, WorkflowFileError
from .schema import TEMPLATE_RX, require_valid

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKFLOWS = ROOT / "workflows.yml"
APPROVED_DIR = ROOT / "workflows_approved"
DRAFTS_DIR = ROOT / "workflow_drafts"
# Fields that do not change what a workflow DOES; excluded from the approval hash.
HASH_IGNORED = {"status", "approval", "generated"}


def load_yaml_file(path) -> dict:
    p = Path(path)
    if not p.is_file():
        raise WorkflowFileError(f"Workflow file not found: {p}")
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise WorkflowFileError(f"Cannot read {p}: {e.strerror or e}")
    if len(text) > 1_000_000:
        raise WorkflowFileError(f"{p} is larger than 1 MB; refusing to parse")
    try:
        doc = yaml.safe_load(text)                    # safe_load: no Python object construction, ever
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise WorkflowFileError(f"{p} is not valid YAML{where}: {getattr(e, 'problem', None) or 'syntax error'}")
    if doc is None:
        raise WorkflowFileError(f"{p} is empty")
    return doc


def load_workflows(path=DEFAULT_WORKFLOWS, *, draft_rules: bool = False) -> list:
    """Parse + validate one file; return a list of workflow dicts (each tagged with `_file`)."""
    doc = load_yaml_file(path)
    try:
        require_valid(doc, draft_rules=draft_rules, source=str(path))
    except SchemaError as e:
        raise SchemaError([p for p in e.problems])
    out = []
    for w in doc["workflows"]:
        w = copy.deepcopy(w)
        w["_file"] = str(path)
        out.append(w)
    return out


def load_all(paths=None, approved_dir=APPROVED_DIR) -> list:
    """workflows.yml plus every workflows_approved/*.yml. Duplicate ids across files are an error."""
    files = [Path(p) for p in (paths or [DEFAULT_WORKFLOWS])]
    if paths is None and Path(approved_dir).is_dir():
        files += sorted(Path(approved_dir).glob("*.yml"))
    wfs, seen = [], {}
    for f in files:
        for w in load_workflows(f):
            if w["id"] in seen:
                raise SchemaError([f"duplicate workflow id {w['id']!r} in {f} and {seen[w['id']]}"])
            seen[w["id"]] = str(f)
            wfs.append(w)
    return wfs


def canonical(wf: dict) -> str:
    data = {k: v for k, v in wf.items() if k not in HASH_IGNORED and not k.startswith("_")}
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def content_hash(wf: dict) -> str:
    """sha256 over the executable content. Any material change (steps, targets, writes, risk, app...) changes it."""
    return hashlib.sha256(canonical(wf).encode("utf-8")).hexdigest()


def synth_values(run_id: str, wf_id: str) -> dict:
    """Synthetic-only substitution values, unique per run AND per workflow (isolation between parallel runs)."""
    h = int(hashlib.sha256(f"{run_id}:{wf_id}".encode()).hexdigest(), 16)
    return {
        "RUN_ID": run_id,
        "WF_ID": wf_id,
        "MARKER": f"AUTOTEST_{run_id}",
        "SYNTH_PHONE": "9" + str(h % 10**9).zfill(9),
        "SYNTH_EMAIL": f"autotest+{run_id}-{h % 10**6}@example.invalid",
        "SYNTH_NAME": f"AUTOTEST {run_id}",
    }


def expand(text: str, values: dict) -> str:
    return TEMPLATE_RX.sub(lambda m: values[m.group(1)], text)
