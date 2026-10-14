"""Step recorder: every check becomes one row with evidence. Nothing is PASS unless its asserts ran."""
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from config import SHOTS, RUN_ID
from utils.redact import redact


class Blocked(Exception):
    """Feature cannot be tested (missing credentials, feature not present, write disabled...)."""


class NotTested(Exception):
    """Deliberately not executed (e.g. unsafe or expected behaviour unknown)."""


class NotVerified(Exception):
    """The check ran but its result is unreliable, so we cannot call it PASS or FAIL.
    Needs a human look (or a better assertion) - never report it as a website bug."""


@dataclass
class Step:
    test: str
    step_no: int
    name: str
    role: str
    page_title: str
    url: str
    expected: str
    actual: str
    status: str
    severity: str
    duration_s: float
    confidence: str = ""
    screenshot: str = ""
    error: str = ""
    console_errors: list = field(default_factory=list)
    failed_requests: list = field(default_factory=list)
    timestamp: str = ""


class StepCtx:
    def __init__(self, mon):
        self.actual = ""
        self.screenshot = ""      # set this to attach a screenshot you took yourself
        self.confidence = ""      # "High" / "Medium" / "Low" - how sure are we a FAIL is a real defect
        self.mon = mon
        self._c = len(mon.console) if mon else 0
        self._f = len(mon.failed) if mon else 0

    def new_console(self):
        return list(self.mon.console[self._c:]) if self.mon else []

    def new_failed(self):
        return list(self.mon.failed[self._f:]) if self.mon else []


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", s)[:60]


class Recorder:
    def __init__(self):
        self.steps: list[Step] = []
        self._n: dict[str, int] = {}

    @contextmanager
    def step(self, test, name, role, expected, page=None, mon=None, severity="Medium", shot=False):
        n = self._n[test] = self._n.get(test, 0) + 1
        ctx = StepCtx(mon)
        t0 = time.time()
        status, error = "PASS", ""
        try:
            yield ctx
        except Blocked as e:
            status, error = "BLOCKED", str(e)
        except NotTested as e:
            status, error = "NOT TESTED", str(e)
        except NotVerified as e:
            status, error = "NOT VERIFIED", str(e)
        except AssertionError as e:
            status, error = "FAIL", str(e) or "Assertion failed"
        except Exception as e:  # playwright timeouts etc.
            first = str(e).splitlines()[0][:300] if str(e) else ""
            status, error = "FAIL", f"{type(e).__name__}: {first}"
        dur = round(time.time() - t0, 2)

        title = url = ""
        shot_path = ctx.screenshot
        if page is not None:
            try:
                url, title = page.url, page.title()
            except Exception:
                pass
            if not shot_path and (status in ("FAIL", "NOT VERIFIED") or shot):
                try:
                    p = SHOTS / f"{RUN_ID}_{_safe(test)}_{n:02d}_{status}.png"
                    page.screenshot(path=str(p))
                    shot_path = str(p)
                except Exception:
                    pass

        actual = ctx.actual or (error if status != "PASS" else "Expected result verified by assertions")
        self.steps.append(Step(
            test=test, step_no=n, name=name, role=role, page_title=redact(title), url=redact(url),
            expected=expected, actual=redact(actual), status=status,
            severity=severity if status == "FAIL" else "",
            duration_s=dur, confidence=ctx.confidence if status == "FAIL" else "", screenshot=shot_path, error=redact(error),
            console_errors=[redact(x) for x in ctx.new_console()[:10]],
            failed_requests=[redact(x) for x in ctx.new_failed()[:10]],
            timestamp=datetime.now().isoformat(timespec="seconds"),
        ))

    def add(self, test, name, role, expected, status, reason, severity="Medium"):
        """Record a step without running anything (e.g. whole role blocked)."""
        n = self._n[test] = self._n.get(test, 0) + 1
        self.steps.append(Step(test=test, step_no=n, name=name, role=role, page_title="", url="",
                               expected=expected, actual=redact(reason), status=status,
                               severity=severity if status == "FAIL" else "", duration_s=0.0,
                               error=redact(reason), timestamp=datetime.now().isoformat(timespec="seconds")))

    def summary(self):
        out = {"total": len(self.steps), "PASS": 0, "FAIL": 0, "BLOCKED": 0, "NOT VERIFIED": 0, "NOT TESTED": 0}
        for s in self.steps:
            out[s.status] += 1
        return out

    def failures(self):
        return [s for s in self.steps if s.status == "FAIL"]


REC = Recorder()
