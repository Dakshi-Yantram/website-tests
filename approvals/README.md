# Approval records

* `approvals.json` - one record per approved workflow: sha256 of its executable content, approver, reason, timestamp.
* `audit.jsonl`    - append-only, hash-chained log of every approve / re-approve / revoke. `python agent.py validate` verifies the chain.

Commit both files through a normal pull request so a reviewer sees every approval. The runner (and CI) refuses to run
any workflow that has no record here or whose content changed after approval. Nothing in this folder is a secret.
