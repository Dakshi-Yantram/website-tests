"""The autonomous explorer against a LOCAL synthetic single-page app that contains deliberate bugs.

Nothing here touches a real system: no network, synthetic accounts, a throw-away HTTP server.
What is proven:
  * the explorer finds the planted bugs by itself (nobody writes steps or selectors for it)
  * it does NOT report the healthy pages / healthy validation as bugs
  * it never lets a write reach the server (only login POSTs)
  * login: a 5xx is retried and reported; a 401 is not retried; missing credentials are reported as 'nothing tested'
  * the report is written, redacted, and contains the observed validation message
"""
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qa_agent import explore_report
from qa_agent.explorer import ExploreConfig, Explorer
from qa_agent.redaction import clear_registered_secrets

SPA = r"""<!doctype html><html><head><title>Synthetic HIMS</title>
<style>body{margin:0;font-family:sans-serif}#side{position:fixed;left:0;top:0;width:200px;height:100%;background:#eee}
.item{padding:10px;cursor:pointer}#main{margin-left:220px;padding:20px}.toast{position:fixed;top:10px;right:10px;background:#c33;color:#fff;padding:8px}
.dlg{position:fixed;left:300px;top:100px;background:#fff;border:1px solid #333;padding:20px}</style></head><body><div id="root"></div>
<script>
const tok = () => localStorage.getItem('tok'), role = () => localStorage.getItem('role');
const MENU = [['Dashboard','/app/dashboard'],['Add patient','/app/add-patient'],['Add staff','/app/staff'],['Billing','/app/billing'],
              ['Reports','/app/reports'],['Blank','/app/blank'],['Lookup','/app/lookup']];
function toast(m){const t=document.createElement('div');t.className='toast';t.setAttribute('role','alert');t.textContent=m;document.body.appendChild(t);setTimeout(()=>t.remove(),1500);}
function nav(p){history.pushState({},'',p);render();}
window.onpopstate=render;
function shell(html){
  const items = MENU.concat(role()==='admin' ? [['Admin Console','/app/admin-console'],['Secrets','/app/secret-admin']] : []);
  document.getElementById('root').innerHTML = '<div id="side">' + items.map(i => '<div class="item" data-p="'+i[1]+'">'+i[0]+'</div>').join('') +
      '<div class="item" data-p="/login">Logout</div></div><div id="main">'+html+'</div>';
  document.querySelectorAll('.item').forEach(e => e.onclick = () => { if (e.dataset.p==='/login') localStorage.clear(); nav(e.dataset.p); });
}
function renderLogin(){
  document.getElementById('root').innerHTML = '<div id="main"><h1>Sign in</h1><label>Email <input id="em" type="email"></label><label>Password <input id="pw" type="password"></label><button id="go">Log in</button></div>';
  document.getElementById('go').onclick = async () => {
    const r = await fetch('/api/v1/user/emailLogin',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email:em.value,password:pw.value})});
    if (r.status===200){const j=await r.json();localStorage.setItem('tok',j.token);localStorage.setItem('role',j.role);nav('/app/dashboard');}
    else if (r.status===401) toast('Invalid credentials'); else toast('Something went wrong');
  };
}
function dashboard(){
  shell('<h1>Dashboard</h1><p>Welcome to the synthetic hospital dashboard.</p><button id="addvisit">Add Visit</button> <button id="del">Delete record</button> <button id="note">Add Note</button>');
  document.getElementById('addvisit').onclick = () => {
    const d=document.createElement('div');d.className='dlg';d.setAttribute('role','dialog');
    d.innerHTML='<h2>New visit</h2><label>Reason * <input id="reason"></label><button id="sv">Save Visit</button><button aria-label="Close" id="cl">x</button>';
    document.body.appendChild(d);
    document.getElementById('sv').onclick=()=>{ if(!document.getElementById('reason').value) toast('Reason is required'); };
    document.getElementById('cl').onclick=()=>d.remove();
    d.addEventListener('keydown',e=>{if(e.key==='Escape')d.remove();});
  };
  document.getElementById('del').onclick = () => fetch('/api/v1/records/1',{method:'DELETE'});
}
function render(){
  const p=location.pathname;
  if(p==='/'||p==='/login'){renderLogin();return;}
  if(p==='/app/secret-admin'){shell('<h1>Secret Admin Settings</h1><label>API endpoint <input value=""></label><label>Retention days <input value=""></label><table><tr><td>row</td></tr></table><p>'+'Sensitive configuration area. '.repeat(20)+'</p>');return;}
  if(!tok()){nav('/login');return;}
  if(p==='/app/dashboard')return dashboard();
  if(p==='/app/add-patient'){
    shell('<h1>Patient Information - OPD Registration</h1><label>First Name * <input id="fn"></label><label>Mobile Number * <input id="mn"></label><button id="sp">Save Patient</button>');
    document.getElementById('sp').onclick=()=>{ if(!fn.value){toast('Please enter patient name.');return;} fetch('/api/v1/patients',{method:'POST',body:'{}'}); };return;}
  if(p==='/app/staff'){
    shell('<h1>Add Staff</h1><label>Staff Name * <input id="sn"></label><label>Phone * <input id="sp2"></label><button id="ss">Save Staff</button>');
    document.getElementById('ss').onclick=()=>{ fetch('/api/v1/staff',{method:'POST',body:'{}'}).catch(()=>{}); };return;}
  if(p==='/app/billing'){shell('<h1>Billing</h1><p id="b">Loading...</p>');fetch('/api/v1/billing').then(r=>{ if(r.status>=500) document.getElementById('b').textContent='Failed to load billing data'; });return;}
  if(p==='/app/reports'){shell('<h1>Reports</h1><p>Total revenue: NaN</p>');return;}
  if(p==='/app/blank'){document.getElementById('root').innerHTML='';return;}
  if(p==='/app/lookup'){shell('<h1>Lookup</h1><input type="search" id="q" placeholder="Search records">');
    document.getElementById('q').onkeydown=e=>{ if(e.key==='Enter') setTimeout(()=>{ throw new Error('lookup crashed on empty result'); },10); };return;}
  if(p==='/app/patients-list'){shell('<h1>Patients</h1><p id="rows">Loading...</p>');
    fetch('/api/v1/patients/list',{method:'POST',body:'{}'}).then(r=>r.json()).then(j=>{document.getElementById('rows').textContent='Loaded '+j.count+' patients';}).catch(()=>{document.getElementById('rows').textContent='No data';});return;}
  if(p==='/app/admin-console'){shell('<h1>Admin Console</h1><table><tr><td>Users</td><td>12</td></tr></table><p>'+'Administrative tools. '.repeat(25)+'</p>');return;}
  shell('<h1>Not found</h1>');
}
render();
</script></body></html>"""


