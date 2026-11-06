"""Which desktop a self-check is allowed to touch, and when it must refuse.

The rule is fixed by the project, not by the command:

* **child session** - the default. Its own input desktop and foreground mean a
  check can raise windows and type without competing with the person using the
  machine.
* **offline fixtures** (mock / scenario / a recorded fixture) - the only other
  default, for machines without a child session or without Windows.
* **the user's main desktop** - never implicit. It requires ``--real``, because
  it takes the foreground away from the person at the keyboard.

This module refuses instead of falling back: a check that quietly moved to the
main desktop would be exactly the disturbance the child session exists to
prevent, and the caller would not know it happened.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass


class SandboxError(RuntimeError):
    """The requested desktop cannot be used; the message says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def render(self) -> str:
        return f"ERR {self.code} hint=report-user msg=\"{self.message}\""


@dataclass(frozen=True, slots=True)
class Sandbox:
    """The desktop a check ended up on, plus the provider that serves it."""

    desktop: str  # "child" | "main"
    provider: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.desktop} ({self.provider})"


def enter(session, *, real: bool = False) -> Sandbox:
    """Move *session* to the sandbox desktop, or raise :class:`SandboxError`.

    ``real=True`` is the explicit ``--real`` opt-in. Everything else targets the
    child session and requires one to be live (or the process to already be
    inside one).
    """
    from somnus.session import childsession

    if not sys.platform.startswith("win"):
        raise SandboxError(
            "PERMISSION_DENIED",
            "a live desktop requires Windows; use --demo, --scenario or "
            "--fixture for the offline path",
        )

    if real:
        if childsession.inside_child_session():
            raise SandboxError(
                "PERMISSION_DENIED",
                "this process is already inside the child session, where the "
                "main desktop is not reachable; run the check from the main "
                "session to use --real",
            )
        if session.session_label != "main":
            answer = session.switch_session("main")
            if answer.startswith("ERR"):
                raise SandboxError("PERMISSION_DENIED", answer)
        return Sandbox("main", _provider_name(session))

    if not childsession.inside_child_session():
        live = childsession.live_child_session()
        if live is None:
            raise SandboxError(
                "PERMISSION_DENIED",
                "no child session is live; start one with "
                "`somnus child-session start --auto` (after the one-time "
                "`somnus child-session setup`), or pass --real to use the "
                "user's desktop explicitly. The check will not fall back on "
                "its own",
            )
    answer = session.switch_session("child")
    if answer.startswith("ERR"):
        raise SandboxError("PERMISSION_DENIED", answer)
    return Sandbox("child", _provider_name(session))


def _provider_name(session) -> str:
    return str(getattr(session.provider, "name", "unknown"))
