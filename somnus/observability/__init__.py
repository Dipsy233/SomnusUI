"""Runtime decision traces: the evidence layer behind tuning and self-evaluation.

This package is deliberately independent of the runtime: it knows the *shape* of
an event (a name plus JSON-safe fields) and how to store one, never what a
``Session`` is or how an action resolves. The runtime emits events through
:class:`~somnus.observability.trace.TraceSink`; a sink decides where they go.

Trace output is a development artefact, not part of the product surface:

* it is written **outside the repository** by default (the platform data
  directory, see :mod:`somnus.observability.jsonl`), so git never tracks it;
* it contains whatever SUI-Text contains - window titles, form values, chat
  text - and is **not redacted in this stage**. Redaction is a close-out step
  before any trace is moved into the repository as reference evidence; the
  entry point and the decision it has to make are documented in
  :mod:`somnus.observability.jsonl` (``TODO(stage-final)``).
"""

from somnus.observability.trace import (
    MemoryTraceSink,
    NullTraceSink,
    TraceSink,
)

__all__ = ["MemoryTraceSink", "NullTraceSink", "TraceSink"]
