"""JSONL trace storage: one directory per run, content-addressed snapshots.

Layout::

    <root>/
    ├── manifest.json      format/version, time base, settings, run metadata
    ├── trace.jsonl        one JSON object per line, one event per object
    └── snapshots/
        └── ab/cd/<sha256>.sui.txt   SUI-Text snapshot, stored once per content

A record carries two clocks, like Playwright's trace: ``ts`` (wall time, for
reading) and ``mono_ms`` (monotonic, for ordering). The manifest says which is
which so a reader never has to guess.

Storage and lifecycle (the decision recorded for this stage):

* **Outside the repository.** The default root is the platform data directory
  (``%LOCALAPPDATA%\\somnusui\\traces`` on Windows), so trace files are never
  git-tracked by construction. ``--trace DIR`` writes to the directory named.
* **Not redacted.** Snapshots are the model-visible SUI-Text, which can contain
  window titles, form values and chat text. This stage deliberately records it
  as-is: the trace is evidence for tuning, kept off the repository.
* **Close-out entry point.** ``TODO(stage-final)`` below is the single place a
  future redactor plugs in before traces are moved into the repository as
  reference evidence. That step decides the retention set and the redaction
  rules; nothing in this module pretends to do it.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import platform
import sys
import time
import uuid
from pathlib import Path
from typing import Any

TRACE_FORMAT = "somnus-trace"
TRACE_VERSION = 1
TRACE_FILE = "trace.jsonl"
MANIFEST_FILE = "manifest.json"
SNAPSHOT_DIR = "snapshots"

#: ``SOMNUS_TRACE``: a directory, or a truthy marker (``1``/``true``/``on``)
#: meaning "use a fresh per-run directory under the platform data dir". Unset
#: (or ``0``/``false``/``off``/``no``) means tracing is off.
ENV_TRACE = "SOMNUS_TRACE"

_ON_VALUES = ("1", "true", "yes", "on")
_OFF_VALUES = ("", "0", "false", "no", "off")


#: Stage-final redaction entry point.
#:
#: TODO(stage-final): implement ``redact_trace(root, rules)`` here (or in a
#: dedicated module) when the project closes out. It must, at minimum:
#:   1. decide which runs are kept as reference evidence;
#:   2. rewrite ``trace.jsonl`` fields and snapshot bodies (window titles, form
#:      values, chat text) according to an explicit rule table;
#:   3. record the applied rules in ``manifest.json`` (a redacted trace must
#:      say it was redacted, and with what);
#:   4. copy the result into the repository path chosen for reference data -
#:      the live trace directory itself is never that path.
#: Until that step runs, a trace is local evidence only and must not be added
#: to the repository.


def default_trace_dir() -> Path:
    """The per-machine directory that holds traces (never inside the repo)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / "somnusui" / "traces"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "somnusui" / "traces"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "somnusui" / "traces"


def resolve_trace_root(value: str | os.PathLike[str] | None) -> Path | None:
    """Turn a ``--trace`` value / ``SOMNUS_TRACE`` value into a run directory.

    ``None`` means tracing stays off. A bare marker selects a fresh, uniquely
    named directory under :func:`default_trace_dir` so two runs never overwrite
    each other; any other value is a directory the caller named.
    """
    if value is None:
        return None
    text = os.fspath(value).strip()
    lowered = text.lower()
    if lowered in _OFF_VALUES:
        return None
    if lowered in _ON_VALUES:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return default_trace_dir() / f"session-{stamp}-{uuid.uuid4().hex[:6]}"
    return Path(text).expanduser()


def trace_path(root: str | os.PathLike[str]) -> Path:
    """The JSONL file for a trace directory (or the file itself, passed through)."""
    path = Path(root).expanduser()
    return path if path.is_file() else path / TRACE_FILE


class JsonlTraceSink:
    """Buffered JSONL writer with content-addressed SUI-Text snapshots."""

    enabled = True

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        settings: dict[str, Any] | None = None,
        session: str = "main",
        source: str = "somnus",
    ) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / TRACE_FILE
        self._snapshot_root = self.root / SNAPSHOT_DIR
        self._snapshot_root.mkdir(parents=True, exist_ok=True)
        #: Buffered on purpose: a trace is read after the run, so per-event
        #: fsync buys nothing and would charge the action path.
        self._file = self.path.open("w", encoding="utf-8", buffering=1 << 16)
        self._counts: dict[str, int] = {}
        self._closed = False
        self._manifest: dict[str, Any] = {
            "format": TRACE_FORMAT,
            "version": TRACE_VERSION,
            "source": source,
            "session": session,
            "created_at": _iso_now(),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "time_base": {
                "ts": "wall clock, epoch seconds",
                "mono_ms": "monotonic clock, milliseconds",
            },
            "trace": TRACE_FILE,
            "settings": dict(settings or {}),
        }
        self._write_manifest()
        # A CLI that exits without calling close() still gets a flushed trace.
        atexit.register(self.close)

    # ------------------------------------------------------------------ write

    def emit(self, event: str, **fields: Any) -> None:
        if self._closed:
            return
        record: dict[str, Any] = {
            "event": event,
            "ts": round(time.time(), 3),
            "mono_ms": round(time.monotonic() * 1000, 3),
        }
        record.update(fields)
        self._file.write(
            json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":"))
            + "\n"
        )
        self._counts[event] = self._counts.get(event, 0) + 1

    def snapshot(self, text: str) -> str | None:
        """Store ``text`` once; return its trace-relative path."""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        relative = Path(SNAPSHOT_DIR) / digest[:2] / digest[2:4] / f"{digest}.sui.txt"
        target = self.root / relative
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        return relative.as_posix()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._file.flush()
        finally:
            self._file.close()
        self._manifest["closed_at"] = _iso_now()
        self._manifest["events"] = dict(self._counts)
        self._write_manifest()

    def _write_manifest(self) -> None:
        (self.root / MANIFEST_FILE).write_text(
            json.dumps(self._manifest, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


def attach_trace(
    session: Any,
    value: str | os.PathLike[str] | None,
    *,
    settings: dict[str, Any] | None = None,
    session_label: str = "main",
) -> JsonlTraceSink | None:
    """Attach a JSONL sink to ``session`` and return it (or ``None`` when off).

    Duck-typed on purpose: the observability package must not import the runtime.
    """
    root = resolve_trace_root(value)
    if root is None:
        return None
    sink = JsonlTraceSink(root, settings=settings, session=session_label)
    session.trace_sink = sink
    return sink


def attach_from_env(session: Any, **kwargs: Any) -> JsonlTraceSink | None:
    return attach_trace(session, os.environ.get(ENV_TRACE), **kwargs)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")
