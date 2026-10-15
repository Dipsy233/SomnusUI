"""Action / result / event value objects used across the runtime boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ActionKind = Literal["call", "find", "inspect", "observe", "unknown"]


@dataclass(slots=True)
class Action:
    """A parsed, validated LLM request."""

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