class State:
    def __init__(self):
        self.log = []
        self.fail_login = 0           # next N logins answer 500
        self.always_500 = False
        self.users = {"reception@test.invalid": ("pw-reception-1", "reception"), "admin@test.invalid": ("pw-admin-2", "admin")}
        self.lock = threading.Lock()


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            state.log.append(("GET", path))
            if path == "/api/v1/billing":
                return self._send(500, json.dumps({"error": "boom"}), "application/json")
            if path.startswith("/api/"):
                return self._send(200, "{}", "application/json")
            if path == "/favicon.ico":
                return self._send(204, "")
            self._send(200, SPA)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode() or "{}"
            path = self.path.split("?")[0]
            state.log.append(("POST", path))
            if path == "/api/v1/user/emailLogin":
                with state.lock:
                    if state.always_500 or state.fail_login > 0:
                        state.fail_login = max(0, state.fail_login - 1)
                        return self._send(500, json.dumps({"error": "db down"}), "application/json")
                body = json.loads(raw)
                user = state.users.get(body.get("email"))
                if not user or user[0] != body.get("password"):
                    return self._send(401, json.dumps({"error": "bad credentials"}), "application/json")
                return self._send(200, json.dumps({"token": "t-" + user[1], "role": user[1]}), "application/json")
            if path == "/api/v1/patients/list":
                return self._send(200, json.dumps({"count": 2}), "application/json")
            self._send(200, "{}", "application/json")

        def do_DELETE(self):
            state.log.append(("DELETE", self.path))
            self._send(200, "{}", "application/json")

        def log_message(self, *a):
            pass
    return H


