"""SUI-Text action parsing (dev doc section 6.2).

The *only* action syntax is::

    CALL <ref>.<cap>(<args>)
    FIND [TYPE] "STRING" [in REF]
    INSPECT REF [depth=N] [range=A:B]
    UNKNOWN "reason"

Primitive verbs (``click``, ``uia_invoke``) and native identifiers
(``AutomationId``) are rejected here, before they can reach the resolver.
"""

from __future__ import annotations

import re
from typing import Any

from somnus.errors import parse_error
from somnus.ir.action import Action

# ---------------------------------------------------------------------------
# Tokenizing
# ---------------------------------------------------------------------------

_CALL_RE = re.compile(
    r"""^CALL\s+
        (?P<ref>[a-z]{1,3}[0-9]+)\s*\.\s*
        (?P<cap>[A-Za-z_][A-Za-z0-9_]*)
        \s*\(\s*(?P<args>.*?)\s*\)\s*$""",
    re.VERBOSE | re.IGNORECASE,
)

#: ``ref.cap(args)`` with the CALL keyword omitted (tool-boundary tolerance).
_BARE_CALL_RE = re.compile(
    r"^[a-z]{1,3}[0-9]+\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\s*\(.*\)\s*$",
    re.IGNORECASE,
)

_FIND_RE = re.compile(
    r"""^FIND\s+
        (?:(?P<scope>[a-z]{1,3}[0-9]+)\s+)?        # scope first: ref-shaped
        (?:(?P<type>[A-Za-z_][A-Za-z0-9_]*)\s+)?   # then optional type
        (?P<query>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
        (?:\s+in\s+(?P<scope2>[a-z]{1,3}[0-9]+))?
        \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_INSPECT_RE = re.compile(
    r"""^INSPECT\s+(?P<ref>[a-z]{1,3}[0-9]+)
        (?:\s+depth=(?P<depth>\d+))?
        (?:\s+range=(?P<r0>\d+):(?P<r1>\d+))?
        \s*$""",
    re.VERBOSE | re.IGNORECASE,
)

_UNKNOWN_RE = re.compile(r'^UNKNOWN\s+(?P<reason>".*"|\S+)\s*$', re.IGNORECASE)

_OBSERVE_RE = re.compile(
    r"^OBSERVE(?:\s+(?P<window>[a-z]{1,3}[0-9]+))?"
    r"(?:\s+(?P<rest>windows|all|desktop|full))?"
    r"(?:\s+expand\s+(?P<expand>[a-z]{1,3}[0-9]+))?\s*$",
    re.IGNORECASE,
)

#: Primitive verbs that must never appear in an action.
FORBIDDEN_VERBS = frozenset(
    {
        "click",
        "doubleclick",
        "rightclick",
        "mousemove",
        "keypress",
        "sendkeys",
        "type",
        "screenshot",
        "uia_invoke",
        "invoke_native",
    }
)

_ARG_RE = re.compile(r'^\s*(?:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)?(.+?)\s*$')


def parse_action(text: str, rev: int | None = None) -> Action:
    """Parse a single SUI-Text action line.

    Raises :class:`~somnus.errors.SomnusError` with ``PARSE_ERROR`` on any
    syntax violation - never guesses.
    """
    raw = text.strip()
    if not raw:
        raise parse_error("empty action")

    m = _CALL_RE.match(raw)
    if m:
        return _parse_call(m, rev, raw)

    # Tolerance at the tool boundary: models frequently drop the leading CALL.
    # ``b1.activate()`` is unambiguous, so accept it while the *specified*
    # syntax (and everything the runtime emits) stays ``CALL b1.activate()``.
    if _BARE_CALL_RE.match(raw):
        return parse_action("CALL " + raw, rev)

    m = _FIND_RE.match(raw)
    if m:
        return _parse_find(m, raw)

    m = _INSPECT_RE.match(raw)
    if m:
        return _parse_inspect(m, raw)

    m = _UNKNOWN_RE.match(raw)
    if m:
        reason = m.group("reason").strip('"')
        return Action(kind="unknown", query=reason, raw=raw)

    m = _OBSERVE_RE.match(raw)
    if m:
        rest = (m.group("rest") or "").strip().lower()
        # ``OBSERVE all`` / ``OBSERVE w3 all`` also lists the other windows; a
        # bare OBSERVE (with or without a window) is that window alone.
        desktop = rest in ("all", "desktop")
        # ``OBSERVE w5 full`` (or OBSERVE full) renders the window's ENTIRE
        # tree: no folding, no budget. ``all`` means "this window plus the
        # desktop list"; ``full`` is the escape hatch for a projection that
        # hid something the agent needs.
        full = rest == "full"
        expand = m.group("expand")
        return Action(
            kind="observe",
            window=m.group("window"),
            scope="desktop" if desktop else ("full" if full else "window"),
            # With an expand ref, ``query`` carries the ref to unfold; otherwise
            # it only means "windows" (list-windows-only).
            depth=1 if expand else None,
            query=expand or ("windows" if rest == "windows" else None),
            raw=raw,
        )

    # Give a precise error for forbidden primitive verbs.
    head = raw.split("(", 1)[0].split(".")[-1].strip().lower()
    if head in FORBIDDEN_VERBS:
        raise parse_error(f"primitive verb '{head}' is not allowed")
    raise parse_error(f"unrecognized action: {raw[:60]}")

def parse_find(text: str) -> Action:
    raw = text.strip()
    m = _FIND_RE.match(raw)
    if not m:
        raise parse_error(f"invalid FIND: {raw[:60]}")
    return _parse_find(m, raw)


def parse_inspect(text: str) -> Action:
    raw = text.strip()
    m = _INSPECT_RE.match(raw)
    if not m:
        raise parse_error(f"invalid INSPECT: {raw[:60]}")
    return _parse_inspect(m, raw)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _parse_call(m: re.Match[str], rev: int | None, raw: str) -> Action:
    ref = m.group("ref")
    cap = m.group("cap")
    if cap.lower() in FORBIDDEN_VERBS:
        raise parse_error(f"primitive verb '{cap}' is not allowed")
    args = _parse_args(m.group("args"))
    return Action(
        kind="call", ref=ref, capability=cap, args=args, rev=rev, raw=raw
    )


def _parse_find(m: re.Match[str], raw: str) -> Action:
    query = m.group("query").strip()
    if query[:1] in ("\"", "'"):
        query = query[1:-1]
    return Action(
        kind="find",
        query_type=(m.group("type") or "").lower() or None,
        query=query,
        # Both spellings resolve the same way: ``FIND w5 "库"`` and
        # ``FIND "库" in w5``. A leading ref is a *scope*, not a type -
        # ``FIND l1 "..."`` and ``FIND link "..."`` read ambiguously, so the
        # ref-shaped token always means "search inside this node/window".
        scope=m.group("scope") or m.group("scope2"),
        raw=raw,
    )


def _parse_inspect(m: re.Match[str], raw: str) -> Action:
    r0 = m.group("r0")
    r1 = m.group("r1")
    rng = (int(r0), int(r1)) if r0 is not None and r1 is not None else None
    return Action(
        kind="inspect",
        ref=m.group("ref"),
        depth=int(m.group("depth")) if m.group("depth") else None,
        range=rng,
        raw=raw,
    )


def _parse_args(text: str) -> dict[str, Any]:
    """Parse ``size="large", qty=2`` into a dict (positional args -> ``_0``...)."""
    args: dict[str, Any] = {}
    if not text.strip():
        return args
    position = 0
    for piece in _split_args(text):
        m = _ARG_RE.match(piece)
        if not m:
            raise parse_error(f"bad argument: {piece!r}")
        key, value = m.groups()
        parsed = _parse_value(value)
        if key is None:
            args[f"_{position}"] = parsed
            position += 1
        else:
            args[key] = parsed
    return args


def _split_args(text: str) -> list[str]:
    """Split on commas that are not inside quotes."""
    out: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for ch in text:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    if current:
        out.append("".join(current))
    return [p for p in (s.strip() for s in out) if p]


def _parse_value(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].replace('\\"', '"').replace("\\n", "\n")
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered in ("null", "none"):
        return None
    try:
        if "." in text or "e" in lowered:
            return float(text)
        return int(text)
    except ValueError:
        return text
