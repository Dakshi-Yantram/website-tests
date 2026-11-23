"""AI-driven explorer against the same LOCAL synthetic app as test_explorer.py, with a SCRIPTED fake model (no network, no real LLM).

Proves the design, not the model's intelligence:
  * the model chooses actions, code detects the bugs (planted bugs are found through the model's navigation)
  * an unsafe action proposed by the model (Delete) is refused in code and never reaches the server
  * the model cannot invent severity: its own 'report' is stored as an unverified Low/Info observation
  * production / unverified environments and a missing key fail closed
  * when the model keeps failing, the deterministic crawler takes over instead of the run dying
"""
import json

import pytest

from qa_agent import llm
from qa_agent.errors import SafetyBlocked
from qa_agent.llm_explorer import AIExploreConfig, LLMExplorer
from qa_agent.redaction import clear_registered_secrets
from qa_agent.safety import Environment

from agent_tests.test_explorer import _serve, roles


def _env(base, kind="local"):
    return Environment(name="local-test", kind=kind, base_urls={"hospital": base}, allowed_origins=(base,))


def _cfg(state, tmp_path, kind="local", **kw):
    cfg = dict(apps=("hospital",), base_urls={"hospital": state.base}, roles=("reception",), roles_override=roles("reception"),
               env=_env(state.base, kind), artifacts_dir=tmp_path / "artifacts", run_id="ai1", max_pages=25, max_minutes=3,
               login_retry_wait_s=0.05, screenshots="off", anonymous_check=False, cross_role_check=False, model="qwen-test", ai_steps=40)
    cfg.update(kw)
    return AIExploreConfig(**cfg)


class ScriptedModel:
    """Behaves like a tester: tries a forbidden Delete, opens/closes a dialog, searches, visits every menu item, reports once, finishes."""
    def __init__(self):
        self.s, self.calls = {}, 0

    def __call__(self, instructions, prompt, **kw):
        self.calls += 1
        page = json.loads(prompt.split("<page>")[1].split("</page>")[0])
        els = {e["l"]: e for e in page["elements"]}
        click = lambda e: json.dumps({"thought": "test", "action": "click", "id": e["id"]})
        if page["dialog"]:
            return click(els["Close"])
        if not self.s.get("del") and "Delete record" in els:
            self.s["del"] = 1
            return click(els["Delete record"])
        if page["path"] == "/app/dashboard" and not self.s.get("dlg") and "Add Visit" in els:
            self.s["dlg"] = 1
            return click(els["Add Visit"])
        if page["path"] == "/app/lookup" and not self.s.get("q"):
            self.s["q"] = 1
            return json.dumps({"thought": "search nothing", "action": "fill", "id": els["Search records"]["id"], "value": "zzqa"})
        if page["path"] == "/app/reports" and not self.s.get("rep"):
            self.s["rep"] = 1
            return json.dumps({"thought": "odd", "action": "report", "issue": {"severity": "High", "title": "Revenue looks odd", "detail": "value is not a number"}})
        if page["nav_unvisited"]:
            return click(els[page["nav_unvisited"][0]])
        return json.dumps({"thought": "covered", "action": "finish"})


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("QA_LLM_API_KEY", "k-test")
    state, srv = _serve()
    yield state
    srv.shutdown()
    clear_registered_secrets()


@pytest.fixture(scope="module")
def ai_run(tmp_path_factory):
    import os
    os.environ["QA_LLM_API_KEY"] = "k-test"
    state, srv = _serve()
    tmp = tmp_path_factory.mktemp("ai")
    model = ScriptedModel()
    orig = llm.complete
    llm.complete = model
    try:
        result = LLMExplorer(_cfg(state, tmp)).run()
    finally:
        llm.complete = orig
        srv.shutdown()
        clear_registered_secrets()
    yield state, result, tmp, model


def titles(result):
    return [f.title for f in result.findings]


def test_model_navigation_finds_the_planted_bugs_through_code_detectors(ai_run):
    _, r, _, model = ai_run
    t = " | ".join(titles(r)).lower()
    assert model.calls >= 8
    assert "blank page: /app/blank" in t
    assert "get /api/v1/billing returned http 500" in t
    assert "nan" in t or "error text" in t                       # Reports page
    assert "lookup crashed" in t                                  # JS error caused by the model's own search ("zzqa" + Enter)
    visited = {p.path for p in r.pages}
    assert {"/app/dashboard", "/app/billing", "/app/reports", "/app/lookup", "/app/add-patient"} <= visited