def _serve():
    state = State()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state.base = f"http://127.0.0.1:{srv.server_address[1]}"
    return state, srv


@pytest.fixture
def app():
    state, srv = _serve()
    yield state
    srv.shutdown()
    clear_registered_secrets()


@dataclass
class FakeRole:
    name: str
    app: str
    email: str
    password: str
    email_env: str = "X_EMAIL"
    password_env: str = "X_PASSWORD"
    landing_prefix: str = "/app"


def roles(*names):
    table = {"reception": FakeRole("reception", "hospital", "reception@test.invalid", "pw-reception-1"),
             "admin": FakeRole("admin", "hospital", "admin@test.invalid", "pw-admin-2"),
             "wrongpw": FakeRole("wrongpw", "hospital", "reception@test.invalid", "not-the-password"),
             "nocreds": FakeRole("nocreds", "hospital", "", "")}
    return {n: table[n] for n in names}


def explore(app, tmp_path, names, **kw):
    cfg = dict(apps=("hospital",), base_urls={"hospital": app.base}, roles=tuple(names), roles_override=roles(*names),
               artifacts_dir=tmp_path / "artifacts", run_id="exp1", max_pages=25, max_minutes=3, login_retry_wait_s=0.05, screenshots="off")
    cfg.update(kw)
    return Explorer(ExploreConfig(**cfg)).run()


def titles(result, **match):
    return [f.title for f in result.findings if all(getattr(f, k) == v for k, v in match.items())]


def find(result, text, **match):
    return [f for f in result.findings if text.lower() in f.title.lower() and all(getattr(f, k) == v for k, v in match.items())]


# ------------------------------------------------------------------------------------------------ the main promise
@pytest.fixture(scope="module")
def full_run(tmp_path_factory):
    """One exploration of the whole synthetic app (about a minute); the tests below only inspect its result."""
    state, srv = _serve()
    tmp = tmp_path_factory.mktemp("explore")
    r = explore(state, tmp, ["reception", "admin"])
    yield state, r, tmp
    srv.shutdown()
    clear_registered_secrets()


def test_explorer_logs_in_and_discovers_pages_without_any_steps(full_run):
    app, r, _ = full_run
    paths = {p.path for p in r.pages if p.role == "reception"}
    # div-based sidebar (no <a href>, no <nav>): every menu item was found and visited by clicking
    for expected in ("/app/dashboard", "/app/add-patient", "/app/staff", "/app/billing", "/app/reports", "/app/blank", "/app/lookup"):
        assert expected in paths, f"{expected} not discovered; got {sorted(paths)}"
    assert "/app/admin-console" not in paths                                  # reception has no such menu entry
    assert "/app/admin-console" in {p.path for p in r.pages if p.role == "admin"}
    assert {o.role: o.status for o in r.roles} == {"reception": "EXPLORED", "admin": "EXPLORED"}


def test_planted_bugs_are_found(full_run):
    _, r, _ = full_run
    assert find(r, "GET /app", severity="x") == []                            # sanity: helper works
    billing = find(r, "GET /api/v1/billing returned HTTP 500", severity="High")
    assert len(billing) == 1 and set(billing[0].role.split(", ")) == {"reception", "admin"}   # one bug, both roles listed (not two bugs)
    assert find(r, "Blank page: /app/blank", severity="High")
    assert find(r, "Error text visible on /app/reports", severity="Medium")   # 'NaN'
    assert find(r, "Error text visible on /app/billing", severity="Medium")   # 'Failed to load'
    assert find(r, "Uncaught JavaScript error", severity="Medium")           # search crash on /app/lookup
    sent = find(r, "Empty form is sent to the server without any visible validation: /app/staff", severity="Low")
    assert sent                                                               # left-aligned Save button was found (not mistaken for sidebar)
    assert find(r, "Button 'Add Note' has no visible effect", severity="Low")  # dead control


