"""CI storage budget: bulky screenshots are short-lived and separate; reports and logs stay available."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _uploads(job):
    wf = yaml.safe_load((ROOT / ".github/workflows/website-tests.yml").read_text())
    return {s["with"]["name"]: s["with"] for s in wf["jobs"][job]["steps"] if s.get("uses", "").startswith("actions/upload-artifact")}


def _lines(w):
    return [x.strip() for x in str(w["path"]).splitlines() if x.strip()]


def test_legacy_jobs_split_screenshots_from_reports():
    for app in ("nurseconnect", "hospital"):
        up = _uploads(app)
        rep, shots = up[f"{app}-report"], up[f"{app}-screenshots"]
        assert "artifacts/" in _lines(rep) and "!artifacts/screenshots/" in _lines(rep)     # nothing else is dropped
        assert _lines(shots) == ["artifacts/screenshots/"]
        assert shots["retention-days"] == 1 and rep["retention-days"] >= 3
        assert shots["if-no-files-found"] == "ignore"


def test_agent_tests_evidence_is_short_lived_but_reports_are_kept_longer():
    up = _uploads("agent-tests")
    ev = [v for k, v in up.items() if k.startswith("agent-evidence-")][0]
    rep = [v for k, v in up.items() if k.startswith("agent-report-")][0]
    assert ev["retention-days"] == 1 and rep["retention-days"] >= 7
    for ext in ("html", "json", "md"):
        assert f"artifacts/reports/*.{ext}" in _lines(rep) and f"!artifacts/reports/*.{ext}" in _lines(ev)


def test_every_upload_step_is_non_blocking_and_compressed():
    wf = yaml.safe_load((ROOT / ".github/workflows/website-tests.yml").read_text())
    for job in ("agent-tests", "nurseconnect", "hospital"):
        for s in wf["jobs"][job]["steps"]:
            if s.get("uses", "").startswith("actions/upload-artifact"):
                assert s.get("continue-on-error") is True, f"{job}: {s['with']['name']} must not fail the job on a quota error"
                assert s["with"].get("compression-level") == 9
