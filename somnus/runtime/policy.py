"""Policy: allow-list, risk gating, confirmation, UIPI awareness.

Policy is *authorization*, deliberately separate from pack projection (which is
observation policy only). Three responsibilities:

* process/window allow-list;
* critical-action confirmation (returns CONFIRMATION_REQUIRED unless the host
  has pre-confirmed the exact action);
* refusing anything the LLM is not allowed to reach (native ids, coordinates,
  raw keys) - those never exist as capabilities, so this is a defensive check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from somnus.errors import ErrorCode, SomnusError

#: Capability names that are always considered critical (belt and braces).
CRITICAL_NAMES = frozenset(
    {"submit", "pay", "delete", "send", "close", "checkout", "confirm", "purchase"}
)


@dataclass
class Policy:
    #: Process names (lower-case) the runtime may attach to. Empty = all.
    allow_processes: tuple[str, ...] = ()
    #: False by default: critical actions return CONFIRMATION_REQUIRED until the
    #: host (CLI flag / MCP confirmation) explicitly confirms them.
    confirmed: bool = False
    #: Capabilities that need confirmation even if the pack did not mark them.
    extra_critical: frozenset[str] = field(default_factory=frozenset)
    #: Whether synthesised keyboard input may be used at all. **On by default
    #: in this build.** The project's stance is still that an agent drives an
    #: application through the interface the application publishes, not by
    #: pretending to be a person at the keyboard - but measured against the
    #: 50-task benchmark, refusing the keyboard left whole classes of tasks
    #: (terminals, omniboxes that commit only on Enter, self-drawn editors)
    #: unfinished with no alternative. Typing is still the last resort in every
    #: `via` chain; it is merely *allowed* now, and a host can turn it off with
    #: ``--no-allow-keyboard`` or ``allow_keyboard = false``.
    allow_keyboard: bool = True
    #: Whether the clipboard channel may be used. **On by default in this
    #: build.** It is cheaper and far more reliable than typing (constant ~37 ms
    #: regardless of length, no queue to stall), but it borrows a resource that
    #: belongs to the user, needs the target in the foreground, and cannot
    #: restore a non-text clipboard. The writes are marked opt-out so they do
    #: not enter clipboard history or cloud sync, and the channel refuses
    #: outright when the clipboard holds something that could not be put back
    #: (docs/text-input-channels.md 3.3, 6). Turn it off with
    #: ``--no-allow-clipboard`` or ``allow_clipboard = false``.
    allow_clipboard: bool = True

    def check_step(self, primitive: str) -> None:
        """Refuse a step the policy does not authorise.

        Checked per step rather than per capability, because a capability may
        declare a native route *and* a keyboard fallback; only the fallback
        needs authorisation.

        The message names **both** ways to opt in: the one-run flag and the
        persistent settings file. An agent that hits this refusal can then act on
        it - the settings file is deliberately small and text-only so it can be
        edited without the runtime's help (see somnus/config.py).
        """
        if primitive == "type_text" and not self.allow_keyboard:
            raise SomnusError(
                ErrorCode.ACTION_NOT_SUPPORTED,
                message=(
                    "the element offers no interface this runtime can use, and "
                    "synthetic keyboard input is disabled; enable it for one run "
                    "with --allow-keyboard, or persistently with "
                    "`somnus config --allow-keyboard`"
                ),
            )
        if primitive == "paste_text" and not self.allow_clipboard:
            raise SomnusError(
                ErrorCode.ACTION_NOT_SUPPORTED,
                message=(
                    "the element offers no interface this runtime can use, and "
                    "the clipboard channel is disabled; enable it for one run "
                    "with --allow-clipboard, or persistently with "
                    "`somnus config --allow-clipboard` (it borrows the clipboard "
                    "and needs the target in the foreground)"
                ),
            )

    def check_process(self, process: str) -> None:
        if not self.allow_processes:
            return
        proc = (process or "").lower()
        if not any(a.lower() == proc for a in self.allow_processes):
            raise SomnusError(ErrorCode.PERMISSION_DENIED, message=process)

    def check_action(self, resolution, *, confirmed: bool | None = None) -> None:
        critical = (
            resolution.risk == "critical"
            or resolution.capability in CRITICAL_NAMES
            or resolution.capability in self.extra_critical
        )
        if not critical:
            return
        is_confirmed = self.confirmed if confirmed is None else confirmed
        if not is_confirmed:
            raise SomnusError(
                ErrorCode.CONFIRMATION_REQUIRED,
                ref=resolution.ref,
                message=f"{resolution.ref}.{resolution.capability}",
            )

    def check_native_permission(self, error: str | None) -> None:
        if error in ("PERMISSION_DENIED", "UIPI"):
            raise SomnusError(ErrorCode.PERMISSION_DENIED)