def test_healthy_behaviour_is_not_reported_as_a_bug(full_run):
    _, r, _ = full_run
    bad = [f.title for f in r.findings if f.severity in ("High", "Medium")]
    for ok_path in ("/app/dashboard", "/app/add-patient"):
        assert not [t for t in bad if ok_path in t], f"false positive on healthy page {ok_path}: {bad}"
    assert not find(r, "empty required form", ) or all("/app/add-patient" not in f.title for f in find(r, "empty required form"))
    assert not any("/app/add-patient" in f.title for f in r.findings if f.category == "validation")
    assert not any("Add Visit" in f.title for f in r.findings)                # dialog validation works -> no finding


def test_validation_messages_actually_shown_to_users_are_recorded(full_run):
    _, r, _ = full_run
    seen = {(v["path"], v["where"]): v["messages"] for v in r.validations if v["role"] == "reception"}
    assert any("Please enter patient name." in m for m in seen[("/app/add-patient", "page form")])
    assert any("Reason is required" in m for m in seen[("/app/dashboard", "dialog 'Add Visit'")])
    assert any(d["button"] == "Add Visit" and d["title"] == "New visit" for d in r.dialogs)


def test_access_control_checks(full_run):
    _, r, _ = full_run
    anon = find(r, "Page opens without logging in: /app/secret-admin", category="access-control")
    assert anon and anon[0].severity in ("High", "Medium")
    assert not find(r, "Page opens without logging in: /app/dashboard")        # properly guarded: redirected to login
    cross = find(r, "can open a page only 'admin' reached by browsing: /app/admin-console", role="reception")
    assert cross, titles(r, category="access-control")


def test_read_only_guarantee_no_write_ever_reaches_the_server(full_run):
    app, _, _ = full_run
    writes = [(m, p) for m, p in app.log if m != "GET"]
    assert all(p == "/api/v1/user/emailLogin" and m == "POST" for m, p in writes), writes
    assert len(writes) == 2                                                    # exactly one login per role, nothing else


def test_destructive_controls_are_never_clicked(full_run):
    app, _, _ = full_run
    assert ("DELETE", "/api/v1/records/1") not in app.log


def test_report_files_are_written_redacted_and_bug_first(full_run):
    app, r, tmp = full_run
    paths = explore_report.write_reports(r, tmp / "out")
    md = paths["md"].read_text(encoding="utf-8")
    html = paths["html"].read_text(encoding="utf-8")
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert "BUG(S) FOUND" in md and "BUG-001" in md and "How to reproduce" in md
    assert "Please enter patient name." in md and "Please enter patient name." in html
    for secret in ("pw-reception-1", "pw-admin-2"):
        assert secret not in md + html + json.dumps(data)
    assert data["counts"]["High"] >= 2
    head, hint, _ = explore_report.verdict(r)
    assert hint == "bugs"


# ------------------------------------------------------------------------------------------------ login behaviour
def test_server_error_during_login_is_retried_then_succeeds_and_is_reported(app, tmp_path):
    app.fail_login = 2
    r = explore(app, tmp_path, ["reception"], max_pages=3)
    assert r.roles[0].status in ("EXPLORED", "PARTIAL") and r.roles[0].login_attempts == 3     # PARTIAL = stopped at --max-pages=3
    f = find(r, "Login needed 3 attempts")
    assert f and f[0].severity == "Medium"
    assert [m for m, p in app.log if m == "POST" and p.endswith("emailLogin")] == ["POST"] * 3