def test_unsafe_action_is_refused_in_code_and_never_reaches_the_server(ai_run):
    state, r, tmp, _ = ai_run
    assert ("DELETE", "/api/v1/records/1") not in state.log
    writes = [x for x in state.log if x[0] in ("POST", "PUT", "PATCH", "DELETE") and x[1] != "/api/v1/user/emailLogin"]
    assert writes == []
    trace = json.loads(next((tmp / "artifacts" / "reports").glob("agent_trace_*reception*.json")).read_text())
    refused = [s for s in trace["steps"] if s.get("refused")]
    assert refused and "destructive" in refused[0]["refused"]


def test_model_cannot_set_its_own_severity_and_labels_itself_unverified(ai_run):
    _, r, _, _ = ai_run
    ai = [f for f in r.findings if f.category == "ai-observation"]
    assert ai and ai[0].severity in ("Low", "Info")
    assert "unverified" in ai[0].title.lower() and "NOT been confirmed" in ai[0].detail


def test_report_meta_records_model_host_and_calls(ai_run):
    _, r, _, model = ai_run
    assert r.meta["ai"]["model"] == "qwen-test" and r.meta["ai"]["model_calls"] == model.calls
    assert r.meta["ai"]["page_text_sent"] is False
    assert "k-test" not in json.dumps(r.meta)


@pytest.mark.parametrize("kind", ["production", "staging-not-declared"])
def test_non_staging_environments_are_refused(app, tmp_path, kind):
    cfg = _cfg(app, tmp_path, kind="production" if kind == "production" else "local")
    if kind != "production":
        cfg.env = None
    with pytest.raises(SafetyBlocked):
        LLMExplorer(cfg)


def test_missing_key_or_model_fails_closed(app, tmp_path, monkeypatch):
    monkeypatch.delenv("QA_LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SafetyBlocked, match="key"):
        LLMExplorer(_cfg(app, tmp_path))
    monkeypatch.setenv("QA_LLM_API_KEY", "k")
    monkeypatch.delenv("QA_LLM_MODEL", raising=False)
    with pytest.raises(SafetyBlocked, match="model"):
        LLMExplorer(_cfg(app, tmp_path, model=""))


def test_action_validator_rules(app, tmp_path):
    ex = LLMExplorer(_cfg(app, tmp_path))
    obs = {"elements": [{"id": 0, "k": "button", "l": "Delete patient"}, {"id": 1, "k": "input:text", "l": "Name"},
                        {"id": 2, "k": "link", "l": "Docs", "h": "EXTERNAL"}, {"id": 3, "k": "select", "l": "Ward", "o": ["A", "B"]},
                        {"id": 4, "k": "button", "l": "Reports"}]}
    chk = lambda **a: ex._check_action(None, a, obs)
    assert "destructive" in chk(action="click", id=0)
    assert "leaves" in chk(action="click", id=2)
    assert chk(action="click", id=4) is None
    assert chk(action="click", id=99)
    assert chk(action="fill", id=1, value="zzqa test") is None
    assert "personal" in chk(action="fill", id=1, value="jane@example.com")
    assert "personal" in chk(action="fill", id=1, value="9876543210")
    assert chk(action="fill", id=4, value="x")
    assert chk(action="select", id=3, value="C") and chk(action="select", id=3, value="A") is None
    assert chk(action="goto", path="https://evil.example/") and chk(action="goto", path="//evil.example")
    assert chk(action="goto", path="/app/logout") and chk(action="goto", path="/app/billing") is None
    assert chk(action="report", issue={"title": ""})


def test_model_outage_falls_back_to_deterministic_crawler(app, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(llm, "complete", boom)
    r = LLMExplorer(_cfg(app, tmp_path)).run()
    assert any("deterministic crawler" in n for n in r.notes)
    assert len({p.path for p in r.pages}) >= 5                    # the built-in crawler still explored the app
    assert r.meta["ai"]["model_failures"] >= 3
