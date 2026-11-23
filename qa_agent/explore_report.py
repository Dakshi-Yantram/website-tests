"""Bug report for the autonomous explorer: HTML (for people), Markdown (for CI job summary / chat) and JSON (for tools).

Everything is redacted, then re-checked; if a secret-like value survives, nothing is written (fail closed) - same contract as
qa_agent/reporting.py.
"""
from __future__ import annotations

import html
import json
import shutil
from dataclasses import asdict
from pathlib import Path

from .errors import SensitiveDataError
from .explorer import SEVERITIES, ExploreResult
from .redaction import assert_clean_obj, redact_obj

COLORS = {"High": "#cf222e", "Medium": "#bc4c00", "Low": "#9a6700", "Info": "#57606a"}


def verdict(result: ExploreResult) -> tuple:
    """-> (headline, exit_hint, explanation). Never says 'PASS': this is an exploration, not a business-rule verification."""
    bugs = [f for f in result.findings if f.severity in ("High", "Medium")]
    incomplete = [r for r in result.roles if r.status in ("LOGIN FAILED", "BLOCKED", "PARTIAL")]
    explored = [r for r in result.roles if r.status in ("EXPLORED", "PARTIAL") and r.pages]
    if not explored and not any(f.category == "login" for f in result.findings):
        return "NOTHING WAS TESTED", "blocked", "No role could be signed in (credentials missing or not usable)."
    if bugs:
        head = f"{len(bugs)} BUG(S) FOUND"
    elif result.findings:
        head = "NO BUGS - ONLY MINOR OBSERVATIONS"
    else:
        head = "NO BUGS DETECTED by the automatic checks"
    extra = ""
    if incomplete:
        extra = " Some roles were not (fully) explored - see 'Roles'."
    return head + (" (INCOMPLETE)" if incomplete else ""), "bugs" if bugs else "ok", \
        "Automatic exploration checks pages, forms, navigation and errors. It does NOT verify business rules (calculations, clinical logic)." + extra


def _env(m: dict) -> str:
    return ("not declared (no TEST_ENV): treated as read-only, screenshots off by default" if m["environment_kind"] == "unverified"
            else f"{m['environment']} ({m['environment_kind']})")


def _mode_note(m: dict) -> str:
    if m.get("fill"):
        return ("FILL MODE (staging only): forms were filled with synthetic data and saved - records marked " + str(m["run_id"]) +
                " (AUTOTEST_" + str(m["run_id"]) + ") were created, see 'Records created'. DELETE requests and payment endpoints were blocked; "
                "destructive/payment/messaging controls were never clicked.")
    return "Read-only run: every write request was blocked at the network layer; destructive/payment/messaging controls were never clicked."


def _sev_counts(findings) -> dict:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}


def _plain(result: ExploreResult) -> dict:
    head, hint, why = verdict(result)
    return {"meta": result.meta, "verdict": head, "verdict_note": why, "counts": _sev_counts(result.findings),
            "findings": [asdict(f) for f in result.findings], "roles": [asdict(r) for r in result.roles],
            "pages": [asdict(p) for p in result.pages], "validation_messages_observed": result.validations,
            "dialogs_opened": result.dialogs, "records_created_by_fill_mode": result.created,
            "write_requests_blocked_by_read_only_guard": [{"request": k, "count": v} for k, v in sorted(result.blocked_writes.items())],
            "notes": result.notes}