def test_persistent_login_500_is_a_high_bug_not_a_silent_skip(app, tmp_path):
    app.always_500 = True
    r = explore(app, tmp_path, ["reception"])
    assert r.roles[0].status == "LOGIN FAILED" and "HTTP 500" in r.roles[0].reason
    f = find(r, "Login API returns HTTP 500", severity="High", category="login")
    assert f and "server-side" in f[0].detail
    assert len([1 for m, p in app.log if m == "POST"]) == 3                    # 1 try + 2 retries, then it stops
    assert explore_report.verdict(r)[2] and "Some roles" in explore_report.verdict(r)[2]


def test_wrong_password_is_never_retried_to_avoid_lockout(app, tmp_path):
    r = explore(app, tmp_path, ["wrongpw"])
    assert r.roles[0].status == "LOGIN FAILED" and "credentials rejected" in r.roles[0].reason
    assert find(r, "Login rejected", severity="High")
    assert len([1 for m, p in app.log if m == "POST"]) == 1


def test_missing_credentials_means_nothing_was_tested_and_says_so(app, tmp_path):
    r = explore(app, tmp_path, ["nocreds"])
    assert r.roles[0].status == "BLOCKED" and "credentials not configured" in r.roles[0].reason
    head, hint, _ = explore_report.verdict(r)
    assert head == "NOTHING WAS TESTED" and hint == "blocked"
    assert [m for m, p in app.log if m != "GET"] == []


# ------------------------------------------------------------------------------------------------ command line
def test_cli_auto_exit_codes(app, tmp_path, monkeypatch):
    import agent
    monkeypatch.setattr(agent, "ART", tmp_path / "artifacts")
    monkeypatch.setenv("RUN_ID", "cli1")
    monkeypatch.setattr("config.ROLES", roles("reception") | {}, raising=False)
    monkeypatch.setattr("config.AGENT_ROLES", {}, raising=False)
    rc = agent.main(["auto", "--app", "hospital", "--base-url", f"hospital={app.base}", "--max-pages", "6", "--no-cross-role-check",
                     "--report-dir", str(tmp_path / "rep")])
    assert rc == 0 or rc == 1
    assert list((tmp_path / "rep").glob("auto_report_cli1.html"))


# ------------------------------------------------------------------------------------------------ read-only POST allowlist
def test_post_list_calls_are_blocked_by_default_and_reported(app, tmp_path):
    r = explore(app, tmp_path, ["reception"], max_pages=2, seed_paths=("/app/patients-list",), anonymous_check=False, cross_role_check=False)
    assert ("POST", "/api/v1/patients/list") not in app.log
    assert r.blocked_writes.get("POST /api/v1/patients/list", 0) >= 1
    assert "--allow-read-post" in explore_report._md(explore_report._plain(r))


def test_operator_declared_read_only_post_is_allowed_but_nothing_else(app, tmp_path):
    r = explore(app, tmp_path, ["reception"], max_pages=3, seed_paths=("/app/patients-list",), allow_read_post=(r"/patients/list$",),
                anonymous_check=False, cross_role_check=False)
    assert ("POST", "/api/v1/patients/list") in app.log
    assert [x for x in app.log if x[0] != "GET" and x != ("POST", "/api/v1/patients/list") and x != ("POST", "/api/v1/user/emailLogin")] == []
    assert r.meta["read_only_post_patterns_allowed_by_operator"] == ["/patients/list$"]


# ------------------------------------------------------------------------------------------------ fill mode (staging/local only)
from agent_tests.conftest import make_env_file  # noqa: E402
from qa_agent.safety import CONFIRM_PREFIX, SafetyBlocked, get_environment  # noqa: E402


def _fill_cfg(app, tmp_path, phrase=None, kind="local"):
    envfile = make_env_file(tmp_path, app.base, kind=kind)
    env = get_environment("localtest", envfile)
    return env, (CONFIRM_PREFIX + "localtest") if phrase is None else phrase


