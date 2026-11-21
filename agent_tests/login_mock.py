"""A LOCAL stand-in for the NurseConnect login page, mirroring the markup/behaviour of
nurseconnect-frontend/src/routes/auth.login.tsx + auth.forgot-password.tsx (labels without `for`, icon-only password toggle,
tabs, safeRedirect, portal landing). It is NOT the real site: it proves the agent's logic, not the live site's state."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LOGIN_HTML = r"""<!doctype html><html><head><title>Login - NurseConnect</title></head><body>
<div id="root"></div>
__BEACON__
<script>
const API = '';
let mode = 'signin', showPassword = false, error = null, loading = false;
const FLAGS = __FLAGS__;
const params = new URLSearchParams(location.search);
const redirect = params.get('redirect');
function isSafeInternalPath(p){ if(typeof p!=='string'||!p.length) return false; if(p[0]!=='/') return false; if(p[1]==='/'||p[1]==='\\') return false;
  for(let i=0;i<p.length;i++){const c=p.charCodeAt(i); if(c<=0x1f||c===0x7f||p[i]==='\\') return false;} return true; }
function routePortal(path){ if(path.startsWith('/consumer')) return 'consumer'; if(path.startsWith('/partner')) return 'partner'; return null; }
const HOME = {consumer:'/consumer', partner:'/partner'};
function safeRedirect(role, raw){
  if(!raw) return null;
  if(FLAGS.vulnerable_redirect && /^https?:/.test(raw)) return raw;   // deliberately broken variant
  let d = raw; for(let i=0;i<5&&/%[0-9A-Fa-f]{2}/.test(d);i++){ const n=decodeURIComponent(d); if(n===d) break; d=n; }
  if(!isSafeInternalPath(d)) return null;
  const path = d.split('?')[0]; if(path.startsWith('/auth')) return null;
  const t = routePortal(path); if(t && t !== role) return null; return d;
}
try { const u = JSON.parse(localStorage.getItem('nc.session.v1')); if (u) { location.replace(safeRedirect(u.role, redirect) || HOME[u.role]); } } catch(e) {}
const EYE = '<svg aria-hidden="true" width="16" height="16"><circle cx="8" cy="8" r="6"/></svg>';
function render(){
  const root = document.getElementById('root');
  const tabs = (mode!=='verify') ? `<div class="tabs">` + [['signin','Email'],['otp_phone','Phone OTP'],['register','Register']].map(([m,l])=>`<button type="button" data-mode="${m}">${l}</button>`).join('') + `</div>` : '';
  let body = '';
  if(mode==='signin') body = `<h2>Welcome Back</h2><p>Login to continue to your portal</p>
   <form id="f"><div><label>Email</label><input type="email" id="email" placeholder="you@example.com" autocomplete="email"></div>
   <div><label>Password</label><div class="relative"><input type="${showPassword?'text':'password'}" id="pw" placeholder="&bull;&bull;&bull;&bull;" autocomplete="current-password"><button type="button" id="toggle">${EYE}</button></div></div>
   ${error?`<div class="text-red-600">${error}</div>`:''}
   <div class="text-right"><a href="/auth/forgot-password">Forgot password?</a></div>
   <button id="submit" ${loading?'disabled':''}>${loading?'Logging in…':'Login'}</button></form>
   <div>Don't have an account? <button type="button" data-mode="register">Register</button></div>`;
  if(mode==='register') body = `<h2>Join the marketplace</h2><form id="rf"><div><label>Full name</label><input placeholder="e.g. Asha Mehra"></div>
   <div><label>Email</label><input type="email" placeholder="you@example.com" autocomplete="email"></div>
   <div><label>Mobile Number</label><span>+91</span><input placeholder="9999900001"><button type="button">Send OTP</button></div>
   <div><label>Mobile OTP</label><input placeholder="Tap 'Send OTP' first" disabled></div>
   <div><label>Password</label><input type="password" autocomplete="new-password"></div>
   <button disabled>Create account</button></form><div>Already registered? <button type="button" data-mode="signin">Sign in</button></div>`;
  if(mode==='otp_phone') body = `<h2>Login with OTP</h2><form id="of"><div><label>Mobile Number</label><span>+91</span><input placeholder="9999900001"></div>
   <button>Send OTP</button></form><div>Prefer email and password? <button type="button" data-mode="signin">Email login</button></div>`;
  root.innerHTML = tabs + body;
  root.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => { mode = b.dataset.mode; error = null; render(); });
  const t = document.getElementById('toggle'); if(t) t.onclick = () => { const v = document.getElementById('pw').value; showPassword = !showPassword; render(); document.getElementById('pw').value = v; };
  const f = document.getElementById('f'); if(f) f.onsubmit = submit;
  const of = document.getElementById('of'); if(of) of.onsubmit = e => e.preventDefault();
}
async function submit(e){
  e.preventDefault(); error = null; loading = true; const email = document.getElementById('email').value, pw = document.getElementById('pw').value; render();
  document.getElementById('email').value = email; document.getElementById('pw').value = pw;
  try {
    const res = await fetch(API + '/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email, password: pw})});
    const data = await res.json().catch(()=>({}));
    if(!res.ok){ error = res.status===401 ? (data.detail||'Login failed') : res.status===422 ? "Some of the details entered aren't valid. Please check and try again." : 'Login failed'; loading=false; render(); return; }
    localStorage.setItem('access_token', data.tokens.access_token); localStorage.setItem('refresh_token', data.tokens.refresh_token);
    const role = {consumer:'consumer', worker:'partner'}[data.user.role];
    localStorage.setItem('nc.session.v1', JSON.stringify({role}));
    location.assign(safeRedirect(role, redirect) || HOME[role]);
  } catch(err){ error = 'Network error. Please check your connection.'; loading=false; render(); }
}
render();
</script></body></html>"""

FORGOT_HTML = """<!doctype html><title>Reset password</title><body><a href="/auth/login"><svg aria-hidden="true"></svg> Back to sign in</a>
__BEACON__<h1>Reset password</h1><p>Enter your registered email or mobile number</p><label>Email or mobile number</label>
<input type="text" placeholder="you@example.com or 9876543210"><button disabled>Send code to email</button></body>"""

BEACON = "<script>fetch('/cdn-cgi/rum', {method: 'POST', body: '{}'}).catch(() => {});</script>"
SLOW_APP = """<!doctype html><title>slow</title><button id="b" type="button">Go</button><p id="out"></p>
<script async src="/slow.js"></script>"""     # the button works only after /slow.js (0.8 s) has loaded, like a hydrating SSR page
SLOW_JS = "document.getElementById('b').onclick = () => { document.getElementById('out').textContent = 'wired'; };"

PORTALS = {"/consumer": "Consumer portal", "/consumer/bookings": "Consumer bookings", "/partner": "Partner portal",
           "/partner/assignments": "Partner assignments"}


class MockSite:
    """users: {email: (password, backend_role)}. flags: vulnerable_redirect, leaky_errors."""

    def __init__(self, users=None, host="127.0.0.1", **flags):
        self.users = users or {}
        self.flags = flags
        self.requests: list = []          # (method, path)
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                data = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                p = urlsplit(self.path).path
                outer.requests.append(("GET", p))
                beacon = BEACON if outer.flags.get("rum_beacon") else ""
                if p == "/auth/login":
                    self._send(200, LOGIN_HTML.replace("__FLAGS__", json.dumps(outer.flags)).replace("__BEACON__", beacon))
                elif p == "/auth/forgot-password":
                    self._send(200, FORGOT_HTML.replace("__BEACON__", beacon))
                elif p == "/slow-app":
                    self._send(200, SLOW_APP)
                elif p == "/slow.js":
                    import time
                    time.sleep(0.8)
                    self._send(200, SLOW_JS, "application/javascript")
                elif p in PORTALS:
                    self._send(200, f"<title>{PORTALS[p]}</title><body><h1>{PORTALS[p]}</h1><p>Signed-in content for the synthetic user.</p></body>")
                else:
                    self._send(404, "not found")

            def do_POST(self):
                p = urlsplit(self.path).path
                outer.requests.append(("POST", p))
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if p != "/api/auth/login":
                    return self._send(404, "{}", "application/json")
                email, pw = body.get("email", ""), body.get("password", "")
                if not email or "@" not in email or email.endswith(".invalid") and outer.flags.get("strict_email"):
                    return self._send(422, json.dumps({"detail": [{"msg": "value is not a valid email address"}]}), "application/json")
                user = outer.users.get(email)
                if user and user[0] == pw:
                    return self._send(200, json.dumps({"tokens": {"access_token": "a" * 40, "refresh_token": "r" * 40},
                                                       "user": {"role": user[1], "email": email}}), "application/json")
                msg = "No account found for this email" if outer.flags.get("leaky_errors") and not user else "Invalid email or password"
                self._send(401, json.dumps({"detail": msg}), "application/json")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.url = f"http://{host}:{self.port}"   # use host="localhost" for a second site: safety.py compares HOSTS
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def posts(self):
        return [p for m, p in self.requests if m != "GET"]
