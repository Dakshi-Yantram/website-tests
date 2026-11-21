"""PASS/FAIL reports: JSON, Markdown, HTML and JUnit XML. Everything is redacted first and re-checked; if anything
sensitive-looking survives, NOTHING is written."""
import html
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

from .errors import SensitiveDataError
from .redaction import assert_clean_obj, redact_obj

ORDER = ("PASS", "FAIL", "BLOCKED", "NOT TESTED")
COLORS = {"PASS": "#1a7f37", "FAIL": "#cf222e", "BLOCKED": "#9a6700", "NOT TESTED": "#57606a"}
SEV_RANK = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}


def summarize(results) -> dict:
    out = {s: 0 for s in ORDER}
    for r in results:
        out[r.status] = out.get(r.status, 0) + 1
    out["total"] = len(results)
    return out


def build(results, meta: dict) -> dict:
    return {"meta": meta, "summary": summarize(results), "tests": [asdict(r) for r in results]}


def write_reports(results, meta: dict, out_dir: Path) -> dict:
    """Returns {"json": path, "md": path, "html": path, "junit": path}. Raises SensitiveDataError (nothing written) on a leak."""
    data = redact_obj(build(results, meta))
    assert_clean_obj(data, what="login report")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"json": out / "login_report.json", "md": out / "login_report.md", "html": out / "login_report.html",
             "junit": out / "junit.xml"}
    paths["json"].write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    paths["md"].write_text(_markdown(data), encoding="utf-8")
    paths["html"].write_text(_html(data), encoding="utf-8")
    _junit(data, paths["junit"])
    return {k: str(v) for k, v in paths.items()}


def _markdown(d) -> str:
    m, s = d["meta"], d["summary"]
    lines = [f"# NurseConnect login test report", "",
             f"- Environment: **{m['environment']}** ({m['env_kind']})  -  target `{m['base_origin']}`",
             f"- Run: `{m['run_id']}`  -  {m['started']}  -  agent {m['agent_version']}",
             f"- Result: " + ", ".join(f"**{s[k]} {k}**" for k in ORDER if s.get(k)) + f" of {s['total']} test(s)", "",
             "| Test | Severity | Result | Seconds | Why / evidence |", "|---|---|---|---|---|"]
    for t in d["tests"]:
        why = (t["reason"] or "").replace("|", "/").replace("\n", " ")
        shot = "  ".join(f"[screenshot]({p})" for p in t["screenshots"])
        lines.append(f"| {t['id']} {t['title']} | {t['severity']} | {t['status']} | {t['duration_ms'] / 1000:.1f} | {why} {shot} |")
    fails = [t for t in d["tests"] if t["status"] == "FAIL"]
    if fails:
        lines += ["", "## Failures"]
        for t in fails:
            lines += ["", f"### {t['id']} - {t['title']}", f"Severity: {t['severity']}  -  {t['reason']}", ""]
            lines += [f"{st['n']}. `{st['action']}` - **{st['status']}** {st['detail']}" for st in t["steps"]]
            lines += [f"- screenshot: {p}" for p in t["screenshots"]]
    notes = [(t["id"], n) for t in d["tests"] for n in t["notes"]]
    if notes:
        lines += ["", "## Notes"] + [f"- {i}: {n}" for i, n in notes]
    lines += ["", "Statuses: PASS = assertions ran and held; FAIL = an assertion did not hold; BLOCKED = could not run "
              "(site unreachable, credentials missing); NOT TESTED = intentionally skipped (e.g. credential tests on production).", ""]
    return "\n".join(lines)


def _html(d) -> str:
    e = html.escape
    m, s = d["meta"], d["summary"]
    rows = []
    for t in d["tests"]:
        c = COLORS.get(t["status"], "#57606a")
        steps = "".join(f"<li><code>{e(st['action'])}</code> <b style='color:{COLORS.get(st['status'], '#57606a')}'>{e(st['status'])}</b> {e(st['detail'])}</li>"
                        for st in t["steps"])
        items = "".join(f"<li>{e(i['item'])}: <b style='color:{COLORS.get(i['status'], '#57606a')}'>{e(i['status'])}</b> {e(i['reason'])}</li>" for i in t["items"])
        shots = "".join(f"<a href='{e(p)}'><img src='{e(p)}' alt='screenshot {e(t['id'])}' style='max-width:420px;border:1px solid #ccc'></a>" for p in t["screenshots"])
        notes = "".join(f"<li>{e(n)}</li>" for n in t["notes"])
        rows.append(f"<details {'open' if t['status'] == 'FAIL' else ''}><summary><b style='color:{c}'>{e(t['status'])}</b> "
                    f"{e(t['id'])} - {e(t['title'])} <small>({e(t['severity'])}, {t['duration_ms'] / 1000:.1f}s)</small></summary>"
                    f"<p>{e(t['reason'])}</p>{('<ol>' + steps + '</ol>') if steps else ''}{('<ul>' + items + '</ul>') if items else ''}"
                    f"{('<ul>' + notes + '</ul>') if notes else ''}{shots}</details>")
    chips = " ".join(f"<span style='background:{COLORS[k]};color:#fff;padding:2px 10px;border-radius:10px'>{s[k]} {k}</span>" for k in ORDER if s.get(k))
    return ("<!doctype html><meta charset='utf-8'><title>NurseConnect login test report</title>"
            "<body style='font-family:system-ui,Segoe UI,sans-serif;max-width:980px;margin:24px auto;line-height:1.45'>"
            f"<h1>NurseConnect login test report</h1><p>{chips}</p>"
            f"<p>Environment <b>{e(m['environment'])}</b> ({e(m['env_kind'])}) - target <code>{e(m['base_origin'])}</code> - run <code>{e(m['run_id'])}</code> - {e(m['started'])}</p>"
            + "".join(rows) + "</body>")


def _junit(d, path: Path):
    s = d["summary"]
    suite = ET.Element("testsuite", name=d["meta"]["suite"], tests=str(s["total"]), failures=str(s["FAIL"]),
                       skipped=str(s["BLOCKED"] + s["NOT TESTED"]), time=f"{sum(t['duration_ms'] for t in d['tests']) / 1000:.2f}")
    for t in d["tests"]:
        tc = ET.SubElement(suite, "testcase", classname=d["meta"]["suite"], name=f"{t['id']} {t['title']}", time=f"{t['duration_ms'] / 1000:.2f}")
        if t["status"] == "FAIL":
            ET.SubElement(tc, "failure", message=t["reason"][:300]).text = "\n".join(f"{x['n']}. {x['action']} {x['status']} {x['detail']}" for x in t["steps"])
        elif t["status"] in ("BLOCKED", "NOT TESTED"):
            ET.SubElement(tc, "skipped", message=f"{t['status']}: {t['reason']}"[:300])
        if t["screenshots"]:
            ET.SubElement(tc, "system-out").text = "screenshot: " + ", ".join(t["screenshots"])
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


def exit_code(results, fail_on="any", strict=False) -> int:
    floor = 1 if fail_on == "any" else SEV_RANK[fail_on.capitalize()]
    if any(r.status == "FAIL" and SEV_RANK[r.severity] >= floor for r in results):
        return 1
    if strict and any(r.status == "BLOCKED" for r in results):
        return 1
    return 0
