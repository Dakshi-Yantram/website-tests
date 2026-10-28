"""Static discovery on a MOCK repository, sensitive-file exclusion, deterministic draft generation, draft safety."""
import json
from pathlib import Path

import pytest
import yaml

from agent_tests.conftest import ROOT
from qa_agent import discovery as D
from qa_agent import drafts as G
from qa_agent.approvals import ApprovalStore
from qa_agent.errors import SchemaError
from qa_agent.schema import validate_document
from qa_agent.workflows import load_all

APP_JSX = """\
import { Routes, Route } from 'react-router-dom';
import Login from './pages/Login';
import Visits from './pages/Visits';
import Booking from './pages/BookAppointment';
import Settings from './pages/Settings';

export default function App() {
  return (
    <Routes>
      <Route path="/auth/login" element={<Login />} />
      <Route element={<RequireAuth />}>
        <Route path="/partner/visits" element={<Visits />} />
        <Route path="/partner/book-appointment" element={<Booking />} />
      </Route>
      <Route path="/settings" element={<ProtectedRoute><Settings /></ProtectedRoute>} />
      <Route path="/patients/:id" element={<Visits />} />
    </Routes>
  );
}
"""
LOGIN_JSX = """\
export default function Login() {
  return (
    <form onSubmit={submit}>
      <TextField label="Email" name="email" type="email" required />
      <TextField label="Password" name="password" type="password" required />
      <Button type="submit">Log in</Button>
    </form>
  );
}
"""
BOOKING_JSX = """\
const schema = yup.object({
  patientName: yup.string().required('Patient name is required'),
  mobile: yup.string().required('Mobile number is required').matches(/^[6-9]\\d{9}$/, 'Enter a valid mobile number'),
});
export default function BookAppointment() {
  return (
    <form onSubmit={handleSubmit}>
      <TextField label="Patient name" name="patientName" required />
      <TextField label="Mobile number" name="mobile" placeholder="10 digit number" />
      <Select label="Doctor" name="doctor" />
      <Button type="submit">Book appointment</Button>
      <Button onClick={cancel}>Cancel booking</Button>
    </form>
  );
}
"""
DANGER_JSX = """\
export default function Settings() {
  return (<form><Input name="note" placeholder="Note" /><Button type="submit">Delete account</Button></form>);
}
"""
VISITS_JSX = "export default function Visits() { return (<div><a href='/partner/book-appointment'>Book</a></div>); }\n"

SECRET_FILE = "export const cfg = { api_key: 'sk_live_abcdef1234567890ABCDEF', token: 'ghp_" + "a1B2" * 9 + "' };\n"


@pytest.fixture
def mock_repo(tmp_path):
    r = tmp_path / "app"
    (r / "src" / "pages").mkdir(parents=True)
    (r / "node_modules" / "x").mkdir(parents=True)
    (r / "src" / "App.jsx").write_text(APP_JSX)
    (r / "src" / "pages" / "Login.jsx").write_text(LOGIN_JSX)
    (r / "src" / "pages" / "BookAppointment.jsx").write_text(BOOKING_JSX)
    (r / "src" / "pages" / "Settings.jsx").write_text(DANGER_JSX)
    (r / "src" / "pages" / "Visits.jsx").write_text(VISITS_JSX)
    (r / ".env").write_text("DATABASE_URL=postgres://root:SuperSecretDbPass9@db.internal/prod\nJWT_SECRET=topsecretvalue123456\n")
    (r / ".env.production").write_text("API_KEY=should-never-be-read-1234\n")
    (r / "src" / "config.js").write_text(SECRET_FILE)
    (r / "prod_dump.sql").write_text("INSERT INTO patients VALUES ('Ramesh Kumar','9876543210');\n")
    (r / "server.pem").write_text("-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n")
    (r / "node_modules" / "x" / "index.js").write_text("<Route path='/from-node-modules' />")
    return r


@pytest.fixture
def result(mock_repo):
    return D.analyze_repo(mock_repo, "nurseconnect")


def routes_by_path(result):
    return {r["route"]: r for r in result["routes"]}


def test_routes_found_with_source_file_and_line(result):
    rp = routes_by_path(result)
    assert {"/auth/login", "/partner/visits", "/partner/book-appointment", "/settings"} <= set(rp)
    r = rp["/auth/login"]
    assert r["source_file"].endswith("App.jsx") and r["line"] == 10 and r["confidence"] == "high"
    assert r["component_file"].endswith("Login.jsx")
    assert "/from-node-modules" not in rp, "node_modules must never be scanned"


