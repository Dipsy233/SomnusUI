"""Read SUI-Text observations back into facts a self-check can act on.

The self-check commands are host code: they read the text a model would have
received and turn it into a type, a name and a ref. That is deliberately the
*same* representation the model uses - no native handle, no introspection into
the compiler - so a replay or check exercises the surface the model actually
sees rather than a privileged side channel.

Only two things are parsed here:

* node lines (``TYPE ref "name" attrs +caps``), including the ``WINDOW`` /
  ``WINDOWS`` forms that name top-level windows;
* the short ``FOUND n`` reply, so a re-located ref can be read back without
  reaching into the session's compiled result.

The parser is intentionally permissive about everything after the name: the
point is identity (type, name, ref, order), not a second copy of the grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: ``TYPE ref "name" rest``. A ref is the bare name the runtime prints; the
#: type is the rendered display type (``PRODUCT``, ``BUTTON``, ``WINDOW``).
_LINE_RE = re.compile(
    r"""^\s*
        (?P<kind>[A-Z][A-Z0-9_]*)\s+
        (?P<ref>[a-z]{1,3}[0-9]+)
        (?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?
        (?P<rest>.*)$""",
    re.VERBOSE,
)

#: A line that is neither a node nor a window list header is a marker, a delta
#: or an advisory note; none of them names something to act on.
_SKIP_PREFIXES = ("@rev", "+", "-", "~", "…", "WINDOWS count=")


@dataclass(frozen=True, slots=True)
class NodeLine:
    """One identity line from an observation."""

    kind: str
    ref: str
    name: str | None
    rest: str
    depth: int
    raw: str

    @property
    def is_window(self) -> bool:
        return self.kind in ("WINDOW", "WINDOWS")


def parse_observation(text: str) -> list[NodeLine]:
    """Every identity line in *text*, in document order."""
    out: list[NodeLine] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith(_SKIP_PREFIXES):
            continue
        # The compact desktop summary puts several windows on one line:
        # ``WINDOWS w1 "a" | w2 "b" active``. Split it so each window is an
        # addressable identity rather than only the first segment being seen.
        if stripped.startswith("WINDOWS "):
            indent = raw[: len(raw) - len(raw.lstrip())]
            for segment in stripped[len("WINDOWS ") :].split(" | "):
                _append_line(out, f"{indent}WINDOW {segment}", indent)
            continue
        _append_line(out, raw, raw[: len(raw) - len(raw.lstrip())])
    return out


def _append_line(out: list[NodeLine], raw: str, indent: str) -> None:
    match = _LINE_RE.match(raw)
    if match is None:
        return
    name = match.group("name")
    out.append(
        NodeLine(
            kind=match.group("kind"),
            ref=match.group("ref"),
            name=_unescape(name) if name is not None else None,
            rest=match.group("rest"),
            depth=len(indent),
            raw=raw,
        )
    )


def line_for_ref(text: str, ref: str) -> NodeLine | None:
    """The line that printed *ref*, or ``None`` when the text does not carry it."""
    for line in parse_observation(text):
        if line.ref == ref:
            return line
    return None


def ordinal_of(lines: list[NodeLine], target: NodeLine) -> int:
    """Position of *target* among lines with the same type and name.

    Refs are session-scoped, so a replay cannot reuse them. Type + name plus
    document order is the weakest identity that still separates two rows that
    share a caption.
    """
    index = 0
    for line in lines:
        if line.kind == target.kind and line.name == target.name:
            if line.ref == target.ref:
                return index
            index += 1
    return 0


def pick_window(text: str) -> NodeLine | None:
    """The target window named by an observation, if one is printed.

    Long-form headers mark the observed window with ``obs``; the default
    desktop summary marks the foreground window with ``active``. Either is a
    better guess than the first line, which may be an unrelated helper window.
    """
    windows = [line for line in parse_observation(text) if line.is_window]
    for marker in ("obs", "active"):
        for line in windows:
            if marker in line.rest.split():
                return line
    return windows[0] if windows else None


def pick_actionable(text: str) -> NodeLine | None:
    """The first named node that prints a capability, else the first named node.

    A capability token (``+something``) is the strongest evidence that the
    element is real and addressable; the fallback keeps the check working on a
    bare screen. The caller still only invokes a harmless capability
    (``focus``), so this is a smoke probe, not an action.
    """
    named = [
        line for line in parse_observation(text) if line.name and not line.is_window
    ]
    for line in named:
        if "+" in line.rest and "disabled" not in line.rest.split():
            return line
    return named[0] if named else None


def parse_found(text: str) -> list[NodeLine]:
    """The hit lines of a ``FOUND`` reply (empty when the reply is a miss).

    A miss is not empty text: the runtime follows ``FOUND 0`` with a list of
    containers to scope into. Those are suggestions, not hits, so the count on
    the first line decides.
    """
    lines = text.splitlines()
    if not lines or not lines[0].startswith("FOUND "):
        return []
    try:
        count = int(lines[0].split()[1])
    except (IndexError, ValueError):
        return []
    if count <= 0:
        return []
    return [line for line in parse_observation("\n".join(lines[1:])) if line.kind != "FOUND"]


def _unescape(text: str) -> str:
    """Decode the two escapes the serializer emits (see ``sui/parse.py``).

    Everything else - ``\\n``, ``\\t``, ``\\r`` - is kept verbatim, because the
    serializer only promises one-line display and the parser deliberately does
    not turn a Windows path into a line feed.
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in ("\\", '"'):
                out.append(nxt)
                i += 2
                continue
            out.append(char)
            i += 1
            continue
        out.append(char)
        i += 1
    return "".join(out)
