import yaml
from pathlib import Path

def steps(path, label):
    return [
        {"action": "goto", "path": path, "expected": f"{label} registration page loads"},
        {"action": "expect_visible", "target": {"label": "First Name"}, "expected": "First Name field visible"},
        {"action": "expect_visible", "target": {"label": "Last Name"}, "expected": "Last Name field visible"},
        {"action": "expect_visible", "target": {"label": "Mobile Number"}, "expected": "Mobile Number field visible"},
        {"action": "expect_visible", "target": {"role": "button", "name": "Save Patient"}, "expected": "Save Patient button visible"},
        {"action": "expect_no_writes_sent", "expected": "Nothing was sent"},
    ]

TARGETS = {
    "spec_rec-003": ("/hospital-dashboard/reception/addPatient/ipd", "IPD"),
    "spec_rec-004": ("/hospital-dashboard/reception/addPatient/emergency", "Emergency"),
}

for wid, (path, label) in TARGETS.items():
    f = Path("workflows_approved") / f"{wid}.yml"
    doc = yaml.safe_load(f.read_text(encoding="utf-8"))
    wf = doc["workflows"][0]
    assert wf["id"] == wid, wf["id"]
    wf["description"] = (f"Read-only smoke check: the {label} registration form renders for the reception role "
                         "(name, mobile fields and Save Patient button). It does NOT verify the business "
                         "assertions of the source spec; those need a staging write test.")
    wf["expected_outcome"] = [f"{label} registration form renders with its key fields"]
    wf["steps"] = steps(path, label)
    f.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, default_flow_style=False), encoding="utf-8")
    print("updated", f)