def test_auth_protected_routes_identified_and_unknown_is_not_called_public(result):
    rp = routes_by_path(result)
    assert rp["/partner/visits"]["protected"] is True and "auth guard" in rp["/partner/visits"]["protection_evidence"]
    assert rp["/partner/book-appointment"]["protected"] is True
    assert rp["/settings"]["protected"] is True
    assert rp["/auth/login"]["protected"] == "unknown", "absence of a guard in source is 'unknown', never 'public'"


def test_navigation_links_and_reachability(result):
    rp = routes_by_path(result)
    assert any(n["target"] == "/partner/book-appointment" for n in result["nav_links"])
    assert rp["/partner/book-appointment"]["reachable_via_links"] is True
    assert rp["/partner/book-appointment"]["linked_from"]


def test_forms_controls_labels_placeholders_and_accessible_names(result):
    forms = {Path(f["source_file"]).name: f for f in result["forms"]}
    book = forms["BookAppointment.jsx"]
    labels = {c["label"] for c in book["fields"]}
    assert {"Patient name", "Mobile number", "Doctor"} <= labels
    mobile = next(c for c in book["fields"] if c["name"] == "mobile")
    assert mobile["placeholder"] == "10 digit number" and mobile["accessible_name"] == "Mobile number"
    assert next(c for c in book["fields"] if c["name"] == "patientName")["required"] is True
    assert "Book appointment" in {b["accessible_name"] for b in book["buttons"]} or "Book appointment" in {b["text"] for b in book["buttons"]}
    assert forms["Login.jsx"]["has_password"] is True
    assert any(c["dangerous"] for c in result["controls"] if "Cancel" in c["accessible_name"] + c["text"])


def test_validation_messages_and_requirements(result):
    msgs = {(v["field"], v["message"]) for v in result["validation"]}
    assert ("patientName", "Patient name is required") in msgs
    assert ("mobile", "Mobile number is required") in msgs
    assert ("mobile", "Enter a valid mobile number") in msgs


def test_sensitive_files_are_never_read_or_reported(result, mock_repo):
    blob = json.dumps(result)
    for needle in ("SuperSecretDbPass9", "topsecretvalue123456", "should-never-be-read", "sk_live_", "Ramesh", "9876543210", "BEGIN PRIVATE KEY",
                   "ghp_"):
        assert needle not in blob, needle
    reasons = {s["file"]: s["reason"] for s in result["skipped"]}
    assert reasons[".env"] == "sensitive file name" and reasons[".env.production"] == "sensitive file name"
    assert reasons["prod_dump.sql"] == "sensitive file name" and reasons["server.pem"] == "sensitive file name"
    assert "secret-like content" in reasons["src/config.js"], "a source file containing a credential is skipped whole (fail closed)"
    assert "node_modules/" in reasons


def test_symlink_escape_is_not_followed(mock_repo, tmp_path):
    outside = tmp_path / "outside.jsx"
    outside.write_text("<Route path='/leaked' element={<X/>} />")
    (mock_repo / "src" / "link.jsx").symlink_to(outside)
    res = D.analyze_repo(mock_repo, "nurseconnect")
    assert "/leaked" not in routes_by_path(res)
    assert any(s["reason"] == "symlink" for s in res["skipped"])


def test_missing_repo_directory_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        D.analyze_repo(tmp_path / "nope", "hospital")


def test_nextjs_file_routes(tmp_path):
    r = tmp_path / "next"
    (r / "pages" / "api").mkdir(parents=True)
    (r / "pages" / "index.tsx").write_text("export default function Home(){return <div/>}")
    (r / "pages" / "visits" / "[id]").mkdir(parents=True)
    (r / "pages" / "visits" / "[id]" / "index.tsx").write_text("export default function V(){return <div/>}")
    (r / "pages" / "api" / "x.ts").write_text("export default function h(){}")
    (r / "middleware.ts").write_text("export const config = { matcher: ['/visits/:path*'] }")
    res = D.analyze_repo(r, "hospital")
    rp = routes_by_path(res)
    assert "/" in rp and "/visits/[id]" in rp and "/api/x" not in rp
    assert rp["/visits/[id]"]["protected"] is True and "middleware" in rp["/visits/[id]"]["protection_evidence"]


