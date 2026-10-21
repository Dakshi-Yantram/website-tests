"""Fixtures for the agent's own tests: a local HTTP server with synthetic pages, temp approval stores, temp env files.
No network, no real credentials, no production data."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGES = {
    "/": "<html><title>Home</title><body><h1>Synthetic home</h1><a href='/login'>Login</a> <a href='/form'>Form</a></body></html>",
    "/login": """<html><title>Login</title><body><h1>Sign in</h1>
      <form onsubmit="event.preventDefault(); fetch('/api/login',{method:'POST',body:'x'}).then(()=>{location.href='/dashboard'})">
      <label>Email <input name="email" type="email"></label>
      <label>Password <input name="password" type="password"></label>
      <button type="submit">Log in</button></form></body></html>""",
    "/dashboard": "<html><title>Dash</title><body><h1>Dashboard</h1><nav><a href='/visits'>Visits</a></nav><p>Welcome</p></body></html>",
    "/visits": "<html><title>Visits</title><body><h1>Visits</h1><p>None yet</p></body></html>",
    # a button that POSTs via fetch even when the fields are empty (so the network guard, not HTML validation, is what stops it)
    "/form": """<html><title>Form</title><body><h1>Patient form</h1>
      <label>First Name <input name="first"></label>
      <label>Mobile Number <input name="mobile"></label>
      <button id="save" onclick="fetch('/api/save',{method:'POST',body:JSON.stringify({a:1})}).then(()=>{document.getElementById('msg').innerText='saved'}).catch(()=>{document.getElementById('msg').innerText='This field is required'})">Save Patient</button>
      <button onclick="fetch('/api/delete',{method:'POST'})">Delete record</button>
      <p id="msg"></p></body></html>""",
    "/slow": "<html><title>Slow</title><body><h1>ok</h1></body></html>",
}


class _Handler(BaseHTTPRequestHandler):
    log: list = []

    def _send(self, code, body, ctype="text/html"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        type(self).log.append(("GET", self.path))
        path = self.path.split("?")[0]
        if path == "/__env":
            return self._send(200, json.dumps({"environment": "staging-local"}), "application/json")
        if path == "/boom":
            return self._send(500, "server error")
        if path in PAGES:
            return self._send(200, PAGES[path])
        self._send(404, "<html><body>not found</body></html>")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        type(self).log.append(("POST", self.path))
        self._send(200, "{}", "application/json")

    def log_message(self, *a):
        pass


@pytest.fixture(scope="session")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def requests_log(server):
    _Handler.log.clear()
    return _Handler.log


def posts(log):
    return [e for e in log if e[0] != "GET"]


def make_env_file(tmp_path, base_url, extra_prod="http://production.invalid:8080", verify=None, kind="local", name="localtest", auth_origins=None):
    env = {"kind": kind, "allowed_origins": [base_url], "apps": {"nurseconnect": {"base_url": base_url}, "hospital": {"base_url": base_url}}}
    if verify:
        env["verify"] = verify
    if auth_origins:
        env["auth_origins"] = auth_origins
    doc = {"version": 1, "production_origins": [],
           "environments": {"production": {"kind": "production", "apps": {"nurseconnect": {"base_url": extra_prod}, "hospital": {"base_url": extra_prod}}},
                            name: env}}
    p = tmp_path / "environments.yml"
    p.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return p


def wf(wid="demo_workflow", app="nurseconnect", steps=None, writes=False, **kw):
    d = {"id": wid, "app": app, "title": "Demo", "description": "Synthetic demo workflow", "status": "approved", "risk": "low", "writes": writes,
         "steps": steps or [{"action": "goto", "path": "/"}]}
    d.update(kw)
    return d


def write_wf_file(tmp_path, *workflows, name="workflows.yml"):
    p = tmp_path / name
    p.write_text(yaml.safe_dump({"version": 1, "workflows": list(workflows)}, sort_keys=False), encoding="utf-8")
    return p
