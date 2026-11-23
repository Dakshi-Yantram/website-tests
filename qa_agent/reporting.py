"""JSON + HTML + Markdown run reports and the workflow inventory. Everything is redacted, then re-checked;
if anything sensitive-looking survives, NOTHING is written (fail closed)."""
import html
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import yaml

from .errors import SensitiveDataError
from .diagnostics import generate_findings, enrich_with_ai
from .artifacts import safe_name
from .redaction import assert_clean, assert_clean_obj, redact, redact_obj
from .workflows import DRAFTS_DIR

STATUS_ORDER = ("PASS", "FAIL", "BLOCKED", "NOT VERIFIED", "NOT TESTED")
COLORS = {"PASS": "#1a7f37", "FAIL": "#cf222e", "BLOCKED": "#9a6700", "NOT VERIFIED": "#8250df", "NOT TESTED": "#57606a"}


def summarize(results) -> dict:
    out = {s: 0 for s in STATUS_ORDER}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    out["total"] = len(results)
    return out


def build_inventory(workflows, results, store, drafts_dir=DRAFTS_DIR) -> list:
    """One row per known workflow: approved/changed/unapproved ones from the YAML files, plus every draft file."""
    last = {r.id: r.status for r in results}
    rows = []
    for w in workflows:
        state, detail = store.state(w)
        rows.append({"id": w["id"], "app": w["app"], "title": w["title"], "source": Path(w.get("_file", "")).name, "risk": w["risk"],
                     "writes": w["writes"], "approval": state, "approval_detail": detail,
                     "last_result": last.get(w["id"], "NOT TESTED"), "reason_not_run": ""})
    known = {r["id"] for r in rows}
    d = Path(drafts_dir)
    if d.is_dir():
        for f in sorted(d.glob("*/*.yml")):
            try:
                doc = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
                for w in doc.get("workflows", []):
                    if w.get("id") in known:
                        continue
                    rows.append({"id": w.get("id", "?"), "app": w.get("app", "?"), "title": w.get("title", ""), "source": f"{f.parent.name}/{f.name}",
                                 "risk": w.get("risk", "?"), "writes": bool(w.get("writes")), "approval": "draft",
                                 "approval_detail": "generated draft - not executable until approved", "last_result": "NOT TESTED",
                                 "reason_not_run": "draft: awaiting explicit approval"})
            except Exception as e:
                rows.append({"id": f.stem, "app": "?", "title": "(unreadable draft)", "source": f"{f.parent.name}/{f.name}", "risk": "?", "writes": False,
                             "approval": "draft", "approval_detail": f"unreadable: {type(e).__name__}", "last_result": "NOT TESTED", "reason_not_run": "invalid draft file"})
    res_by_id = {r.id: r for r in results}
    for r in rows:
        if r["id"] in res_by_id and res_by_id[r["id"]].status in ("BLOCKED", "NOT TESTED"):
            r["reason_not_run"] = res_by_id[r["id"]].reason
    return rows


def _e(x):
    return html.escape(str(x if x is not None else ""))


def _screenshot_markup(step):
    link = step.get("screenshot_link", "")
    if not link:
        return ""
    safe = _e(link)
    return (f'<br><a href="{safe}" target="_blank" rel="noopener">View failure screenshot</a>'
            f'<br><a href="{safe}"><img loading="lazy" class="shot" src="{safe}" '
            f'alt="Masked failure screenshot"></a>')


def _source_md(sources) -> list:
    """Source code under test. Missing/dirty states are shown explicitly, never hidden."""
    if not sources:
        return ["- Source commits: **NOT RECORDED** (run with --source frontend=PATH --source backend=PATH)"]
    out = ["- Source commits under test:"]
    for n, r in sources.items():
        if not r.get("available"):
            out.append(f"  - `{n}`: **UNAVAILABLE** - {r.get('reason', '')}")
        else:
            flag = f" **UNCOMMITTED CHANGES ({r['dirty_files']} file(s))**" if r.get("dirty") else ""
            out.append(f"  - `{n}`: `{r['sha']}` on `{r.get('branch', '')}`{flag}")
    return out


