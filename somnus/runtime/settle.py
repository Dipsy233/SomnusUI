"""Settle: wait for the UI to become stable after an action.

Rule: consider the UI settled when no event has arrived
for ``quiet_ms`` and no observed node carries the ``loading`` state - or when
``timeout_s`` elapses, in which case we return ``settled=False`` but still report
whatever delta we observed.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from somnus.ir.node import RawTree

log = logging.getLogger("somnus.settle")

_TRACE_STATES = os.environ.get("SOMNUS_TRACE_STATES", "") not in ("", "0", "false")


@dataclass(slots=True)
class SettleResult:
    settled: bool
    waited_ms: int
    events: int = 0
    timed_out: bool = False


class Settler:
    def __init__(
        self,
        *,
        quiet_ms: int = 150,
        timeout_s: float = 3.0,
        poll_interval_ms: int = 50,
        clock: Callable[[], float] = time.monotonic,
        skip_when_deterministic: bool = False,
    ) -> None:
        self.quiet_ms = quiet_ms
        self.timeout_s = timeout_s
        self.poll_interval_ms = poll_interval_ms
        self.clock = clock
        #: Skip the quiet window for providers that declare
        #: ``is_deterministic``. Only the session turns this on, and only for
        #: such a provider; see :meth:`wait`.
        self.skip_when_deterministic = skip_when_deterministic

    def wait(
        self,
        fetch: Callable[[], RawTree],
        *,
        event_source: Callable[[], int] | None = None,
        deterministic: bool = False,
    ) -> tuple[RawTree, SettleResult]:
        """Poll ``fetch`` until stable.

        ``event_source`` (when provided) returns a monotonically increasing event
        count. The UI is quiet once the count stops rising for ``quiet_ms``.

        ``deterministic`` says the provider cannot change between the action and
        this call unless something is still in flight. When the settler was
        built with ``skip_when_deterministic``, one snapshot is enough: there is
        no second version of the tree to wait for, so the quiet window would
        only sleep before observing the same bytes. This is a provider
        *capability*, not a test mode - a replay provider declares it, UIA never
        does.
        """
        start = self.clock()
        last_event_count = event_source() if event_source else 0
        last_change = start
        previous: RawTree | None = None
        events = 0
        skip = deterministic and self.skip_when_deterministic

        while True:
            tree = fetch()
            now = self.clock()
            elapsed_ms = (now - start) * 1000

            if previous is None or not _same_shape(previous, tree):
                if _TRACE_STATES and previous is not None:
                    log.info("settle: state/structure moved, restarting quiet window")
                last_change = now
            previous = tree

            if event_source:
                count = event_source()
                if count != last_event_count:
                    events += max(0, count - last_event_count)
                    last_event_count = count
                    last_change = now

            if skip:
                return tree, SettleResult(
                    settled=True, waited_ms=int(elapsed_ms), events=events
                )

            quiet = (now - last_change) * 1000 >= self.quiet_ms
            loading = any("loading" in n.states for n in tree.walk())

            if quiet and not loading:
                return tree, SettleResult(
                    settled=True, waited_ms=int(elapsed_ms), events=events
                )
            if elapsed_ms >= self.timeout_s * 1000:
                return tree, SettleResult(
                    settled=False,
                    waited_ms=int(elapsed_ms),
                    events=events,
                    timed_out=True,
                )
            time.sleep(self.poll_interval_ms / 1000)


def _same_shape(a: RawTree, b: RawTree) -> bool:
    """Cheap structural comparison: role/name/value/child-count/state signature.

    ``states`` **must** be part of it. A control that only flips between disabled
    and enabled changes no role, name, value or child count, so leaving states
    out made such a change invisible to settle (found live: QQ's 发送 button is
    disabled while the message box is empty and enabled once it has text; after
    `set_text` the old code called the UI settled immediately, the stale
    `disabled` state survived into the next compilation, and `activate` was
    refused with TARGET_DISABLED - even though the button was by then clickable
    and a plain UIA Invoke worked).
    """
    return _sig(a.root) == _sig(b.root)


def _sig(node) -> str:
    # Sorted states keep the signature stable when the provider happens to
    # enumerate the same state set in a different order between snapshots.
    parts = [
        node.role,
        node.name or "",
        node.value or "",
        str(len(node.children)),
        ",".join(sorted(node.states)),
    ]
    for c in node.children:
        parts.append(_sig(c))
    return "|".join(parts)