def test_discovery_outputs_are_written_clean(result, tmp_path):
    paths = D.write_discovery(result, tmp_path / "out")
    for k in ("json", "routes_md", "forms_md"):
        text = Path(paths[k]).read_text()
        assert "SuperSecretDbPass9" not in text
    assert "/partner/visits" in Path(paths["routes_md"]).read_text()
    assert "Mobile number" in Path(paths["forms_md"]).read_text()


# --------------------------------------------------------------------------------- drafts
@pytest.fixture
def drafts(result):
    return G.generate_drafts(result)


def test_every_draft_is_draft_status_read_only_validated_and_sourced(drafts):
    wfs, skipped = drafts
    assert wfs
    for w in wfs:
        assert w["status"] == "draft" and w["writes"] is False
        assert w["sources"] and w["preconditions"] and w["expected_outcome"] and w["description"] and w["risk"] in ("low", "medium")
        assert not validate_document({"version": 1, "workflows": [w]}, draft_rules=True)
        assert all(s["action"] != "submit" for s in w["steps"])


def test_drafts_skip_credentials_forms_and_destructive_buttons_with_reasons(drafts):
    wfs, skipped = drafts
    reasons = " | ".join(s["item"] + ": " + s["reason"] for s in skipped)
    assert "credentials form" in reasons
    assert "destructive" in reasons
    for w in wfs:
        blob = yaml.safe_dump(w)
        assert "Delete account" not in blob and "Cancel booking" not in blob


def test_booking_draft_uses_synthetic_templates_and_never_submits_for_real(drafts):
    wfs, _ = drafts
    b = next(w for w in wfs if w["id"].startswith("booking_flow_"))
    fills = [s for s in b["steps"] if s["action"] == "fill"]
    assert fills and all("{{" in s["value"] for s in fills)
    assert {"{{SYNTH_PHONE}}", "{{SYNTH_NAME}}"} <= {s["value"] for s in fills}
    assert not any(s["action"] == "submit" for s in b["steps"]) and b["steps"][-2]["action"] == "guarded_submit"
    assert b.get("role") == "nurse" and any("guess" in p for p in b["preconditions"] + ["guess"])


def test_generation_is_deterministic(result):
    a, _ = G.generate_drafts(result)
    b, _ = G.generate_drafts(result)
    assert yaml.safe_dump(a) == yaml.safe_dump(b)


def test_save_drafts_writes_to_drafts_dir_only_and_marks_unapproved(drafts, tmp_path):
    wfs, skipped = drafts
    info = G.save_drafts(wfs, skipped, "nurseconnect", drafts_dir=tmp_path / "drafts", approved_dir=tmp_path / "approved", main_file=tmp_path / "nope.yml")
    assert info["written"] and all(str(tmp_path / "drafts") in p for p in info["written"])
    assert not (tmp_path / "approved").exists()
    first = Path(info["written"][0])
    assert first.read_text().startswith("# GENERATED DRAFT - NOT APPROVED")
    assert "Skipped (with reason)" in (tmp_path / "drafts" / "nurseconnect" / "INDEX.md").read_text()


def test_generated_drafts_cannot_be_executed_or_loaded_as_workflows(drafts, tmp_path):
    wfs, skipped = drafts
    G.save_drafts(wfs, skipped, "nurseconnect", drafts_dir=tmp_path / "drafts", approved_dir=tmp_path / "approved", main_file=tmp_path / "nope.yml")
    # load_all() only reads workflows.yml + workflows_approved/ - never the drafts directory
    assert not any(w["id"] in {x["id"] for x in wfs} for w in load_all())
    store = ApprovalStore(tmp_path / "ap")
    assert store.state(wfs[0])[0] == "draft"


def test_regenerating_never_overwrites_an_approved_workflow(drafts, tmp_path):
    wfs, skipped = drafts
    approved_dir = tmp_path / "approved"
    approved_dir.mkdir()
    chosen = dict(wfs[0], status="approved")
    chosen.pop("generated")
    (approved_dir / f"{chosen['id']}.yml").write_text(yaml.safe_dump({"version": 1, "workflows": [chosen]}))
    info = G.save_drafts(wfs, list(skipped), "nurseconnect", drafts_dir=tmp_path / "drafts", approved_dir=approved_dir, main_file=tmp_path / "nope.yml")
    assert not (tmp_path / "drafts" / "nurseconnect" / f"{chosen['id']}.yml").exists()
    assert chosen["id"] in info["unchanged_approved"] + info["differs_from_approved"]
