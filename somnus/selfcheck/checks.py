"""The model-free self-check: observe, find, call - judged from runtime state.

Each task uses the same public methods a model's tool calls reach, in the same
order, and then checks the *runtime's* answer rather than the step's own claim:

* ``observe`` passes when the session compiled a result for a real window;
* ``find`` passes when the session's own result contains the ref the reply
  printed;
* ``call`` passes when the call outcome is an ``OK`` variant.

No task needs a particular application, control name or task script. The
target is whatever the observed desktop offers: the first named, actionable
node, or the window itself when the screen is bare. That keeps the check
generic and keeps it useful on unknown applications.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from somnus.ir.action import Action
from somnus.selfcheck import nodes


@dataclass(slots=True)
class CheckResult:
    name: str
    ok: bool
    ms: float
    detail: str = ""
    skipped: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "task": self.name,
            "ok": self.ok,
            "skipped": self.skipped,
            "ms": round(self.ms, 1),
            "detail": self.detail,
        }


@dataclass(slots=True)
class CheckReport:
    desktop: str
    provider: str
    session: str
    tasks: list[CheckResult] = field(default_factory=list)
    observation: str = ""
    find_reply: str = ""
    call_reply: str = ""
    duration_ms: float = 0.0

    @property
    def considered(self) -> list[CheckResult]:
        return [task for task in self.tasks if not task.skipped]

    @property
    def passed(self) -> int:
        return sum(1 for task in self.considered if task.ok)

    @property
    def failed(self) -> int:
        return sum(1 for task in self.considered if not task.ok)

    @property
    def ok(self) -> bool:
        return self.failed == 0 and bool(self.considered)

    def as_dict(self) -> dict[str, Any]:
        return {
            "desktop": self.desktop,
            "provider": self.provider,
            "session": self.session,
            "ok": self.ok,
            "passed": self.passed,
            "failed": self.failed,
            "duration_ms": round(self.duration_ms, 1),
            "tasks": [task.as_dict() for task in self.tasks],
            "observation": _clip(self.observation, 2400),
            "find": _clip(self.find_reply, 800),
            "call": _clip(self.call_reply, 800),
        }


def run_self_checks(session, *, desktop: str = "", provider: str = "") -> CheckReport:
    """Run the three generic tasks against the session's current desktop."""
    started = time.perf_counter()
    report = CheckReport(
        desktop=desktop or session.session_label,
        provider=provider or str(getattr(session.provider, "name", "unknown")),
        session=session.session_label,
    )

    window_ref: str | None = None
    observation_text = ""
    task_started = time.perf_counter()
    try:
        windows_text = session.observe(windows_only=True).text
        window = nodes.pick_window(windows_text)
        if window is None:
            raise RuntimeError("no top-level window was enumerated")
        window_ref = window.ref
        observation = session.observe(window_ref)
        observation_text = observation.text
        report.observation = observation_text
        ok = (
            session.result is not None
            and observation.rev >= 1
            and observation_text.startswith("@rev")
        )
        detail = f"rev={observation.rev} tokens={observation.tokens}"
        if not ok:
            detail = "observation did not compile a result"
        report.tasks.append(
            CheckResult("observe", ok, (time.perf_counter() - task_started) * 1000, detail)
        )
    except Exception as exc:  # noqa: BLE001 - a check records failure, it does not raise
        report.tasks.append(
            CheckResult(
                "observe",
                False,
                (time.perf_counter() - task_started) * 1000,
                f"{type(exc).__name__}: {exc}",
            )
        )
        report.duration_ms = (time.perf_counter() - started) * 1000
        return report

    # ---------------------------------------------------------------- find
    task_started = time.perf_counter()
    target = nodes.pick_actionable(observation_text)
    found_ref: str | None = None
    if target is None or target.name is None:
        report.tasks.append(
            CheckResult(
                "find",
                True,
                (time.perf_counter() - task_started) * 1000,
                "skipped: the screen printed no named node",
                skipped=True,
            )
        )
    else:
        try:
            reply = session.find(
                Action(
                    kind="find",
                    query_type=target.kind.lower(),
                    query=target.name,
                )
            )
            hits = nodes.parse_found(reply)
            if not hits:
                # The rendered type is presentation vocabulary; a check must
                # not fail because a control's role differs from the word printed
                # for it. Retry by name alone, which is what a model would do.
                reply = session.find(Action(kind="find", query=target.name))
                hits = nodes.parse_found(reply)
            report.find_reply = reply
            found_ref = hits[0].ref if hits else None
            ok = bool(found_ref) and (
                session.result is not None
                and session.result.find(found_ref) is not None
            )
            detail = (
                f'{target.kind} "{target.name}" -> {len(hits)} hit(s)'
                if hits
                else reply.splitlines()[0]
            )
            report.tasks.append(
                CheckResult("find", ok, (time.perf_counter() - task_started) * 1000, detail)
            )
        except Exception as exc:  # noqa: BLE001
            report.tasks.append(
                CheckResult(
                    "find",
                    False,
                    (time.perf_counter() - task_started) * 1000,
                    f"{type(exc).__name__}: {exc}",
                )
            )

    # ---------------------------------------------------------------- call
    task_started = time.perf_counter()
    ref = found_ref or window_ref
    try:
        outcome = session.call(
            Action(kind="call", ref=ref, capability="focus")
        )
        report.call_reply = outcome.text
        ok = str(outcome.status).startswith("OK")
        first_line = outcome.text.splitlines()[0] if outcome.text else outcome.status
        report.tasks.append(
            CheckResult(
                "call",
                ok,
                (time.perf_counter() - task_started) * 1000,
                f"{ref}.focus() -> {first_line}",
            )
        )
    except Exception as exc:  # noqa: BLE001
        report.tasks.append(
            CheckResult(
                "call",
                False,
                (time.perf_counter() - task_started) * 1000,
                f"{ref}.focus() -> {type(exc).__name__}: {exc}",
            )
        )

    report.duration_ms = (time.perf_counter() - started) * 1000
    return report


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