def _source_html(sources) -> str:
    if not sources:
        return "<br>Source commits: <b>NOT RECORDED</b>"
    parts = []
    for n, r in sources.items():
        if not r.get("available"):
            parts.append(f"{_e(n)}: <b>UNAVAILABLE</b> ({_e(r.get('reason', ''))})")
        else:
            flag = f" <b>UNCOMMITTED CHANGES ({_e(r['dirty_files'])})</b>" if r.get("dirty") else ""
            parts.append(f"{_e(n)}: <code>{_e(r['sha'])}</code> on {_e(r.get('branch', ''))}{flag}")
    return "<br>Source commits: " + " &middot; ".join(parts)


def write_reports(results, meta: dict, out_dir, inventory=None, basename: str | None = None) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = meta["run_id"]
    base = basename or f"agent_report_{run_id}"
    data = redact_obj({"meta": meta, "summary": summarize(results), "workflows": [asdict(r) for r in results],
                       "inventory": inventory or [],
                       "skipped": [{"id": r.id, "status": r.status, "reason": r.reason} for r in results if r.status in ("NOT TESTED", "BLOCKED")]})
    data["findings"] = enrich_with_ai(generate_findings(data["workflows"]),
                                           env_kind=str(meta.get("environment_kind", "")))
    # A report directory is independently downloadable: it contains its own masked screenshot copies.
    # Never copy arbitrary paths: only screenshot files recorded by the runner under /screenshots/.
    # The PNG files are generated through the masking policy; CI never publishes unmasked captures.
    screenshot_sources = {}
    for w in data["workflows"]:
        for st in w["steps"]:
            src = st.get("screenshot", "")
            if not src:
                continue
            path = Path(src)
            if (path.suffix.lower() != ".png" or path.parent.name != "screenshots" or
                path.name != safe_name(path.stem, limit=255) + ".png" or not path.is_file() or
                path.stat().st_size > 8_000_000 or
                path.open('rb').read(8) != b"\x89PNG\r\n\x1a\n"):
                st["screenshot_note"] = "Screenshot file missing or rejected for safety"
                st["screenshot"] = ""
                continue
            rel = "screenshots/" + path.name
            st["screenshot_link"] = rel
            screenshot_sources[rel] = path
    for finding in data["findings"]:
        file = finding.get("screenshot", "")
        if file and "screenshots/" + Path(file).name in screenshot_sources:
            finding["screenshot_link"] = "screenshots/" + Path(file).name
    try:
        assert_clean_obj(data, what="test report")
    except SensitiveDataError:
        (out_dir / f"{base}_WITHHELD.txt").write_text(
            "Report withheld: sensitive-looking data was detected and could not be safely redacted.\n"
            "Review the run locally; nothing from it was written.\n", encoding="utf-8")
        raise

    s, m = data["summary"], data["meta"]
    # ---- Markdown
    md = [f"# Test run {run_id}", "",
          f"- Environment: **{m['environment']}** ({m['environment_kind']})   Mode: **{m['mode']}**",
          f"- Started: {m['started']}   Finished: {m.get('finished', '')}",
          f"- Browser: {m['browser_version']}   Python: {m['python']}   Platform: {m['platform']}",
          *_source_md(m.get("sources")), "",
          "| " + " | ".join(STATUS_ORDER) + " | Total |", "|" + "---|" * (len(STATUS_ORDER) + 1),
          "| " + " | ".join(str(s[k]) for k in STATUS_ORDER) + f" | {s['total']} |", "",
          "## Workflows", "", "| Workflow | App | Result | Approval | Risk | Writes | Reason / note |", "|---|---|---|---|---|---|---|"]
    for w in data["workflows"]:
        md.append(f"| `{w['id']}` | {w['app']} | **{w['status']}** | {w['approval_state']} | {w['risk']} | {w['writes']} | {(w['reason'] or '').replace('|', '/')[:160]} |")
    fails = [(w, st) for w in data["workflows"] for st in w["steps"] if st["status"] == "FAIL"]
    md += ["", "## Failures", ""]
    md += [f"- `{w['id']}` step {st['n']} ({st['action']}) at {st['timestamp']} - expected: {st['expected']}; error: {st['error']}" for w, st in fails] or ["None."]
    md += ["", "## Investigate these issues (hypotheses, not verified root causes)", ""]
    for f in data["findings"]:
        evidence = (f"; [view screenshot]({f['screenshot_link']})" if f.get("screenshot_link") else "")
        md += [f"- **{f['id']}** ({f['status']}, {f['severity']}; {f['count']} occurrence(s)): "
               f"{f['type']} - {f['summary']}. Next: {f['investigate']}{evidence}"]
        if f.get('ai_hypothesis_unverified'):
            md.append(f"  - Optional AI hypothesis (NOT VERIFIED): {f['ai_hypothesis_unverified']}")
    if not data["findings"]:
        md.append("None.")
    md += ["", "## Skipped / not run, with reasons", ""]
    md += [f"- `{x['id']}` - **{x['status']}** - {x['reason']}" for x in data["skipped"]] or ["None."]
    if inventory:
        md += ["", "## Workflow inventory", "", "| Workflow | App | Approval state | Last result | Risk | Writes | Source |", "|---|---|---|---|---|---|---|"]
        md += [f"| `{r['id']}` | {r['app']} | {r['approval']} | {r['last_result']} | {r['risk']} | {r['writes']} | {r['source']} |" for r in data["inventory"]]
    md_text = "\n".join(md) + "\n"

    # ---- HTML
    cards = "".join(f'<div class="card" style="border-top:4px solid {COLORS[k]}"><div class="n">{s[k]}</div>{k}</div>' for k in STATUS_ORDER)
    rows = []
    for w in data["workflows"]:
        rows.append(f'<tr><td><b>{_e(w["id"])}</b><br>{_e(w["title"])}</td><td>{_e(w["app"])}</td>'
                    f'<td style="color:{COLORS.get(w["status"], "#000")};font-weight:700">{_e(w["status"])}</td><td>{_e(w["approval_state"])}</td>'
                    f'<td>{_e(w["risk"])}</td><td>{_e(w["writes"])}</td><td>{_e(w["reason"])}</td></tr>')
        for st in w["steps"]:
            det = "".join(f"<br><i>console:</i> {_e(x)}" for x in st["console_errors"]) + "".join(f"<br><i>request:</i> {_e(x)}" for x in st["failed_requests"])
            rows.append(f'<tr class="step"><td colspan="2">&nbsp;&nbsp;#{st["n"]} {_e(st["action"])} <small>{_e(st["timestamp"])}</small></td>'
                        f'<td style="color:{COLORS.get(st["status"], "#000")}">{_e(st["status"])}</td><td colspan="4">{_e(st["expected"])} &rarr; {_e(st["actual"])}'
                        f'{"<br><b>" + _e(st["error"]) + "</b>" if st["error"] else ""}{det}'
                        f'{_screenshot_markup(st)}'
                        f'{"<br>" + _e(st.get("screenshot_note")) if st.get("screenshot_note") else ""}</td></tr>')
    finding_rows = []
    for f in data["findings"]:
        link = (f'<a href="{_e(f["screenshot_link"])}" target="_blank" rel="noopener">View masked screenshot</a>'
                if f.get("screenshot_link") else 'No screenshot (preflight or capture unavailable)')
        affected = ', '.join(_e(item['workflow']) + ' step ' + str(item['step']) for item in f['occurrences'][:12])
        extra = (f'<p><b>Optional AI hypothesis (unverified):</b> {_e(f["ai_hypothesis_unverified"])}</p>'
                 if f.get('ai_hypothesis_unverified') else '')
        finding_rows.append(f'<section class="finding"><h3>{_e(f["id"])} — {_e(f["type"])} '
                            f'<small>{_e(f["status"])} / {_e(f["severity"])} / {f["count"]} occurrence(s)</small></h3>'
                            f'<p><b>Observed:</b> {_e(f["summary"])}</p>'
                            f'<p><b>Expected:</b> {_e(f["expected"])}</p>'
                            f'<p><b>Investigation suggestion, not a verified cause:</b> {_e(f["investigate"])}</p>'
                            f'<p><b>Affected:</b> {affected}</p><p>{link}</p>{extra}</section>')
    inv_rows = "".join(f'<tr><td>{_e(r["id"])}</td><td>{_e(r["app"])}</td><td>{_e(r["approval"])}</td><td>{_e(r["last_result"])}</td><td>{_e(r["risk"])}</td>'
                       f'<td>{_e(r["writes"])}</td><td>{_e(r["source"])}</td><td>{_e(r["reason_not_run"])}</td></tr>' for r in data["inventory"])
    doc = (f'<!doctype html><html><head><meta charset="utf-8"><title>Test run {_e(run_id)}</title><style>'
           'body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2328}.cards{display:flex;gap:12px;margin:16px 0}'
           '.card{background:#f6f8fa;padding:12px 20px;border-radius:6px;text-align:center}.n{font-size:28px;font-weight:700}'
           'table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:32px}td,th{border:1px solid #d0d7de;padding:6px;vertical-align:top;word-break:break-word}'
           'th{background:#f6f8fa;text-align:left}.step td{background:#fcfcfd;font-size:12px}'
           '.shot{display:block;max-width:460px;max-height:260px;border:1px solid #d0d7de;margin-top:8px}'
           '.finding{border:1px solid #d0d7de;border-radius:6px;padding:10px 15px;margin:12px 0}.finding h3{margin:4px 0}</style></head><body>'
           f'<h1>Test run {_e(run_id)}</h1><p>Environment <b>{_e(m["environment"])}</b> ({_e(m["environment_kind"])}) &middot; mode <b>{_e(m["mode"])}</b> &middot; '
           f'{_e(m["started"])} &rarr; {_e(m.get("finished", ""))}<br>Browser {_e(m["browser_version"])} &middot; Python {_e(m["python"])} &middot; {_e(m["platform"])}{_source_html(m.get("sources"))}</p>'
           f'<div class="cards">{cards}</div><h2>Issues requiring investigation</h2>{"".join(finding_rows) or "<p>None.</p>"}<h2>Workflows</h2><table><tr><th>Workflow</th><th>App</th><th>Result</th><th>Approval</th><th>Risk</th><th>Writes</th><th>Reason</th></tr>{"".join(rows)}</table>'
           f'<h2>Workflow inventory</h2><table><tr><th>Id</th><th>App</th><th>Approval</th><th>Last result</th><th>Risk</th><th>Writes</th><th>Source</th><th>Not run because</th></tr>{inv_rows}</table>'
           '<p><small>PASS = assertions ran and held. FAIL = an assertion failed or an error occurred. BLOCKED = a safety/approval/credential gate refused the run or a feature was not found. '
           'NOT VERIFIED = ran but the result is not trustworthy. NOT TESTED = intentionally not executed (dry-run, read-only mode, draft, earlier failure).</small></p></body></html>')

    jtxt = json.dumps(data, indent=2, ensure_ascii=False)
    paths = {"json": out_dir / f"{base}.json", "md": out_dir / f"{base}.md", "html": out_dir / f"{base}.html"}
    paths["json"].write_text(jtxt, encoding="utf-8")
    paths["md"].write_text(md_text, encoding="utf-8")
    paths["html"].write_text(doc, encoding="utf-8")
    copied = []
    for rel, src in screenshot_sources.items():
        destination = out_dir / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Ignore non-PNG or unexpectedly large files to limit accidental artifact disclosure.
        if src.is_file() and src.stat().st_size <= 8_000_000 and src.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
            shutil.copyfile(src, destination)
            copied.append(rel)
    # Safe timeline: metadata only (no Playwright DOM snapshot or network-response bodies).
    timeline = redact_obj({"run_id": run_id, "workflows": [
        {"id": w['id'], "status": w['status'], "steps": [
            {k: st.get(k) for k in ('n', 'action', 'status', 'duration_s', 'timestamp', 'url', 'screenshot_link')}
            for st in w['steps']]} for w in data['workflows']]})
    assert_clean_obj(timeline, what='safe trace timeline')
    timeline_path = out_dir / f"agent_timeline_{run_id}.json"
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding='utf-8')
    paths['timeline_json'] = timeline_path
    if inventory:
        inv_md = ["# Workflow inventory", "", "| Workflow | App | Approval state | Last result | Risk | Writes | Source | Not run because |", "|---|---|---|---|---|---|---|---|"]
        inv_md += [f"| `{r['id']}` | {r['app']} | {r['approval']} | {r['last_result']} | {r['risk']} | {r['writes']} | {r['source']} | {(r['reason_not_run'] or '').replace('|', '/')} |" for r in data["inventory"]]
        p = out_dir / f"inventory_{run_id}.md"
        p.write_text("\n".join(inv_md) + "\n", encoding="utf-8")
        paths["inventory_md"] = p
    return {k: str(v) for k, v in paths.items()}
