"""The trace event contract.

A trace is a flat sequence of ``(event, fields, time)`` records. It is not a
log: the runtime emits one event per decision it took (which route ran, what
the settle saw, what the delta was, why verification failed), so a later reader
can answer "why did this call end like that" without re-running the UI.

The contract is intentionally small and decoupled from the runtime:

* ``emit(event, **fields)`` - one record; fields must be JSON-safe scalars,
  lists or dicts (the JSONL sink falls back to ``str`` for anything else);
* ``snapshot(text) -> ref | None`` - store a SUI-Text snapshot and return a
  reference the event can carry. Content addressing is the sink's concern;
  the runtime only holds the reference string;
* ``close()`` - flush and release.

A sink with ``enabled = False`` must make every call a no-op. The runtime checks
that flag before doing any work, which is what keeps a normal run at zero cost.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TraceSink(Protocol):
    """Where trace records go. Implementations own storage, not semantics."""

    #: False makes the runtime skip every trace branch.
    enabled: bool

    def emit(self, event: str, **fields: Any) -> None: ...

    def snapshot(self, text: str) -> str | None: ...

    def close(self) -> None: ...


class NullTraceSink:
    """The default: tracing off, and nothing on the hot path but one flag check."""

    enabled = False

    def emit(self, event: str, **fields: Any) -> None:
        return None

    def snapshot(self, text: str) -> str | None:
        return None

    def close(self) -> None:
        return None


class MemoryTraceSink:
    """An in-process sink for tests and for callers that inspect a session.

    Snapshots are kept verbatim and addressed by the same ``sha256`` form the
    JSONL sink uses, so a test can assert on content the same way a reader would.
    """

    enabled = True

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.snapshots: dict[str, str] = {}

    def emit(self, event: str, **fields: Any) -> None:
        self.events.append({"event": event, **fields})

    def snapshot(self, text: str) -> str | None:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        self.snapshots[digest] = text
        return digest

    def close(self) -> None:
        return None

    def events_named(self, name: str) -> list[dict[str, Any]]:
        return [record for record in self.events if record.get("event") == name]
