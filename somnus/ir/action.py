"""Action / result / event value objects used across the runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ActionKind = Literal["call", "find", "inspect", "observe", "unknown", "await", "each", "end"]


@dataclass(slots=True)
class Action:
    """A parsed, validated LLM request.

    ``kind="await"`` is a script's waiting step: ``query_type`` fills the
    optional TYPE, ``query`` the name, ``scope`` the optional ``in REF`` and
    ``timeout`` the optional wait in seconds (``None`` = the runtime default).

    ``kind="each"`` opens a script loop block (query fields as ``await``,
    plus ``max_iter``); the matching ``kind="end"`` step closes it.
    """

    kind: ActionKind
    ref: str | None = None
    capability: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    #: find/inspect/observe payloads.
    query: str | None = None
    query_type: str | None = None
    scope: str | None = None
    depth: int | None = None
    range: tuple[int, int] | None = None
    window: str | None = None
    rev: int | None = None
    #: script AWAIT's optional wait, in seconds.
    timeout: float | None = None
    #: Script step modifiers. TRY sets ``on_fail="continue"``: record the
    #: failure and run on. ``IF ok`` / ``IF failed`` set ``cond``: run only
    #: when the previously *executed* step ended in that state.
    on_fail: str = "stop"
    cond: str = "none"
    #: EACH's ``max=N`` iteration cap; ``None`` = the runtime default.
    max_iter: int | None = None
    raw: str = ""


@dataclass(slots=True)
class ExecResult:
    """Outcome of a single native primitive invocation."""

    ok: bool
    error: str | None = None
    message: str | None = None
    #: True when the call may still be in flight (e.g. modal dialog opened).
    undetermined: bool = False
    #: True when the element simply does not offer this interface, as opposed to
    #: offering it and failing. This is the difference between "try the next
    #: route" and "stop and report": a capability may declare several `via`
    #: steps (write through the Value pattern, else type), and only a missing
    #: interface makes the next one the right thing to do. A pattern that
    #: exists and raises (a disabled control, a UIPI denial) is a real answer
    #: about this element and must be reported, not routed around.
    unsupported: bool = False


@dataclass(slots=True, frozen=True)
class UiEvent:
    """A coarse UI change event emitted by a provider subscription."""

    window_id: str
    kind: str  # structure | property | focus | window_open | window_close
    native: Any = None