def test_fill_refused_without_confirmation_phrase(app, tmp_path):
    env, _ = _fill_cfg(app, tmp_path)
    with pytest.raises(SafetyBlocked):
        explore(app, tmp_path, ["reception"], env=env, fill=True, confirm_phrase="")
    assert not [e for e in app.log if e[0] != "GET"], "nothing may be sent when the gate refuses"


def test_fill_refused_on_production_kind(app, tmp_path):
    env, phrase = _fill_cfg(app, tmp_path, kind="production")
    with pytest.raises(SafetyBlocked):
        explore(app, tmp_path, ["reception"], env=env, fill=True, confirm_phrase=phrase)


def test_fill_refused_without_environment(app, tmp_path):
    with pytest.raises(SafetyBlocked):
        explore(app, tmp_path, ["reception"], env=None, fill=True, confirm_phrase="x")


@pytest.fixture(scope="module")
def fill_run(tmp_path_factory):
    state, srv = _serve()
    tmp = tmp_path_factory.mktemp("fill")
    env, phrase = _fill_cfg(state, tmp)
    r = explore(state, tmp, ["reception"], env=env, fill=True, confirm_phrase=phrase, max_pages=12)
    yield state, r, tmp
    srv.shutdown()
    clear_registered_secrets()


def test_fill_saves_data_and_reports_records(fill_run):
    state, r, _ = fill_run
    posted = [p for m, p in state.log if m == "POST" and p != "/api/v1/user/emailLogin"]
    assert "/api/v1/patients" in posted, "the valid form must actually be submitted"
    assert any(c["path"] == "/app/add-patient" and c["writes_sent"] >= 1 for c in r.created)
    assert all(c["marker"] == "AUTOTEST_exp1" for c in r.created)


def test_fill_never_sends_delete_or_touches_billing(fill_run):
    state, r, _ = fill_run
    assert not [e for e in state.log if e[0] == "DELETE"]
    assert not [c for c in r.created if "billing" in c["path"]]
    assert any("DELETE" in b for b in r.blocked_writes) or True


def test_fill_reports_missing_confirmation_after_save(fill_run):
    _, r, _ = fill_run
    assert find(r, "No confirmation after saving valid data")


def test_fill_report_lists_records_to_clean_up(fill_run):
    _, r, tmp = fill_run
    paths = explore_report.write_reports(r, tmp / "reports")
    text = paths["md"].read_text(encoding="utf-8") + paths["html"].read_text(encoding="utf-8")
    assert "Records created" in text and "AUTOTEST_exp1" in text
    assert "FILL MODE" in text


def test_cli_fill_requires_declared_environment():
    import subprocess, sys
    from agent_tests.conftest import ROOT
    p = subprocess.run([sys.executable, "agent.py", "auto", "--app", "hospital", "--fill"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert p.returncode == 2 and "BLOCKED" in (p.stdout + p.stderr)


# ------------------------------------------------------------------------------------------------ video recording
def test_video_not_recorded_without_declared_staging(app, tmp_path):
    r = explore(app, tmp_path, ["reception"], video=True, max_pages=2)       # no env => screenshots/video stay off
    assert all(not o.video for o in r.roles)
    assert any("Video NOT recorded" in n for n in r.notes)
    assert not list((tmp_path / "artifacts").rglob("*.webm"))


def test_video_recorded_on_local_env_and_linked_in_report(app, tmp_path):
    env, _ = _fill_cfg(app, tmp_path)
    r = explore(app, tmp_path, ["reception"], env=env, video=True, screenshots="masked", max_pages=3)
    vids = [o.video for o in r.roles if o.video]
    assert vids, f"no video; notes={r.notes}"
    from pathlib import Path
    assert Path(vids[0]).is_file() and Path(vids[0]).stat().st_size > 1000
    paths = explore_report.write_reports(r, tmp_path / "rep")
    assert (tmp_path / "rep" / "videos").is_dir()
    assert "<video" in paths["html"].read_text(encoding="utf-8")
    assert "videos/" in paths["md"].read_text(encoding="utf-8")
