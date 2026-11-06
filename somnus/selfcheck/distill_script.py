"""Distil a recorded trace into a reusable ``run`` script draft.

An agent's first pass through a flow costs one round trip per step: observe,
find, call, each with its own settle and its own model turn. The trace already
records every step that worked (ref, capability, args, the observation the
step ran against); the ``run`` tool already executes a whole script in one
call; the ``heal`` loop already re-locates steps a drifted UI breaks. The one
missing piece is deterministic: turn the trace into the script draft.

The draft is *ref-free on purpose*. Refs die with the session, so a script
that names ``p1`` replays against nothing. Every distilled target becomes an
``AWAIT TYPE "name"`` (the locator the step actually had when it worked)
followed by ``CALL $it.cap(args)``; consecutive steps on the same element
share one binding. Steps the trace cannot name honestly become ``# TODO``
comments rather than guesses:

* the ref is not in the step's own observation snapshot;
* the element has no name (an ``AWAIT`` needs one);
* the target is the window itself - ``run`` takes a ``window=`` argument
  instead of a ``CALL wN.focus()`` step.

The output is validated with the same parser the runtime uses
(:func:`somnus.sui.parse.parse_script`), so a draft that reaches a model is
guaranteed to parse; whether the names are unambiguous at replay time is what
``replay`` and ``heal`` are for.

Like the rest of :mod:`somnus.selfcheck`, this is host code: it reads traces
and emits text, and the runtime never imports it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from somnus.errors import ErrorCode, SomnusError
from somnus.observability.view import load_events, trace_path
from somnus.selfcheck.nodes import NodeLine, parse_observation, pick_window
from somnus.selfcheck.replay import _call_pairs, _snapshot_text
from somnus.sui.parse import MAX_SCRIPT_STEPS, parse_script

#: ``call.end`` statuses that mean the step did what it set out to do. A
#: failed attempt is experience, not a recipe: distilling it would replay the
#: failure. (The heal loop is where a *failing* script gets repaired.)
_DISTILLABLE_STATUS = ("OK", "OK_UNVERIFIED")

_POSITIONAL = re.compile(r"_\d+")


@dataclass(slots=True)
class DistillReport:
    """The draft plus the counts a reviewer needs to trust (or check) it."""

    script: str
    source: str
    window: str | None
    #: Successful ``call`` steps the trace offered.
    considered: int
    #: Steps that became ``AWAIT``/``CALL $it`` lines.
    distilled: int
    #: Steps that became ``# TODO`` comments.
    skipped: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "window": self.window,
            "considered": self.considered,
            "distilled": self.distilled,
            "skipped": self.skipped,
        }


def distill_script(
    path: str | Path, *, await_timeout: float = 0.0
) -> DistillReport:
    """Read the trace at *path* and return its ``run``-script draft.

    ``await_timeout`` becomes ``timeout=`` on every generated ``AWAIT``. The
    default ``0`` states what the trace actually proves: the element was
    there the moment the previous step settled, so one search is enough. A
    positive value makes the draft tolerate re-timing at the cost of waiting
    when the element never comes.
    """
    target = trace_path(path)
    root = target.parent
    events = load_events(target)

    steps: list[tuple[dict[str, Any], dict[str, Any], NodeLine | None]] = []
    window: str | None = None
    last_snapshot: str | None = None
    for start, end in _call_pairs(events):
        if str(start.get("tool") or "") != "call":
            continue
        if str(end.get("status") or "") not in _DISTILLABLE_STATUS:
            continue
        text = _snapshot_text(root, start.get("before")) or last_snapshot or ""
        last_snapshot = text or last_snapshot
        if window is None and text:
            picked = pick_window(text)
            if picked is not None:
                window = picked.name
        lines = parse_observation(text)
        node = next(
            (line for line in lines if line.ref == str(start.get("ref") or "")),
            None,
        )
        steps.append((start, end, node))

    body: list[str] = []
    binding: tuple[str, str] | None = None
    distilled = 0
    skipped = 0
    for start, _end, node in steps:
        ref = str(start.get("ref") or "")
        cap = str(start.get("cap") or "")
        args = dict(start.get("args") or {})
        if node is None or not node.name:
            reason = (
                "no observation recorded for this step"
                if node is None
                else "the element has no name; AWAIT needs one"
            )
            body.append(f"# TODO {ref}.{cap}({_render_args(args)}) - {reason}")
            binding = None
            skipped += 1
            continue
        if node.is_window:
            title = _quote(node.name) if node.name else '"<window title>"'
            body.append(
                f"# TODO {ref}.{cap}() is window-level; pass window={title} "
                "to run instead"
            )
            binding = None
            skipped += 1
            continue
        if binding != (node.kind, node.name):
            body.append(
                f"AWAIT {node.kind} {_quote(node.name)} timeout="
                f"{_render_timeout(await_timeout)}"
            )
            binding = (node.kind, node.name)
        body.append(f"CALL $it.{cap}({_render_args(args)})")
        distilled += 1

    if not body:
        raise SomnusError(
            ErrorCode.INVALID_ARGUMENT,
            message=(
                "the trace holds no successful call step to distil "
                "(record one with --trace and make at least one CALL)"
            ),
        )
    if distilled + skipped > MAX_SCRIPT_STEPS:
        raise SomnusError(
            ErrorCode.INVALID_ARGUMENT,
            message=(
                f"the flow has {distilled + skipped} steps; a run script takes "
                f"at most {MAX_SCRIPT_STEPS} - distil it in parts"
            ),
        )

    header = [
        f"# distilled from trace {Path(target).as_posix()}",
        f"# steps: {distilled} distilled, {skipped} need review",
        "# AWAIT binds the first name match; check the names are unambiguous.",
    ]
    if window:
        header.append(
            f"# recorded window: {_quote(window)} - pass window={_quote(window)} to run"
        )
    script = "\n".join(header + body) + "\n"

    # The draft must parse with the grammar the runtime will run. This is a
    # self-check on the generator (quoting, arg rendering), not on the UI:
    # whether the names still resolve at replay time is replay/heal's job. A
    # draft with no distilled step at all is a review checklist, not a script,
    # and parses to nothing by design.
    if distilled:
        try:
            parse_script(script)
        except SomnusError as exc:
            raise SomnusError(
                ErrorCode.PARSE_ERROR,
                message=f"the distilled draft does not parse ({exc.message})",
            ) from exc

    return DistillReport(
        script=script,
        source=str(target),
        window=window,
        considered=len(steps),
        distilled=distilled,
        skipped=skipped,
    )


def _render_args(args: dict[str, Any]) -> str:
    """Render a trace's args dict back into script form.

    Positional arguments (``_0``, ``_1``, ...) come first in their recorded
    order, then named ones in insertion order - the same text a model would
    have sent, modulo the runtime's own normalisation.
    """
    if not args:
        return ""
    positional = sorted(
        (key for key in args if _POSITIONAL.fullmatch(key)),
        key=lambda key: int(key[1:]),
    )
    parts = [_render_value(args[key]) for key in positional]
    for key, value in args.items():
        if _POSITIONAL.fullmatch(key):
            continue
        parts.append(f"{key}={_render_value(value)}")
    return ", ".join(parts)


def _render_value(value: Any) -> str:
    """One argument value: the literals :func:`somnus.sui.parse._parse_value` reads."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return _quote(str(value))


def _render_timeout(seconds: float) -> str:
    """``timeout=`` rendering: whole seconds stay integers (``0``, not ``0.0``)."""
    value = float(seconds)
    return str(int(value)) if value.is_integer() else repr(value)


def _quote(text: str) -> str:
    """Quote a name or value with the two escapes the grammar decodes."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