def _md(d: dict) -> str:
    m, c = d["meta"], d["counts"]
    out = [f"# Autonomous test report - run {m['run_id']}", "",
           f"**{d['verdict']}**", "",
           f"- Environment: {_env(m)} | Apps: {', '.join(m['apps'])} | Duration: {m.get('seconds', '?')}s | Browser: {m['browser_version']}",
           f"- Findings: " + ", ".join(f"{c[s]} {s}" for s in SEVERITIES),
           f"- {d['verdict_note']}", "",
           _mode_note(m), ""]
    bugs = [f for f in d["findings"] if f["severity"] in ("High", "Medium")]
    obs = [f for f in d["findings"] if f["severity"] not in ("High", "Medium")]
    out += ["## Bugs", ""]
    if not bugs:
        out += ["None found.", ""]
    for f in bugs:
        out += [f"### {f['id']} [{f['severity']}] {f['title']}", "",
                f"- Role(s): {f['role']} | App: {f['app']} | Category: {f['category']} | Seen {f['occurrences']}x",
                f"- Where: {', '.join(f['pages'][:6])}" + (" ..." if len(f["pages"]) > 6 else ""),
                f"- What: {f['detail']}"]
        if f["evidence"]:
            out += ["- Evidence:"] + [f"  - `{e}`" for e in f["evidence"][:6]]
        out += ["- How to reproduce:"] + [f"  {i}. {s}" for i, s in enumerate(f["repro"], 1)]
        if f["screenshot"]:
            out += [f"- Screenshot: {f['screenshot']}"]
        out += [""]
    if obs:
        out += ["## Minor observations", ""]
        for f in obs:
            out += [f"- **{f['id']} [{f['severity']}]** {f['title']} - {f['detail']} (where: {', '.join(f['pages'][:4])}; role {f['role']})"]
        out += [""]
    out += ["## Roles", "", "| App | Role | Result | Pages | Login | Note |", "|---|---|---|---|---|---|"]
    for r in d["roles"]:
        out += [f"| {r['app']} | {r['role']} | {r['status']} | {r['pages']} | {r['login_seconds']:.1f}s ({r['login_attempts']} attempt(s)) | {r['reason']}{' - video: ' + r['video'] if r.get('video') else ''} |"]
    out += ["", "## Pages explored", "", "| Role | Page | HTTP | Load | What was exercised | Findings |", "|---|---|---|---|---|---|"]
    for p in d["pages"]:
        out += [f"| {p['role']} | `{p['path']}` | {p['status'] or '-'} | {p['load_s']}s | {'; '.join(p['checks'][:4])} | {p['findings']} |"]
    if d["validation_messages_observed"]:
        out += ["", "## Validation behaviour observed (empty required forms)", "", "| Role | Page | Where | Button | Message(s) shown |", "|---|---|---|---|---|"]
        for v in d["validation_messages_observed"]:
            msg = "; ".join(v["messages"]) or (f"{v['invalid_fields']} field(s) marked invalid" if v["invalid_fields"] else "(none)")
            out += [f"| {v['role']} | `{v['path']}` | {v['where']} | {v['button']} | {msg} |"]
    if d.get("records_created_by_fill_mode"):
        out += ["", "## Records created (delete these from staging when done)", "", "| Role | Page | Where | Fields filled | Writes sent | Outcome |", "|---|---|---|---|---|---|"]
        for c in d["records_created_by_fill_mode"]:
            out += [f"| {c['role']} | `{c['path']}` | {c['where']} | {c['fields_filled']} | {c['writes_sent']} | {c['outcome']} |"]
        out += ["", f"Search for the marker `{d['records_created_by_fill_mode'][0]['marker']}` to find them."]
    if d["write_requests_blocked_by_read_only_guard"]:
        out += ["", "## Write requests the app tried to send (blocked by the read-only guard)", "",
                "If some of these are really read-only list/search calls, pages may have loaded without data. Re-run with `--allow-read-post REGEX` "
                "(only for endpoints you know are read-only) to let them through.", ""]
        out += [f"- `{x['request']}` x{x['count']}" for x in d["write_requests_blocked_by_read_only_guard"][:30]]
    if d["notes"]:
        out += ["", "## Notes", ""] + [f"- {n}" for n in d["notes"][:40]]
    return "\n".join(out) + "\n"


