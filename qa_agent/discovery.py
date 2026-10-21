"""Read-only application discovery.

1. STATIC (default): scans a checked-out application repo with regular expressions. It never executes app
   code, never reads files outside an allowlist, never keeps raw source text - only structured facts
   (route, control label, line number). Files that contain secret-looking content are skipped entirely.
2. BROWSER (optional, `inspect_routes`): GET-only visits of explicitly listed routes on an explicitly given
   base URL, with every non-GET request blocked. Nothing is clicked or submitted.

All findings carry a `confidence` (high | medium | low) and the `source_file`/`line` they came from.
This is heuristic analysis: it can miss routes built dynamically and can over-report config paths.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .redaction import redact

ALLOWED_EXT = {".js", ".jsx", ".ts", ".tsx", ".vue", ".html", ".htm", ".svelte"}
SKIP_DIRS = {"node_modules", ".git", ".github", "dist", "build", ".next", ".nuxt", "out", "coverage", "vendor",
             "__pycache__", ".venv", "venv", "env", ".cache", "tmp", "temp", "logs", "uploads", "backup", "backups",
             "dump", "dumps", "secrets", "private", ".aws", ".ssh", ".idea", ".vscode", "fixtures", "mocks_data",
             "__snapshots__", "test-results", "playwright-report"}
SKIP_NAME_RX = re.compile(
    r"(?i)(^\.env|\.env$|\.pem$|\.key$|\.p12$|\.pfx$|\.crt$|\.sql$|\.dump$|\.sqlite3?$|\.db$|\.bak$|\.log$|\.map$|"
    r"\.min\.(js|css)$|^id_(rsa|dsa|ecdsa|ed25519)|credentials|secret|passwd|keystore|\.tfstate|\.tfvars|service-?account|"
    r"\.csv$|\.xlsx?$|\.zip$|\.tar|\.gz$)")
MAX_FILE_BYTES = 300_000
MAX_FILES = 5000

_HARD_SECRET = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{5,}"),
    re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^/\s:@]+:[^/\s@]+@"),
    re.compile(r"(?i)\b(?:secret|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|password|passwd|token)\w*\s*[:=]\s*[\"'`]"
               r"(?=[A-Za-z0-9/+_\-]{16,}[\"'`])(?=[A-Za-z0-9/+_\-]*\d)(?=[A-Za-z0-9/+_\-]*[A-Za-z])[A-Za-z0-9/+_\-]{16,}"),
]


def has_hard_secret(text: str) -> str:
    """Return a short category name if the text contains a real-looking credential, else ''."""
    from .redaction import known_secret_values
    for v in known_secret_values():
        if v in text:
            return "registered-secret-value"
    names = ["private-key", "github-token", "aws-key", "slack-token", "google-api-key", "jwt", "database-url", "credential-literal"]
    for n, rx in zip(names, _HARD_SECRET):
        if rx.search(text):
            return n
    return ""


# ---------------------------------------------------------------------------------------------------
# file walking
# ---------------------------------------------------------------------------------------------------
def iter_source_files(root: Path, skipped: list):
    root = root.resolve()
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for d in sorted(dirnames):
            if d in SKIP_DIRS or d.lower() in SKIP_DIRS:
                skipped.append({"file": str((Path(dirpath) / d).relative_to(root)) + "/", "reason": "excluded directory"})
            else:
                kept.append(d)
        dirnames[:] = kept
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = str(p.relative_to(root))
            if p.is_symlink():
                skipped.append({"file": rel, "reason": "symlink"})
                continue
            if SKIP_NAME_RX.search(fn):
                skipped.append({"file": rel, "reason": "sensitive file name"})
                continue
            if p.suffix.lower() not in ALLOWED_EXT:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    skipped.append({"file": rel, "reason": "file too large"})
                    continue
                real = p.resolve()
                real.relative_to(root)
            except (OSError, ValueError):
                skipped.append({"file": rel, "reason": "outside repository root"})
                continue
            count += 1
            if count > MAX_FILES:
                skipped.append({"file": rel, "reason": f"more than {MAX_FILES} files; scan truncated"})
                return
            yield p, rel


# ---------------------------------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------------------------------
PROTECT_RX = re.compile(r"(?i)\b(?:Protected\w*|Private\w*|RequireAuth\w*|AuthGuard\w*|AuthRoute\w*|withAuth\w*|RequireRole\w*|RoleRoute\w*|"
                        r"requiresAuth|requireAuth|isAuthenticated)\b")
ROUTER_CFG_RX = re.compile(r"createBrowserRouter|createHashRouter|createMemoryRouter|createRoutesFromElements|createRouter\(|new VueRouter|<Routes\b|<Switch\b|routes\s*[:=]\s*\[")
JSX_ROUTE_RX = re.compile(r"<(Route|PrivateRoute|ProtectedRoute|AuthRoute)\b(.*)")
PATH_ATTR_RX = re.compile(r"""\bpath\s*=\s*["']([^"']*)["']""")
PATH_OBJ_RX = re.compile(r"""\bpath\s*:\s*["'`]([^"'`]*)["'`]""")
ELEMENT_RX = re.compile(r"""(?:\belement\s*[=:]\s*\{?\s*<\s*([A-Z]\w*)|\bcomponent\s*[=:]\s*\{?\s*([A-Z]\w*))""")
LAZY_RX = re.compile(r"""import\(\s*["']([^"']+)["']\s*\)""")
LINK_RX = re.compile(r"""(?:<(?:Link|NavLink|RouterLink|router-link)\b[^>]*?\b(?:to|href)\s*=\s*["']([^"']+)["']|<a\b[^>]*?\bhref\s*=\s*["'](/[^"']*)["']|"""
                     r"""\b(?:navigate|router\.push|history\.push|router\.replace|\$router\.push)\(\s*["'`]([^"'`]+)["'`])""", re.S)
EXPORT_RX = re.compile(r"export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const)\s+([A-Z]\w*)")
EXPORT_DEFAULT_NAME_RX = re.compile(r"export\s+default\s+([A-Z]\w*)\s*;?\s*$", re.M)
MATCHER_RX = re.compile(r"matcher\s*:\s*\[([^\]]*)\]")
TAG_RX = re.compile(r"<(form|input|textarea|select|button|TextField|Select|Autocomplete|Checkbox|Radio|Switch|Button|IconButton|Input|FormControlLabel|DatePicker|el-input|el-button|v-text-field|v-btn)\b", re.I)
ATTR_RX = re.compile(r"""\b(label|name|id|placeholder|type|aria-label|data-testid|title|helperText|pattern|minLength|maxLength|min|max)\s*=\s*(?:"([^"]*)"|'([^']*)'|\{\s*["'`]([^"'`$]*)["'`]\s*\})""")
DYN_ATTR_RX = re.compile(r"""\b(label|name|placeholder|aria-label)\s*=\s*\{(?!\s*["'`])""")
REQUIRED_RX = re.compile(r"""(?<![\w-])required(?:\s*=\s*\{?\s*true\s*\}?|\s|/|>|$)""")
VALID_FIELD_RX = re.compile(r"""^\s*["']?(\w+)["']?\s*:\s*(?:yup|Yup|z|Joi)\.(.*)$""")
MSG_RX = re.compile(r"""\.(required|min|max|email|matches|length|url|positive|oneOf|regex|refine)\s*\((?:[^()"'`]|\([^()]*\))*?["'`]([^"'`]{3,120})["'`]""")
RHF_RX = re.compile(r"""\b(required|minLength|maxLength|pattern|validate)\s*:\s*(?:\{[^}]*?message\s*:\s*)?["'`]([^"'`]{3,120})["'`]""")
HELPER_RX = re.compile(r"""helperText\s*=\s*\{?[^"'`\n]*?["'`]([^"'`]{3,120})["'`]""")
TEXT_AFTER_TAG = re.compile(r">\s*([A-Za-z][^<>{}\n]{1,40}?)\s*<")
DANGER_RX = re.compile(r"(?i)log\s?out|sign\s?out|delete|remove|discharge|cancel|\bpay\b|pay now|checkout|refund|terminate|revoke|reset password|withdraw|send (?:sms|message|email)|notify")
MENU_ITEM_RX = re.compile(r"""(?:label|title|name|text)\s*:\s*["']([^"']{2,40})["'][^\n]{0,120}?(?:path|to|href|url)\s*:\s*["'](/[^"']*)["']""")


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _tag_text(text: str, start: int, limit: int = 700) -> str:
    """Return the opening tag text from `start`, tolerating '>' inside {...} expressions."""
    depth, i, n = 0, start, min(len(text), start + limit)
    while i < n:
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth = max(0, depth - 1)
        elif c == ">" and depth == 0:
            return text[start:i + 1]
        i += 1
    return text[start:n]


def _strip_braces(text: str) -> str:
    """Remove balanced {...} expressions so '/>' inside element={<X />} is not mistaken for the end of the tag."""
    out, depth = [], 0
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def _norm_route(p: str) -> str:
    p = p.strip()
    if not p:
        return ""
    if p != "/" and p.endswith("/"):
        p = p.rstrip("/")
    return p


def next_route_from_file(rel: str):
    """Next.js pages/ and app/ routing -> (route, kind) or None."""
    parts = rel.replace("\\", "/").split("/")
    for marker in ("pages", "app"):
        if marker in parts:
            idx = len(parts) - 1 - parts[::-1].index(marker)
            sub = parts[idx + 1:]
            if not sub or sub[0] == "api" or sub[0].startswith("_"):
                return None
            stem = Path(sub[-1]).stem
            if marker == "app":
                if stem != "page":
                    return None
                segs = sub[:-1]
            else:
                if stem.startswith("_"):
                    return None
                segs = sub[:-1] + ([] if stem == "index" else [stem])
            segs = [s for s in segs if not (s.startswith("(") and s.endswith(")"))]
            return "/" + "/".join(segs), "next-" + marker
    return None


def analyze_repo(root, app: str) -> dict:
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"Application directory not found: {root}")
    skipped, files = [], {}
    for p, rel in iter_source_files(root, skipped):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            skipped.append({"file": rel, "reason": f"unreadable ({e.strerror})"})
            continue
        cat = has_hard_secret(text)
        if cat:                                                  # fail closed: do not analyse or keep this file
            skipped.append({"file": rel, "reason": f"contains secret-like content ({cat}); not analysed"})
            continue
        files[rel] = text

    comp_files = {}
    for rel, text in files.items():
        base = Path(rel).stem
        comp_files.setdefault(base if base != "index" else Path(rel).parent.name, rel)
        for m in EXPORT_RX.finditer(text):
            comp_files.setdefault(m.group(1), rel)
        for m in EXPORT_DEFAULT_NAME_RX.finditer(text):
            comp_files.setdefault(m.group(1), rel)

    def resolve_import(from_rel: str, spec: str):
        if not spec.startswith("."):
            return None
        base = (Path(from_rel).parent / spec)
        norm = os.path.normpath(str(base)).replace("\\", "/")
        for cand in [norm] + [norm + e for e in ALLOWED_EXT] + [f"{norm}/index{e}" for e in ALLOWED_EXT]:
            if cand in files:
                return cand
        return None

    routes, nav, protected_prefixes = [], [], []
    for rel, text in files.items():
        nr = next_route_from_file(rel)
        if nr:
            routes.append({"route": nr[0], "kind": nr[1], "source_file": rel, "line": 1, "component": Path(rel).stem,
                           "component_file": rel, "protected": "unknown", "protection_evidence": "", "confidence": "high"})
        if Path(rel).stem == "middleware":
            for m in MATCHER_RX.finditer(text):
                for s in re.findall(r"""["']([^"']+)["']""", m.group(1)):
                    protected_prefixes.append((re.sub(r"[:(*].*$", "", s).rstrip("/") or "/", rel, _line_of(text, m.start())))
        if Path(rel).suffix.lower() in (".html", ".htm"):
            r = "/" if Path(rel).stem == "index" and Path(rel).parent == Path(".") else "/" + str(Path(rel).with_suffix("")).replace("\\", "/")
            routes.append({"route": r, "kind": "static-html", "source_file": rel, "line": 1, "component": Path(rel).stem,
                           "component_file": rel, "protected": "unknown", "protection_evidence": "", "confidence": "medium"})

        cfg = bool(ROUTER_CFG_RX.search(text))
        stack = []                                               # protection flags of enclosing <Route> wrappers
        for i, line in enumerate(text.splitlines(), 1):
            for _ in range(line.count("</Route>")):
                if stack:
                    stack.pop()
            jm = JSX_ROUTE_RX.search(line)
            pm = PATH_ATTR_RX.search(line) if jm else PATH_OBJ_RX.search(line)
            self_closing = bool(jm) and _strip_braces(line[jm.start():]).rstrip().endswith("/>")
            if jm:
                wrapper_protected = bool(PROTECT_RX.search(line)) or jm.group(1) != "Route"
                if pm and pm.group(1) is not None:
                    el = ELEMENT_RX.search(line)
                    comp = (el.group(1) or el.group(2)) if el else ""
                    lazy = LAZY_RX.search(line)
                    cf = resolve_import(rel, lazy.group(1)) if lazy else comp_files.get(comp)
                    prot = wrapper_protected or any(stack)
                    ev = "wrapped by auth guard in source" if prot else ""
                    routes.append({"route": _norm_route(pm.group(1)) or "(index)", "kind": "react-router", "source_file": rel, "line": i,
                                   "component": comp, "component_file": cf, "protected": True if prot else "unknown",
                                   "protection_evidence": ev, "confidence": "high" if pm.group(1).startswith("/") else "medium",
                                   "relative": not pm.group(1).startswith("/")})
                elif re.search(r"\bindex\b", line):
                    pass
                if not self_closing and "<Route" in line:
                    stack.append(wrapper_protected)
            elif pm and cfg:
                obj_line = line + " " + " ".join(text.splitlines()[i:i + 3])
                el = ELEMENT_RX.search(obj_line)
                comp = (el.group(1) or el.group(2)) if el else ""
                lazy = LAZY_RX.search(obj_line)
                cf = resolve_import(rel, lazy.group(1)) if lazy else comp_files.get(comp)
                prot = bool(PROTECT_RX.search(obj_line))
                routes.append({"route": _norm_route(pm.group(1)) or "(index)", "kind": "router-config", "source_file": rel, "line": i,
                               "component": comp, "component_file": cf, "protected": True if prot else "unknown",
                               "protection_evidence": "auth guard/meta near route definition" if prot else "",
                               "confidence": "high" if pm.group(1).startswith("/") else "medium",
                               "relative": not pm.group(1).startswith("/")})
            for lm in LINK_RX.finditer(line):
                target = next(g for g in lm.groups() if g)
                if target.startswith("/") and not target.startswith("//"):
                    nav.append({"from_file": rel, "line": i, "target": _norm_route(target.split("?")[0].split("#")[0]) or "/"})
            for mm in MENU_ITEM_RX.finditer(line):
                nav.append({"from_file": rel, "line": i, "target": _norm_route(mm.group(2)), "label": redact(mm.group(1))})

    for r in routes:                                              # Next.js middleware matcher => protected
        for pref, mrel, mline in protected_prefixes:
            if pref and (r["route"] == pref or r["route"].startswith(pref + "/")):
                r["protected"], r["protection_evidence"] = True, f"middleware matcher in {mrel}:{mline}"

    # de-duplicate routes, keep best confidence
    uniq = {}
    for r in routes:
        key = (r["route"], r["source_file"])
        if key not in uniq or (r["confidence"] == "high" and uniq[key]["confidence"] != "high"):
            uniq[key] = r
    routes = sorted(uniq.values(), key=lambda r: (r["route"], r["source_file"]))
    linked = {}
    for n in nav:
        linked.setdefault(n["target"], set()).add(n["from_file"])
    for r in routes:
        r["linked_from"] = sorted(linked.get(r["route"], []))
        r["reachable_via_links"] = bool(r["linked_from"]) or r["route"] in ("/", "(index)")

    # controls / forms / validation, per file
    controls, forms, validation = [], [], []
    for rel, text in files.items():
        file_controls = []
        for m in TAG_RX.finditer(text):
            tag = m.group(1)
            ttext = _tag_text(text, m.start())
            attrs = {}
            for am in ATTR_RX.finditer(ttext):
                attrs.setdefault(am.group(1), next((g for g in am.groups()[1:] if g is not None), ""))
            dyn = sorted({d.group(1) for d in DYN_ATTR_RX.finditer(ttext)})
            kind = tag.lower()
            if kind in ("button", "iconbutton", "el-button", "v-btn"):
                kind = "button"
            elif kind in ("textfield", "input", "el-input", "v-text-field", "datepicker"):
                kind = "input"
            elif kind in ("select", "autocomplete"):
                kind = "select"
            elif kind in ("checkbox", "radio", "switch", "formcontrollabel"):
                kind = kind.replace("formcontrollabel", "choice")
            text_after = TEXT_AFTER_TAG.match(text[m.start() + len(ttext) - 1: m.start() + len(ttext) + 80])
            btn_text = text_after.group(1).strip() if text_after and kind == "button" else ""
            accessible = attrs.get("aria-label") or attrs.get("label") or btn_text or attrs.get("placeholder") or attrs.get("title") or attrs.get("name") or attrs.get("id") or ""
            c = {"source_file": rel, "line": _line_of(text, m.start()), "element": tag, "kind": kind,
                 "label": redact(attrs.get("label", "")), "name": attrs.get("name") or attrs.get("id") or "", "placeholder": redact(attrs.get("placeholder", "")),
                 "type": attrs.get("type", ""), "accessible_name": redact(accessible), "text": redact(btn_text),
                 "required": bool(REQUIRED_RX.search(ttext)), "pattern": attrs.get("pattern", ""),
                 "min_length": attrs.get("minLength", ""), "max_length": attrs.get("maxLength", ""),
                 "dynamic_attributes": dyn, "dangerous": bool(DANGER_RX.search(accessible)),
                 "confidence": "medium" if (accessible and not dyn) else "low"}
            if kind != "form":
                file_controls.append(c)
            else:
                forms.append({"source_file": rel, "line": c["line"], "name": c["name"], "fields": [], "buttons": [],
                              "has_password": False, "confidence": "medium"})
        controls.extend(file_controls)
        file_forms = [f for f in forms if f["source_file"] == rel]
        if file_forms:                                           # attach this file's controls to its forms (nearest preceding form)
            for c in file_controls:
                owner = None
                for f in file_forms:
                    if f["line"] <= c["line"]:
                        owner = f
                if owner is None:
                    continue
                if c["kind"] == "button":
                    owner["buttons"].append(c)
                else:
                    owner["fields"].append(c)
                    if c["type"].lower() == "password" or "password" in (c["name"] + c["label"]).lower():
                        owner["has_password"] = True

        # validation messages and requirements
        for i, line in enumerate(text.splitlines(), 1):
            fm = VALID_FIELD_RX.match(line)
            if fm:
                for mm in MSG_RX.finditer(fm.group(2)):
                    validation.append({"source_file": rel, "line": i, "field": fm.group(1), "rule": mm.group(1),
                                       "message": redact(mm.group(2)), "confidence": "medium"})
            else:
                for mm in RHF_RX.finditer(line):
                    validation.append({"source_file": rel, "line": i, "field": "", "rule": mm.group(1),
                                       "message": redact(mm.group(2)), "confidence": "low"})
            for mm in HELPER_RX.finditer(line):
                validation.append({"source_file": rel, "line": i, "field": "", "rule": "helperText",
                                   "message": redact(mm.group(1)), "confidence": "low"})

    # assign controls/forms/validation to routes via component file
    by_file = {}
    for r in routes:
        for f in {r["source_file"], r.get("component_file")}:
            if f:
                by_file.setdefault(f, []).append(r["route"])
    for coll in (controls, forms, validation):
        for item in coll:
            item["routes"] = sorted(set(by_file.get(item["source_file"], [])))
    return {
        "app": app, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "static-source-analysis (regex heuristics; no code executed)",
        "stats": {"files_analysed": len(files), "files_skipped": len(skipped), "routes": len(routes), "controls": len(controls),
                  "forms": len(forms), "validation_messages": len(validation)},
        "skipped": skipped, "routes": routes, "nav_links": nav, "controls": controls, "forms": forms, "validation": validation,
        "protected_prefixes": [{"prefix": p, "source_file": f, "line": l} for p, f, l in protected_prefixes],
        "limitations": ["Routes built dynamically or from backend data are not seen.",
                        "Relative nested routes are not joined to their parents (marked relative).",
                        "Protection is only reported when an auth guard is visible in source; 'unknown' is NOT 'public'."],
    }


# ---------------------------------------------------------------------------------------------------
# optional browser inspection (GET only)
# ---------------------------------------------------------------------------------------------------
_PAGE_JS = r"""() => {
 const vis = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
 const nm = e => (e.getAttribute('aria-label') || (e.labels && e.labels[0] && e.labels[0].innerText) || e.innerText || e.value || e.placeholder || e.name || '').trim().replace(/\s+/g,' ').slice(0,60);
 const q = s => [...document.querySelectorAll(s)].filter(vis);
 return {title: document.title.slice(0,80), headings: q('h1,h2').map(e => e.innerText.trim().slice(0,60)).slice(0,5),
   hasPassword: q('input[type=password]').length > 0, forms: q('form').length,
   inputs: q('input:not([type=hidden]),textarea,select').map(i => ({type: i.type || i.tagName.toLowerCase(), name: i.name || i.id || '', accessible_name: nm(i), placeholder: i.placeholder || '', required: !!i.required})).slice(0,40),
   buttons: q('button,[role=button],input[type=submit]').map(nm).filter(Boolean).slice(0,30),
   links: q('a[href]').map(a => ({text: nm(a), href: a.getAttribute('href')})).filter(l => l.href && l.href.startsWith('/')).slice(0,60)}; }"""


def inspect_routes(base_url: str, routes, *, max_pages: int = 25, timeout_ms: int = 15000, env=None, headless: bool = True) -> list:
    """GET-only inspection of explicit routes. Returns per-route facts; page text and cookies are never stored."""
    from playwright.sync_api import sync_playwright
    from .safety import RequestGuard, normalize_origin
    origin = normalize_origin(base_url)
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context()
        guard = RequestGuard(env, origin, writes_enabled=False)
        guard.block_all = True                       # discovery never sends anything but GET
        guard.attach(ctx)
        page = ctx.new_page()
        try:
            for route in list(routes)[:max_pages]:
                if not route.startswith("/") or any(ch in route for ch in ":*[]()"):
                    out.append({"route": route, "inspected": False, "reason": "dynamic or relative route; not visited"})
                    continue
                rec = {"route": route, "inspected": True}
                try:
                    resp = page.goto(origin + route, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        pass
                    info = page.evaluate(_PAGE_JS)
                    final = page.url
                    rec.update(status=resp.status if resp else None, final_path=final[len(origin):] if final.startswith(origin) else "(left origin)",
                               redirected=(final[len(origin):] != route), looks_like_login=info["hasPassword"], **{k: info[k] for k in ("title", "headings", "forms", "inputs", "buttons", "links")})
                except Exception as e:
                    rec.update(error=redact(f"{type(e).__name__}: {str(e).splitlines()[0][:150] if str(e) else ''}"))
                out.append(rec)
        finally:
            ctx.close()
            browser.close()
    return json.loads(redact(json.dumps(out)))


def write_discovery(result: dict, out_dir, browser_info=None) -> dict:
    """Write JSON + Markdown discovery docs (redacted, fail-closed). Returns the written paths."""
    from .redaction import assert_clean, assert_clean_obj, redact_obj
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = redact_obj(result)
    if browser_info is not None:
        res["browser_inspection"] = redact_obj(browser_info)
    jtxt = json.dumps(res, indent=2, ensure_ascii=False)
    assert_clean_obj(res, what=f"discovery data for {result['app']}")

    def cell(x):
        return str(x if x not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")

    r_lines = [f"# Route / page discovery - {res['app']}", "", f"_Generated {res['generated_at']} by {res['method']}._", "",
               "| Route | Kind | Protected | Component | Source | Linked from | Confidence |", "|---|---|---|---|---|---|---|"]
    for r in res["routes"]:
        prot = "yes (" + cell(r["protection_evidence"]) + ")" if r["protected"] is True else "unknown"
        r_lines.append(f"| `{cell(r['route'])}` | {cell(r['kind'])} | {prot} | {cell(r['component'])} | {cell(r['source_file'])}:{r['line']} | "
                       f"{cell(', '.join(r['linked_from'][:3]))} | {r['confidence']} |")
    if browser_info:
        r_lines += ["", "## Browser inspection (GET only)", "", "| Route | HTTP | Final path | Login-like | Forms | Note |", "|---|---|---|---|---|---|"]
        for b in res["browser_inspection"]:
            r_lines.append(f"| `{cell(b['route'])}` | {cell(b.get('status'))} | {cell(b.get('final_path'))} | {cell(b.get('looks_like_login'))} | "
                           f"{cell(b.get('forms'))} | {cell(b.get('reason') or b.get('error'))} |")
    r_lines += ["", "## Skipped files (not analysed)", ""] + [f"- `{cell(s['file'])}` - {cell(s['reason'])}" for s in res["skipped"][:200]]
    r_lines += ["", "## Limitations", ""] + [f"- {x}" for x in res["limitations"]]

    f_lines = [f"# Forms and controls - {res['app']}", "", "## Forms", ""]
    for f in res["forms"]:
        f_lines.append(f"### `{cell(f['source_file'])}:{f['line']}` (routes: {cell(', '.join(f['routes']))}) - confidence {f['confidence']}"
                       + (" - contains a password field" if f["has_password"] else ""))
        f_lines += ["", "| Field | Label | Placeholder | Type | Required | Source |", "|---|---|---|---|---|---|"]
        f_lines += [f"| {cell(c['name'])} | {cell(c['label'])} | {cell(c['placeholder'])} | {cell(c['type'] or c['kind'])} | {c['required']} | line {c['line']} |" for c in f["fields"]]
        f_lines += ["", "Buttons: " + (", ".join(cell(b["accessible_name"]) for b in f["buttons"]) or "-"), ""]
    f_lines += ["## All controls", "", "| Kind | Accessible name | Label | Placeholder | Name | Required | Danger | Source | Confidence |", "|---|---|---|---|---|---|---|---|---|"]
    f_lines += [f"| {c['kind']} | {cell(c['accessible_name'])} | {cell(c['label'])} | {cell(c['placeholder'])} | {cell(c['name'])} | {c['required']} | "
                f"{c['dangerous']} | {cell(c['source_file'])}:{c['line']} | {c['confidence']} |" for c in res["controls"]]
    f_lines += ["", "## Validation messages found in source", "", "| Field | Rule | Message | Source | Confidence |", "|---|---|---|---|---|"]
    f_lines += [f"| {cell(v['field'])} | {cell(v['rule'])} | {cell(v['message'])} | {cell(v['source_file'])}:{v['line']} | {v['confidence']} |" for v in res["validation"]]
    rtxt, ftxt = "\n".join(r_lines) + "\n", "\n".join(f_lines) + "\n"
    assert_clean(rtxt, what="routes doc")
    assert_clean(ftxt, what="forms doc")
    paths = {"json": out_dir / "discovery.json", "routes_md": out_dir / "routes.md", "forms_md": out_dir / "forms.md"}
    paths["json"].write_text(jtxt, encoding="utf-8")
    paths["routes_md"].write_text(rtxt, encoding="utf-8")
    paths["forms_md"].write_text(ftxt, encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}
