"""Settle: wait for the UI to become stable after an action.

Rule: consider the UI settled when no event has arrived
for ``quiet_ms`` and no observed node carries the ``loading`` state - or when
``timeout_s`` elapses, in which case we return ``settled=False`` but still report
whatever delta we observed.

Signature probe
---------------
Re-reading the *whole* tree on every polling round is what made a call to a
large window cost two or three full snapshots. A provider can offer a cheap
``probe`` (a snapshot limited to the fields in :data:`SIGNATURE_FIELDS`) and the
settler then works like this:

* nothing changed -> probe only, reuse the tree already on hand, zero full
  snapshots;
* something changed -> one full snapshot, then probes until the signature is
  quiet again.

That is "1 full snapshot + 1 probe" instead of "2 full snapshots", and a no-op
call costs no snapshot at all. The probe must produce the *same* signature as a
full snapshot (:func:`_sig` is shared), otherwise a state flip that is real
could look unchanged and settle early - the QQ / form disabled->enabled case is
the regression this must not break.
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

#: The fields :func:`_sig` compares between two frames. A provider signature
#: probe must be able to produce exactly these; ``states`` is listed last
#: because a control can flip between disabled and enabled without changing any
#: of the structural fields (that was a real, silent miss - a control that only
#: changes state used to read as "unchanged").
SIGNATURE_FIELDS: tuple[str, ...] = (
    "role",
    "name",
    "value",
    "child_count",
    "states",
)


@dataclass(slots=True)
class SettleResult:
    settled: bool
    waited_ms: int
    events: int = 0
    timed_out: bool = False
    #: Full tree fetches this wait performed (``fetch`` callable).
    snapshots: int = 0
    #: Cheap signature probes this wait performed (``probe`` callable).
    probes: int = 0


class Settler:
    def __init__(
        self,
        *,
        quiet_ms: int = 40,
        timeout_s: float = 3.0,
        poll_interval_ms: int = 10,
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
        previous: RawTree | None = None,
        previous_signature: str | None = None,
        probe: Callable[[], RawTree] | None = None,
        signature: Callable[[RawTree], str | None] | None = None,
    ) -> tuple[RawTree, SettleResult]:
        """Poll until stable, returning the tree to compile.

        ``event_source`` (when provided) returns a monotonically increasing event
        count. The UI is quiet once the count stops rising for ``quiet_ms``.

        ``previous`` is the tree the session compiled *before* the action. When
        it is given together with ``probe``, a probe that yields the same
        signature lets the wait return ``previous`` without taking a new full
        snapshot at all.

        ``previous_signature`` is the signature of that tree **as read from the
        provider**, i.e. taken before compilation. Compilation normalises raw
        nodes in place (``edit`` -> ``input``), so the stored tree's signature is
        no longer the one a fresh read would produce; comparing a probe against
        the tree would then always say "changed" and defeat the whole shortcut.
        The caller keeps the pre-compile signature for exactly this reason.

        ``deterministic`` says the provider cannot change between the action and
        this call unless something is still in flight. When the settler was
        built with ``skip_when_deterministic``, one snapshot is enough: there is
        no second version of the tree to wait for, so the quiet window would
        only sleep before observing the same bytes. This is a provider
        *capability*, not a test mode - a replay provider declares it, UIA never
        does.

        ``signature`` projects a tree onto the set that is actually comparable
        (the session's watched nodes). It returns ``None`` when that set cannot
        be evaluated - a watched node is missing from the tree (scrolled away,
        or not captured). The rule then is a hard one: **drop the probe and take
        full snapshots**, never treat "cannot see it" as "did not change".
        """
        sig_of = signature if signature is not None else _full_signature
        start = self.clock()
        last_event_count = event_source() if event_source else 0
        # The quiet window starts *now*, not at the first frame: when the caller
        # hands us the pre-action tree, "unchanged" has to mean "unchanged for
        # quiet_ms since the action", otherwise an action that has not landed
        # yet would be declared settled on the first probe.
        last_change = start
        current = previous
        current_sig = (
            previous_signature
            if (previous is not None and previous_signature is not None)
            else (sig_of(previous) if previous is not None else None)
        )
        if current_sig is None:
            # The caller could not evaluate the watched set before the action;
            # probing would be meaningless, so use the full-read loop.
            probe = None
        using_watch = probe is not None
        snapshots = 0
        probes = 0
        events = 0
        skip = deterministic and self.skip_when_deterministic

        while True:
            if current is None:
                tree = fetch()
                snapshots += 1
                current = tree
                current_sig = (
                    sig_of(tree) if signature is not None else _full_signature(tree)
                )
                last_change = self.clock()
                if skip:
                    return tree, SettleResult(
                        settled=True,
                        waited_ms=int((self.clock() - start) * 1000),
                        events=events,
                        snapshots=snapshots,
                        probes=probes,
                    )
            elif skip:
                if probe is not None:
                    probed = _try_probe(probe)
                    probe_sig = sig_of(probed) if probed is not None else None
                    if probed is not None:
                        probes += 1
                        if probe_sig is not None and probe_sig == current_sig:
                            return current, SettleResult(
                                settled=True,
                                waited_ms=int((self.clock() - start) * 1000),
                                events=events,
                                snapshots=snapshots,
                                probes=probes,
                            )
                tree = fetch()
                snapshots += 1
                return tree, SettleResult(
                    settled=True,
                    waited_ms=int((self.clock() - start) * 1000),
                    events=events,
                    snapshots=snapshots,
                    probes=probes,
                )
            elif probe is not None:
                # Cheap path: only probe, and pay for a full snapshot when the
                # signature moved. Until the quiet window has elapsed there is
                # nothing to confirm yet, so the loop only sleeps.
                if current_sig is None:
                    # The watched set cannot be evaluated on the frame we have
                    # (a node moved out of the captured layers). Probing can no
                    # longer prove anything, and the pre-action frame must not be
                    # trusted either: take one full snapshot now, then continue
                    # with full reads.
                    if _TRACE_STATES:
                        log.info("settle: watched set not covered; using full reads")
                    probe = None
                    using_watch = False
                    tree = fetch()
                    snapshots += 1
                    current = tree
                    current_sig = _full_signature(tree)
                    last_change = self.clock()
                elif (self.clock() - last_change) * 1000 >= self.quiet_ms:
                    probed = _try_probe(probe)
                    probe_sig = sig_of(probed) if probed is not None else None
                    if probed is not None:
                        probes += 1
                    if probed is not None and probe_sig is None:
                        # The probe answered but the watched set cannot be
                        # evaluated on it. That is "cannot see it", never
                        # "unchanged": one full snapshot now, full reads after.
                        if _TRACE_STATES:
                            log.info("settle: probe missing watched nodes; using full reads")
                        probe = None
                        using_watch = False
                        tree = fetch()
                        snapshots += 1
                        current = tree
                        current_sig = _full_signature(tree)
                        last_change = self.clock()
                    elif probe_sig is not None and probe_sig == current_sig:
                        # Nothing moved: keep the tree we already have.
                        tree = current
                    else:
                        # A change (or a probe we could not read) -> one full
                        # snapshot, which is exactly what the old code did
                        # every round.
                        tree = fetch()
                        snapshots += 1
                        current = tree
                        current_sig = sig_of(tree)
                        last_change = self.clock()
                else:
                    tree = current
            else:
                tree = fetch()
                snapshots += 1
                tree_sig = sig_of(tree) if using_watch else None
                if tree_sig is None:
                    # The watched set is not evaluable on this frame either; the
                    # only honest comparison left is the whole tree.
                    tree_sig = _full_signature(tree)
                    using_watch = False
                if tree_sig != current_sig:
                    last_change = self.clock()
                current_sig = tree_sig
                current = tree

            now = self.clock()
            elapsed_ms = (now - start) * 1000

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
                    settled=True,
                    waited_ms=int(elapsed_ms),
                    events=events,
                    snapshots=snapshots,
                    probes=probes,
                )
            if elapsed_ms >= self.timeout_s * 1000:
                return tree, SettleResult(
                    settled=False,
                    waited_ms=int(elapsed_ms),
                    events=events,
                    timed_out=True,
                    snapshots=snapshots,
                    probes=probes,
                )
            time.sleep(self.poll_interval_ms / 1000)


def _try_probe(probe: Callable[[], RawTree]) -> RawTree | None:
    """Run a signature probe, degrading to "unknown" on failure.

    A probe that raises is not fatal: it means the cheap path could not answer
    this round, and the full snapshot below still gives a correct result. The
    one exception that must *not* be swallowed is ``KeyError`` - that is the
    provider saying the target window is gone, which the session maps to a
    STALE/replaced-window outcome.
    """
    try:
        return probe()
    except KeyError:
        raise
    except Exception as exc:  # noqa: BLE001 - a cheap probe must never fail a call
        log.debug("settle probe failed (%s); falling back to a full snapshot", exc)
        return None


def _full_signature(tree: RawTree) -> str:
    """The whole-tree signature: what the settler used before watched sets."""
    return _sig(tree.root)


def signature_of(tree: RawTree) -> str:
    """The signature of ``tree`` as settle compares it (see :data:`SIGNATURE_FIELDS`).

    Public so the session can store a frame's signature *before* compilation
    normalises the tree in place, and so tests can assert the two paths agree.

    ``states`` **must** be part of it. A control that only flips between
    disabled and enabled changes no role, name, value or child count, so leaving
    states out made such a change invisible to settle (found live: QQ's 发送
    button is disabled while the message box is empty and enabled once it has
    text; after `set_text` the old code called the UI settled immediately, the
    stale `disabled` state survived into the next compilation, and `activate`
    was refused with TARGET_DISABLED - even though the button was by then
    clickable and a plain UIA Invoke worked).
    """
    return _sig(tree.root)


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