def _html(d: dict) -> str:
    e = html.escape
    m, c = d["meta"], d["counts"]
    bugs = [f for f in d["findings"] if f["severity"] in ("High", "Medium")]
    head_color = COLORS["High"] if bugs else ("#1a7f37" if "NOTHING" not in d["verdict"] else "#9a6700")

    def card(f):
        rows = "".join(f"<li><code>{e(x)}</code></li>" for x in f["evidence"][:6])
        steps = "".join(f"<li>{e(s)}</li>" for s in f["repro"])
        shot = (f'<p><a href="{e(f["screenshot"])}" target="_blank" rel="noopener">Screenshot (masked)</a></p>' if f["screenshot"] else "")
        return (f'<details class="f" {"open" if f["severity"] == "High" else ""}><summary>'
                f'<span class="sev" style="background:{COLORS[f["severity"]]}">{e(f["severity"])}</span> <b>{e(f["id"])}</b> {e(f["title"])}'
                f'<span class="meta"> &middot; role(s) {e(f["role"])} &middot; {e(f["category"])} &middot; seen {f["occurrences"]}x</span></summary>'
                f'<p>{e(f["detail"])}</p><p class="meta">Where: {e(", ".join(f["pages"][:8]))}</p>'
                f'{"<p><b>Evidence</b></p><ul>" + rows + "</ul>" if rows else ""}'
                f'<p><b>How to reproduce</b></p><ol>{steps}</ol>{shot}</details>')

    cards = "".join(card(f) for f in d["findings"] if f["severity"] in ("High", "Medium")) or "<p>No High/Medium bugs found.</p>"
    minor = "".join(card(f) for f in d["findings"] if f["severity"] not in ("High", "Medium")) or "<p>None.</p>"
    roles = "".join(f"<tr><td>{e(r['app'])}</td><td>{e(r['role'])}</td><td>{e(r['status'])}</td><td>{r['pages']}</td>"
                    f"<td>{r['login_seconds']:.1f}s / {r['login_attempts']}</td><td>{e(r['reason'])}</td></tr>" for r in d["roles"])
    pages = "".join(f"<tr><td>{e(p['role'])}</td><td><code>{e(p['path'])}</code></td><td>{p['status'] or '-'}</td><td>{p['load_s']}s</td>"
                    f"<td>{e('; '.join(p['checks'][:4]))}</td><td>{p['findings']}</td></tr>" for p in d["pages"])
    vals = "".join(f"<tr><td>{e(v['role'])}</td><td><code>{e(v['path'])}</code></td><td>{e(v['where'])}</td><td>{e(v['button'])}</td>"
                   f"<td>{e('; '.join(v['messages']) or (str(v['invalid_fields']) + ' field(s) marked invalid' if v['invalid_fields'] else '(none)'))}</td></tr>"
                   for v in d["validation_messages_observed"])
    blocked = "".join(f"<li><code>{e(x['request'])}</code> x{x['count']}</li>" for x in d["write_requests_blocked_by_read_only_guard"][:30])
    notes = "".join(f"<li>{e(n)}</li>" for n in d["notes"][:40])
    videos = "".join(f"<h3>{e(r['app'])} / {e(r['role'])}</h3><video controls preload='metadata' style='max-width:100%;width:900px' src='{e(r['video'])}'></video>"
                     for r in d["roles"] if r.get("video"))
    created = "".join(f"<tr><td>{e(c['role'])}</td><td><code>{e(c['path'])}</code></td><td>{e(c['where'])}</td><td>{c['fields_filled']}</td><td>{c['writes_sent']}</td><td>{e(c['outcome'])}</td></tr>"
                      for c in d.get("records_created_by_fill_mode", []))
    counts = "".join(f'<div class="c"><b style="color:{COLORS[s]}">{c[s]}</b><span>{s}</span></div>' for s in SEVERITIES)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Autonomous test report {e(m['run_id'])}</title>
