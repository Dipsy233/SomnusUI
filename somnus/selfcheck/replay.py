"""Replay a recorded decision trace and name the first step that diverges.

A trace stores what a call *was* (ref, capability, arguments), what it did
(status, error code, delta counts) and the SUI-Text the model saw before it ran.
Refs are only valid inside the session that printed them, so a replay cannot
reuse them. It resolves each one from the before-snapshot instead: type plus
name (plus document order for same-named rows), then ``FIND`` on the live desktop
to get the ref this run assigned. Window refs resolve by title.

The report answers the question the trace exists for: *does the same action still
land?* A mismatch in status, error code or delta counts is the first divergence
and stops the comparison, because later steps then run against a different state
and would only pile up noise.

This module stays host-side: it drives a normal ``Session`` and never reaches
into the runtime's private call path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from somnus.errors import SomnusError
from somnus.ir.action import Action
from somnus.observability.view import load_events, trace_path
from somnus.selfcheck import nodes


@dataclass(slots=True)
class ReplayStep:
    index: int
    recorded_ref: str
    resolved_ref: str
    capability: str
    expected_status: str
    actual_status: str
    resolved: bool
    output: str = ""
    divergence: str | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.index,
            "ref": self.recorded_ref,
            "resolved": self.resolved_ref,
            "cap": self.capability,
            "expected": self.expected_status,
            "actual": self.actual_status,
            "resolved_by_name": self.resolved,
            "divergence": self.divergence,
            "detail": self.detail,
            "output": self.output,
        }


@dataclass(slots=True)
class ReplayReport:
    trace: str
    desktop: str
    provider: str
    steps: list[ReplayStep] = field(default_factory=list)
    duration_ms: float = 0.0
    trace_out: str | None = None

    @property
    def diverged(self) -> ReplayStep | None:
        return next((step for step in self.steps if step.divergence), None)

    @property
    def ok(self) -> bool:
        return self.diverged is None

    def as_dict(self) -> dict[str, Any]:
        first = self.diverged
        return {
            "trace": self.trace,
            "desktop": self.desktop,
            "provider": self.provider,
            "ok": self.ok,
            "steps": len(self.steps),
            "first_divergence": first.as_dict() if first else None,
            "duration_ms": round(self.duration_ms, 1),
            "trace_out": self.trace_out,
            "step_reports": [step.as_dict() for step in self.steps],
        }


def replay_trace(
    session,
    path: str | Path,
    *,
    desktop: str = "",
    provider: str = "",
    trace_out: str | None = None,
) -> ReplayReport:
    """Drive *session* through every ``call.start`` in a trace."""
    started = time.perf_counter()
    target = trace_path(path)
    root = target.parent
    events = load_events(target)
    report = ReplayReport(
        trace=str(target),
        desktop=desktop or session.session_label,
        provider=provider or str(getattr(session.provider, "name", "unknown")),
        trace_out=trace_out,
    )

    pairs = _call_pairs(events)
    deltas = _delta_counts(events)
    last_snapshot: str | None = None

    for index, (start, end) in enumerate(pairs, start=1):
        before = _snapshot_text(root, start.get("before")) or last_snapshot or ""
        lines = nodes.parse_observation(before)
        header = nodes.pick_window(before)
        title = header.name if header else None
        has_expectation = bool(end)
        expected_status = str(end.get("status") or "?")
        expected_error = end.get("error_code")

        step = ReplayStep(
            index=index,
            recorded_ref=str(start.get("ref") or ""),
            resolved_ref=str(start.get("ref") or ""),
            capability=str(start.get("cap") or ""),
            expected_status=expected_status,
            actual_status="?",
            resolved=False,
        )

        window_error = _reach_window(session, title)
        resolved, resolved_by_name = _resolve_ref(
            session, lines, step.recorded_ref, title
        )
        step.resolved_ref = resolved
        step.resolved = resolved_by_name

        if window_error:
            step.actual_status = "ERR"
            step.divergence = "window"
            step.detail = window_error
            report.steps.append(step)
            break

        action = Action(
            kind="call",
            ref=resolved,
            capability=step.capability,
            args=dict(start.get("args") or {}),
        )
        try:
            outcome = session.call(action)
            step.actual_status = str(outcome.status)
            step.output = outcome.text.splitlines()[0] if outcome.text else ""
            actual_error = None
            actual_delta = _delta_of(outcome)
        except SomnusError as err:
            step.actual_status = "ERR"
            step.output = err.render()
            actual_error = str(err.code)
            actual_delta = None
        except Exception as exc:  # noqa: BLE001 - a replay reports, it does not raise
            step.actual_status = "ERR"
            step.output = f"{type(exc).__name__}: {exc}"
            actual_error = "INTERNAL"
            actual_delta = None

        expected_delta = deltas.get(str(start.get("call_id") or ""))
        if has_expectation and step.actual_status != expected_status:
            step.divergence = "status"
            step.detail = f"expected={expected_status} actual={step.actual_status}"
        elif has_expectation and expected_error and actual_error != expected_error:
            step.divergence = "error"
            step.detail = f"expected={expected_error} actual={actual_error or '-'}"
        elif expected_delta and actual_delta and expected_delta != actual_delta:
            step.divergence = "delta"
            step.detail = f"expected={expected_delta} actual={actual_delta}"

        report.steps.append(step)
        after = _snapshot_text(root, end.get("after"))
        if after:
            last_snapshot = after
        if step.divergence:
            break

    report.duration_ms = (time.perf_counter() - started) * 1000
    return report


# ---------------------------------------------------------------------------
# trace reading
# ---------------------------------------------------------------------------


def _call_pairs(events: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """``(call.start, call.end)`` pairs, in order; unmatched starts kept."""
    pending: dict[str, dict[str, Any]] = {}
    unmatched: list[dict[str, Any]] = []
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    last_start: dict[str, Any] | None = None

    for record in events:
        name = record.get("event")
        if name == "call.start":
            call_id = str(record.get("call_id") or "")
            if call_id:
                pending[call_id] = record
            else:
                last_start = record
        elif name == "call.end":
            call_id = str(record.get("call_id") or "")
            start = pending.pop(call_id, None) if call_id else None
            if start is None:
                start = last_start
                last_start = None
            if start is not None:
                pairs.append((start, record))

    unmatched.extend(pending.values())
    if last_start is not None:
        unmatched.append(last_start)
    pairs.extend((record, {}) for record in unmatched)
    return pairs


def _delta_counts(events: list[dict[str, Any]]) -> dict[str, tuple[int, int, int, int]]:
    """Per call id, the last reported ``(changed, added, removed, folded)``."""
    out: dict[str, tuple[int, int, int, int]] = {}
    for record in events:
        if record.get("event") != "delta":
            continue
        call_id = str(record.get("call_id") or "")
        if not call_id:
            continue
        out[call_id] = (
            int(record.get("changed") or 0),
            int(record.get("added") or 0),
            int(record.get("removed") or 0),
            int(record.get("folded") or 0),
        )
    return out


def _delta_of(outcome) -> tuple[int, int, int, int] | None:
    delta = getattr(outcome, "delta", None)
    if delta is None:
        return None
    return (
        len(delta.changed),
        len(delta.added),
        len(delta.removed),
        int(getattr(delta, "removed_folded", 0)),
    )


def _snapshot_text(root: Path, reference: Any) -> str | None:
    if not isinstance(reference, str) or not reference:
        return None
    try:
        return (root / reference).read_text(encoding="utf-8")
    except OSError:
        return None


# ---------------------------------------------------------------------------
# ref resolution
# ---------------------------------------------------------------------------


def _reach_window(session, title: str | None) -> str | None:
    """Observe the window the trace was talking about; return an error or None."""
    try:
        windows_text = session.observe(windows_only=True).text
    except Exception as exc:  # noqa: BLE001
        return f"cannot enumerate windows: {type(exc).__name__}: {exc}"
    window = None
    if title:
        window = next(
            (
                line
                for line in nodes.parse_observation(windows_text)
                if line.is_window and line.name == title
            ),
            None,
        )
        if window is None:
            return f"window {title!r} is not on this desktop"
    if window is None:
        window = nodes.pick_window(windows_text)
    if window is None:
        return "no top-level window on this desktop"
    try:
        session.observe(window.ref)
    except Exception as exc:  # noqa: BLE001
        return f"cannot observe window {window.ref}: {type(exc).__name__}: {exc}"
    return None


def _resolve_ref(
    session,
    before_lines: list[nodes.NodeLine],
    recorded_ref: str,
    title: str | None,
) -> tuple[str, bool]:
    """The ref this run's desktop assigns to the recorded element.

    Returns ``(ref, resolved_by_name)``. When the before-snapshot carries no
    identity for the ref (a trace recorded before headers existed, or a missing
    snapshot), the recorded ref is attempted unchanged - it may still resolve,
    and if it does not the call's error is the divergence evidence.
    """
    line = next((item for item in before_lines if item.ref == recorded_ref), None)
    if line is None:
        return recorded_ref, False

    if line.is_window:
        if title is None or line.name != title:
            title = line.name
        window_text = session.observe(windows_only=True).text
        match = next(
            (
                item
                for item in nodes.parse_observation(window_text)
                if item.is_window and item.name == title
            ),
            None,
        )
        return (match.ref, True) if match is not None else (recorded_ref, False)

    if not line.name:
        return recorded_ref, False

    try:
        reply = session.find(
            Action(kind="find", query_type=line.kind.lower(), query=line.name)
        )
    except Exception:  # noqa: BLE001 - an unresolvable ref is the step's own failure
        return recorded_ref, False
    hits = nodes.parse_found(reply)
    if not hits:
        return recorded_ref, False
    index = min(nodes.ordinal_of(before_lines, line), len(hits) - 1)
    return hits[index].ref, True
