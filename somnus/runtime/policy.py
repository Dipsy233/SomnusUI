"""Policy: allow-list, risk gating, confirmation, UIPI awareness (section 12).

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
    #: host (CLI flag / MCP confirmation) explicitly confirms (dev doc section
    #: 12.3 - zero unconfirmed critical actions).
    confirmed: bool = False
    #: Capabilities that need confirmation even if the pack did not mark them.
    extra_critical: frozenset[str] = field(default_factory=frozenset)

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