<style>body{{font:14px/1.5 system-ui,Segoe UI,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}}main{{max-width:1100px;margin:auto;padding:24px}}
h1{{margin:0 0 4px}}.banner{{background:{head_color};color:#fff;padding:14px 18px;border-radius:8px;font-size:18px;font-weight:600;margin:12px 0}}
.cards{{display:flex;gap:12px;margin:12px 0}}.c{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:10px 18px;text-align:center}}.c b{{display:block;font-size:26px}}
.f{{background:#fff;border:1px solid #d0d7de;border-radius:8px;padding:10px 14px;margin:8px 0}}summary{{cursor:pointer}}.sev{{color:#fff;border-radius:10px;padding:1px 9px;font-size:12px}}
.meta{{color:#656d76;font-size:12px}}table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px}}td,th{{border:1px solid #d0d7de;padding:5px 8px;text-align:left;vertical-align:top}}
th{{background:#eaeef2}}code{{background:#eff1f3;padding:0 4px;border-radius:4px}}h2{{margin-top:28px}}</style></head><body><main>
<h1>Autonomous test report</h1><div class="meta">Run {e(m['run_id'])} &middot; {e(_env(m))} &middot; apps: {e(', '.join(m['apps']))} &middot; {m.get('seconds', '?')}s &middot; {e(m['browser_version'])}</div>
<div class="banner">{e(d['verdict'])}</div><p>{e(d['verdict_note'])}</p>
<p class="meta">{e(_mode_note(m))}</p>
<div class="cards">{counts}</div>
<h2>Bugs</h2>{cards}<h2>Minor observations</h2>{minor}
<h2>Roles</h2><table><tr><th>App</th><th>Role</th><th>Result</th><th>Pages</th><th>Login (time / attempts)</th><th>Note</th></tr>{roles}</table>
{"<h2>Video of the test run</h2>" + videos if videos else ""}
<h2>Pages explored</h2><table><tr><th>Role</th><th>Page</th><th>HTTP</th><th>Load</th><th>Exercised</th><th>Findings</th></tr>{pages}</table>
{"<h2>Records created (delete these from staging when done)</h2><table><tr><th>Role</th><th>Page</th><th>Where</th><th>Fields</th><th>Writes</th><th>Outcome</th></tr>" + created + "</table>" if created else ""}
{"<h2>Validation behaviour observed</h2><table><tr><th>Role</th><th>Page</th><th>Where</th><th>Button</th><th>Message(s) shown</th></tr>" + vals + "</table>" if vals else ""}
{"<h2>Write requests blocked by the read-only guard</h2><p class='meta'>If some of these are really read-only list/search calls, pages may have loaded without data. Re-run with <code>--allow-read-post REGEX</code> (only for endpoints you know are read-only).</p><ul>" + blocked + "</ul>" if blocked else ""}
{"<h2>Notes</h2><ul>" + notes + "</ul>" if notes else ""}
</main></body></html>"""


def write_reports(result: ExploreResult, out_dir, screenshots_dir=None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = f"auto_report_{result.meta['run_id']}"
    data = redact_obj(_plain(result))
    for r in data["roles"]:
        v = r.get("video") or ""
        if v and Path(v).is_file():
            vd = out_dir / "videos"
            vd.mkdir(parents=True, exist_ok=True)
            shutil.copy2(v, vd / Path(v).name)
            r["video"] = "videos/" + Path(v).name
        else:
            r["video"] = ""
    try:
        assert_clean_obj(data, what="autonomous report")
    except SensitiveDataError:
        (out_dir / f"{base}_WITHHELD.txt").write_text(
            "Report withheld: sensitive-looking data was detected and could not be safely redacted.\nReview the run locally; nothing from it was written.\n",
            encoding="utf-8")
        raise
    if screenshots_dir:
        src_dir, dst_dir = Path(screenshots_dir), out_dir / "screenshots"
        if src_dir.resolve() != dst_dir.resolve():
            for f in data["findings"]:
                if f["screenshot"]:
                    s = src_dir / Path(f["screenshot"]).name
                    if s.is_file():
                        dst_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(s, dst_dir / s.name)
    paths = {"json": out_dir / f"{base}.json", "md": out_dir / f"{base}.md", "html": out_dir / f"{base}.html"}
    paths["json"].write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["md"].write_text(_md(data), encoding="utf-8")
    paths["html"].write_text(_html(data), encoding="utf-8")
    return paths
