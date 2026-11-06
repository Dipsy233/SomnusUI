"""``somnus eval``: run the generic self-check and judge it from runtime state.

This is the model-free half of the evaluation story. It does not ask a language
model whether the screen was handled; it runs observe, find and call itself and
reads the session's answers. A model-in-the-loop evaluation is a different
exercise (``examples/eval_agent.py``) and is not part of this command.

The report is JSON so a script, a CI job or the agent itself can parse it. It
names the desktop the check ran on, the provider behind it and the trace that
holds the evidence.
"""

from __future__ import annotations

import json
import time
from typing import Any

from somnus.selfcheck.checks import CheckReport, run_self_checks


def run(session, *, desktop: str = "") -> CheckReport:
    """Run the built-in tasks against the session's current desktop."""
    return run_self_checks(session, desktop=desktop)


def as_report(report: CheckReport, *, trace: str | None = None) -> dict[str, Any]:
    data = report.as_dict()
    data["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    data["trace"] = trace
    return data


def render_json(report: CheckReport, *, trace: str | None = None) -> str:
    return json.dumps(
        as_report(report, trace=trace),
        ensure_ascii=False,
        indent=2,
        default=str,
    )
