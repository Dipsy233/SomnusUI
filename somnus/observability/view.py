"""Text timeline for a recorded trace (``somnus trace show``).

The JSONL file is the data model; this is the reader that makes it legible
without a browser. It answers the stage-1 question directly: which route ran,
how the settle behaved, what the delta said, and why verification failed.

``--snapshots`` prints the before/after SUI-Text bodies that records reference.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from somnus.observability.jsonl import MANIFEST_FILE, trace_path


def load_events(path: str | Path) -> list[dict[str, Any]]:
    target = trace_path(path)
    events: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def render_timeline(
    path: str | Path, *, snapshots: bool = False, limit: int | None = None
) -> str:
    target = trace_path(path)
    root = target.parent
    events = load_events(target)
    lines = [f"trace {target}"]
    manifest = _load_manifest(root)
    if manifest:
        settings = manifest.get("settings") or {}
        lines.append(
            f"  {manifest.get('format')} v{manifest.get('version')} "
            f"session={manifest.get('session')} created={manifest.get('created_at')}"
        )
        if settings:
            lines.append(
                "  settings "
                + " ".join(f"{key}={_bare(value)}" for key, value in settings.items())
            )
    if not events:
        lines.append("  (no events)")
        return "\n".join(lines)

    base = events[0].get("mono_ms", 0.0)
    for record in events[: limit if limit is not None else None]:
        elapsed = (record.get("mono_ms", base) - base) / 1000
        lines.append(f"[+{elapsed:8.3f}s] {_format(record)}")

    if snapshots:
        refs: list[str] = []
        for record in events:
            for key in ("before", "after", "input", "output"):
                ref = record.get(key)
                if isinstance(ref, str) and ref not in refs:
                    refs.append(ref)
        for ref in refs:
            body_path = root / ref
            lines.append("")
            lines.append(f"--- snapshot {ref} ---")
            try:
                lines.append(body_path.read_text(encoding="utf-8"))
            except OSError:
                lines.append("(missing)")
    return "\n".join(lines)


def _load_manifest(root: Path) -> dict[str, Any]:
    path = root / MANIFEST_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _format(record: dict[str, Any]) -> str:
    name = str(record.get("event", "?"))
    fields = [
        f"{key}={_bare(value)}"
        for key, value in record.items()
        if key not in ("event", "ts", "mono_ms")
    ]
    return " ".join([name, *fields]) if fields else name


def _bare(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    if value is None:
        return "-"
    text = str(value)
    return text if text else '""'
