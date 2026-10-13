"""Settle: wait for the UI to become stable after an action.

Rule (dev doc section 9.2): consider the UI settled when no event has arrived
for ``quiet_ms`` and no observed node carries the ``loading`` state - or when
``timeout_s`` elapses, in which case we return ``settled=False`` but still report
whatever delta we observed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from somnus.ir.node import RawTree


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
    ) -> None:
        self.quiet_ms = quiet_ms
        self.timeout_s = timeout_s
        self.poll_interval_ms = poll_interval_ms
        self.clock = clock

    def wait(
        self,
        fetch: Callable[[], RawTree],
        *,
        event_source: Callable[[], int] | None = None,
    ) -> tuple[RawTree, SettleResult]:
        """Poll ``fetch`` until stable.

        ``event_source`` (when provided) returns a monotonically increasing event
        count. The UI is quiet once the count stops rising for ``quiet_ms``.
        """
        start = self.clock()
        last_event_count = event_source() if event_source else 0
        last_change = start
        previous: RawTree | None = None
        events = 0

        while True:
            tree = fetch()
            now = self.clock()
            elapsed_ms = (now - start) * 1000

            if previous is None or not _same_shape(previous, tree):
                last_change = now
            previous = tree

            if event_source:
                count = event_source()
                if count != last_event_count:
                    events += max(0, count - last_event_count)
                    last_event_count = count
                    last_change = now

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
    """Cheap structural comparison: role/name/value/child-count signature."""
    return _sig(a.root) == _sig(b.root)


def _sig(node) -> str:
    parts = [node.role, node.name or "", node.value or "", str(len(node.children))]
    for c in node.children:
        parts.append(_sig(c))
    return "|".join(parts)
