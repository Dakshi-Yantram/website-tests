"""Explicit, auditable approval of workflows.

* A workflow may run only if (a) its file says status: approved AND (b) approvals/approvals.json holds a record
  whose sha256 equals the workflow's current executable content. Change a step => hash differs => blocked
  until a human approves again.
* Every approve / revoke is appended to approvals/audit.jsonl as a hash-chained line (tamper-evident).
* Approval is a human act: it is refused when CI=true unless QA_ALLOW_APPROVE_IN_CI=1 is set deliberately.
"""
import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .errors import ApprovalError, SchemaError, WorkflowFileError
from .redaction import find_personal, find_secrets
from .schema import SCHEMA_VERSION, validate_document
from .workflows import APPROVED_DIR, ROOT, content_hash, load_yaml_file

APPROVALS_DIR = ROOT / "approvals"


class ApprovalStore:
    def __init__(self, directory=APPROVALS_DIR):
        self.dir = Path(directory)
        self.file = self.dir / "approvals.json"
        self.audit = self.dir / "audit.jsonl"

    # ---- reading -------------------------------------------------------------------------------
    def records(self) -> dict:
        if not self.file.is_file():
            return {}
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            recs = data["approvals"]
            if not isinstance(recs, dict):
                raise TypeError
            return recs
        except Exception:
            # A corrupt approvals file must never be treated as "everything approved".
            raise ApprovalError(f"{self.file} is unreadable or malformed; nothing can run until it is repaired")

    def audited_hashes(self) -> dict:
        """id -> hash as proven by the audit log (approve/re-approve set it, revoke clears it)."""
        out = {}
        if self.audit.is_file():
            for line in self.audit.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") in ("approve", "re-approve"):
                    out[ev.get("id")] = ev.get("hash")
                elif ev.get("event") == "revoke":
                    out.pop(ev.get("id"), None)
        return out

    def state(self, wf: dict) -> tuple:
        """-> (state, detail). state in: approved | draft | unapproved | changed"""
        if wf.get("status") != "approved":
            return "draft", "status is not 'approved'"
        rec = self.records().get(wf["id"])
        if not rec:
            return "unapproved", "no approval record - run: python agent.py approve --id " + wf["id"]
        if rec.get("hash") != content_hash(wf):
            return "changed", "workflow changed since approval (approved hash differs) - re-approval required"
        # An approval only counts if the tamper-evident audit log backs it (a hand-edited approvals.json is not an approval).
        if self.verify_audit():
            return "unapproved", "approvals/audit.jsonl is broken or was edited - approvals cannot be trusted until it is repaired"
        if self.audited_hashes().get(wf["id"]) != rec.get("hash"):
            return "unapproved", "approval record is not backed by the audit log (edited by hand?) - approve it again with agent.py approve"
        return "approved", f"approved by {rec.get('approved_by', '?')} at {rec.get('approved_at', '?')}"

    def require_approved(self, wf: dict) -> None:
        state, detail = self.state(wf)
        if state != "approved":
            raise ApprovalError(f"Workflow {wf['id']!r} cannot run: {detail}")

    # ---- writing -------------------------------------------------------------------------------
    def _save(self, recs: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "approvals": recs}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.file)

    def _audit(self, event: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        prev = "0" * 64
        if self.audit.is_file():
            lines = [ln for ln in self.audit.read_text(encoding="utf-8").splitlines() if ln.strip()]
            if lines:
                import hashlib
                prev = hashlib.sha256(lines[-1].encode("utf-8")).hexdigest()
        event = dict(event, prev=prev, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        with self.audit.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, sort_keys=True) + "\n")

    def verify_audit(self) -> list:
        """Return a list of problems with the audit chain (empty = intact)."""
        import hashlib
        if not self.audit.is_file():
            return []
        problems, prev = [], "0" * 64
        for n, line in enumerate(self.audit.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                problems.append(f"audit line {n}: not JSON")
                break
            if ev.get("prev") != prev:
                problems.append(f"audit line {n}: chain broken (log edited or lines removed)")
            prev = hashlib.sha256(line.encode("utf-8")).hexdigest()
        return problems

    def approve(self, wf: dict, *, by: str, reason: str, confirm_writes: bool = False) -> dict:
        if not by or not by.strip():
            raise ApprovalError("--by (approver name) is required")
        if not reason or len(reason.strip()) < 5:
            raise ApprovalError("--reason (why this is safe to run) is required")
        if os.getenv("CI", "").lower() == "true" and os.getenv("QA_ALLOW_APPROVE_IN_CI") != "1":
            raise ApprovalError("Approval is a human decision and is refused in CI")
        if wf.get("writes") and not confirm_writes:
            raise ApprovalError(f"{wf['id']!r} writes test records; re-run with --i-understand-this-writes to approve it")
        recs = self.records()
        rec = {"hash": content_hash(wf), "approved_by": by.strip(), "reason": reason.strip(),
               "approved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "app": wf["app"], "writes": bool(wf["writes"]), "risk": wf["risk"]}
        replaced = wf["id"] in recs
        recs[wf["id"]] = rec
        self._save(recs)
        self._audit({"event": "re-approve" if replaced else "approve", "id": wf["id"], "hash": rec["hash"],
                     "by": rec["approved_by"], "reason": rec["reason"]})
        return rec

    def revoke(self, wf_id: str, *, by: str, reason: str) -> None:
        recs = self.records()
        if wf_id not in recs:
            raise ApprovalError(f"No approval record for {wf_id!r}")
        del recs[wf_id]
        self._save(recs)
        self._audit({"event": "revoke", "id": wf_id, "by": by, "reason": reason})


def promote_draft(draft_path, *, store: ApprovalStore, by: str, reason: str, confirm_writes: bool = False,
                  approved_dir=APPROVED_DIR) -> list:
    """Approve every workflow in a draft file: validate -> scan -> write to workflows_approved/ -> record approval.

    The draft file itself is left untouched (history). Returns the approved workflow ids.
    """
    from .schema import validate_document as _vd
    doc = load_yaml_file(draft_path)
    wfs = doc.get("workflows") if isinstance(doc, dict) else None
    is_generated = isinstance(wfs, list) and bool(wfs) and all(isinstance(w, dict) and w.get("generated") for w in wfs)
    probs = _vd(doc, draft_rules=is_generated, source=str(draft_path))
    if probs:
        raise SchemaError(probs)
    approved_dir = Path(approved_dir)
    approved_dir.mkdir(parents=True, exist_ok=True)
    ids = []
    for w in doc["workflows"]:
        w = copy.deepcopy(w)
        if w["status"] != "draft":
            raise ApprovalError(f"{w['id']!r} in {draft_path} is not a draft (status={w['status']!r})")
        dump = yaml.safe_dump(w)
        if find_secrets(dump) or find_personal(dump):
            raise ApprovalError(f"{w['id']!r} contains secret/personal-looking content; remove it before approval")
        w["status"] = "approved"
        out = approved_dir / f"{w['id']}.yml"
        if out.exists():
            raise ApprovalError(f"{out} already exists; revoke and remove it first (approvals never silently overwrite)")
        out.write_text(yaml.safe_dump({"version": SCHEMA_VERSION, "workflows": [w]}, sort_keys=False), encoding="utf-8")
        store.approve(w, by=by, reason=reason, confirm_writes=confirm_writes)
        ids.append(w["id"])
    return ids
