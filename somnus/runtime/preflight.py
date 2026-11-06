"""Action preflight: can this element accept this capability right now?

The check reads only the *compiled* node - ``states`` the platform already
reported - so it costs no COM call and nothing at all on the happy path. It is
the runtime's equivalent of Playwright's actionability, reduced to the signals
UIA actually publishes.

What is checked, and why only this:

* ``disabled`` - the one state that makes every gesture fail for a real
  reason. The refusal keeps its historical code (:data:`ErrorCode.TARGET_DISABLED`)
  when no wait was possible, so existing guidance ("satisfy the precondition")
  stays true.
* ``readonly`` for a text write - a capability a pack may still declare on a
  field the platform marks read-only.

What is deliberately *not* checked:

* ``offscreen`` - ``IsOffscreen`` is not a visibility signal here. Measured on
  a 240-row list where 12 rows were on screen, it reported 0 hidden rows
  (README, viewport section). Blocking on it could refuse an element the agent
  can see, and the ``select`` route already scrolls a virtualised row into view.
* ``collapsed`` - a collapsed tree item *must* accept ``expand``; the state is
  a precondition of the capability, not a blocker.
* ``loading`` - reads and settles already report it; an action against a
  loading container is the application's call, not the runtime's.

The wait is bounded and only on the blocked path: a control that is briefly
disabled (a submit button during validation, a row while a list repaints) can
clear its state within a frame or two. The session polls the provider's cheap
window signature, and only after a *change* pays for a full read.
"""

from __future__ import annotations

from dataclasses import dataclass

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.node import SemanticNode

#: Capabilities that ask nothing of the element's actionability: they read or
#: move focus, so a disabled control can still be read and focused.
NON_ACTING_CAPS: frozenset[str] = frozenset({"read", "focus", "inspect"})

#: Capabilities that write text, which a read-only field cannot do.
_WRITE_CAPS: frozenset[str] = frozenset({"set_text"})


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """The outcome of one preflight check."""

    ok: bool
    #: The state that blocked the action, when ``ok`` is False.
    state: str | None = None
    #: Whether the state is one that can clear on its own (so waiting is useful).
    waitable: bool = False


OK = PreflightResult(ok=True)


def resolved_capability(node: SemanticNode, capability: str | None) -> str | None:
    """The capability's canonical name on ``node``, or None when absent.

    Aliases resolve here so a pack that calls a write ``write`` is still judged
    by the canonical ``set_text`` the executor will run.
    """
    if not capability:
        return None
    cap = node.get_cap(capability)
    if cap is not None:
        return cap.name
    for candidate in node.caps:
        if capability in getattr(candidate, "aliases", ()):
            return candidate.name
    return None


def capability_present(node: SemanticNode, capability: str | None) -> bool:
    """Whether ``node`` declares ``capability`` (by name or by alias)."""
    return resolved_capability(node, capability) is not None


def check(node: SemanticNode | None, capability: str | None) -> PreflightResult:
    """State check for one action; never reads the platform.

    A capability the node does not declare returns ``OK``: the resolver owns
    that answer and its ``CAPABILITY_NOT_FOUND`` hint names the nodes that do
    declare it, which is more useful than a state error.
    """
    if node is None:
        return OK
    name = resolved_capability(node, capability)
    if name is None or name in NON_ACTING_CAPS:
        return OK
    if "disabled" in node.states:
        return PreflightResult(ok=False, state="disabled", waitable=True)
    if "readonly" in node.states and name in _WRITE_CAPS:
        return PreflightResult(ok=False, state="readonly")
    return OK


def error(result: PreflightResult, ref: str | None, *, waited_ms: int = 0) -> SomnusError:
    """The one-line refusal for a blocked action.

    ``TARGET_DISABLED`` is kept when nothing was waited on (a batch dry run, or
    a provider with no signature probe): it has always meant exactly this, and
    existing guidance and tests depend on it. Once a bounded wait actually ran
    and the state did not clear, the refusal says so with ``NOT_ACTIONABLE``,
    naming the state and the time spent - "disabled" alone would not tell the
    agent that the runtime already gave the application time to recover.
    """
    if result.state == "disabled":
        if waited_ms > 0:
            return SomnusError(
                ErrorCode.NOT_ACTIONABLE,
                ref=ref,
                hint="wait",
                message=f"state=disabled waited={waited_ms}ms",
            )
        return SomnusError(ErrorCode.TARGET_DISABLED, ref=ref)
    return SomnusError(
        ErrorCode.NOT_ACTIONABLE,
        ref=ref,
        hint="inspect",
        message=f"state={result.state}",
    )
