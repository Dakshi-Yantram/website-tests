"""Writes JSON + HTML reports from the recorder."""
import json
import html
import os
from dataclasses import asdict
from datetime import datetime

from config import REPORTS, RUN_ID, APPS
from utils.recorder import REC

SEV_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "": 4}
COLORS = {"PASS": "#1a7f37", "FAIL": "#cf222e", "BLOCKED": "#9a6700", "NOT VERIFIED": "#8250df", "NOT TESTED": "#57606a"}


def _e(x):
    return html.escape(str(x))


def _conf(step):
    return f" (confidence: {_e(step.confidence)})" if step.confidence else ""


def write_reports(rec=REC):
    summary = rec.summary()
    data = {
        "run_id": RUN_ID,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "targets": {k: a.base for k, a in APPS.items()},
        "summary": summary,
        "steps": [asdict(s) for s in rec.steps],
    }
    jpath = REPORTS / f"report_{RUN_ID}.json"
    jpath.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def shot_link(p):
        if not p:
            return ""
        rel = os.path.relpath(p, REPORTS).replace("\\", "/")
        return f'<a href="{_e(rel)}" target="_blank">screenshot</a>'

    bugs = sorted(rec.failures(), key=lambda s: (SEV_ORDER.get(s.severity, 4), s.test))
    bug_rows = "".join(
        f"<tr><td>{i}</td><td><b>{_e(b.severity)}</b>{_conf(b)}</td><td>{_e(b.role)}</td><td>{_e(b.test)} #{b.step_no}: {_e(b.name)}</td>"
        f"<td>{_e(b.url)}</td><td>Expected: {_e(b.expected)}<br>Actual: {_e(b.actual)}</td><td>{shot_link(b.screenshot)}</td></tr>"
        for i, b in enumerate(bugs, 1)
    ) or '<tr><td colspan="7">No failures recorded.</td></tr>'

    def details(s):
        parts = []
        if s.console_errors:
            parts.append("<b>Console:</b><br>" + "<br>".join(_e(x) for x in s.console_errors))
        if s.failed_requests:
            parts.append("<b>Failed requests:</b><br>" + "<br>".join(_e(x) for x in s.failed_requests))
        if s.error and s.status != "PASS":
            parts.append("<b>Error:</b> " + _e(s.error))
        return "<br>".join(parts)

    rows = "".join(
        f'<tr class="r-{s.status.replace(" ", "")}"><td>{_e(s.test)} #{s.step_no}</td><td>{_e(s.name)}</td><td>{_e(s.role)}</td>'
        f'<td>{_e(s.url)}</td><td>{_e(s.expected)}</td><td>{_e(s.actual)}</td>'
        f'<td style="color:{COLORS[s.status]};font-weight:700">{s.status}</td><td>{_e(s.severity)}</td>'
        f'<td>{s.duration_s}s</td><td>{shot_link(s.screenshot)}</td><td>{details(s)}</td></tr>'
        for s in rec.steps
    )
    cards = "".join(
        f'<div class="card" style="border-top:4px solid {COLORS.get(k, "#0969da")}"><div class="n">{v}</div><div>{k}</div></div>'
        for k, v in summary.items()
    )
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><title>Test report {RUN_ID}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2328}}
.cards{{display:flex;gap:12px;margin:16px 0}}.card{{background:#f6f8fa;padding:12px 20px;border-radius:6px;text-align:center}}
.n{{font-size:28px;font-weight:700}}table{{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:32px}}
td,th{{border:1px solid #d0d7de;padding:6px;vertical-align:top;word-break:break-word}}th{{background:#f6f8fa;text-align:left}}</style></head><body>
<h1>Website Test Report</h1><p>Run {RUN_ID} &middot; {data['generated']}</p>
<div class="cards">{cards}</div>
<h2>Prioritised bug list</h2>
<table><tr><th>#</th><th>Severity</th><th>Role</th><th>Test / step</th><th>URL</th><th>Expected vs actual</th><th>Evidence</th></tr>{bug_rows}</table>
<h2>All steps</h2>
<table><tr><th>Test</th><th>Step</th><th>Role</th><th>URL</th><th>Expected</th><th>Actual</th><th>Status</th><th>Severity</th><th>Time</th><th>Evidence</th><th>Logs</th></tr>{rows}</table>
</body></html>"""
    hpath = REPORTS / f"report_{RUN_ID}.html"
    hpath.write_text(doc, encoding="utf-8")
    return hpath, jpath
