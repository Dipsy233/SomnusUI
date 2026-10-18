"""SUI-Text action parsing.

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
    re.VERBOSE | re.IGNORECASE | re.DOTALL,
)

#: ``ref.cap(args)`` with the CALL keyword omitted (tool-boundary tolerance).
_BARE_CALL_RE = re.compile(
    r"^[a-z]{1,3}[0-9]+\s*\.\s*[A-Za-z_][A-Za-z0-9_]*\s*\(.*\)\s*$",
    re.IGNORECASE | re.DOTALL,
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

#: ``FIND ... in l1 scroll`` - the virtualised-list variant (SomnusDroid doc
#: §7.3). The trailing ``scroll`` is peeled before the plain FIND regex so the
#: same syntax serves both; ``Action.scope_scroll`` carries the flag.
_FIND_SCROLL_RE = re.compile(
    r"^(?P<body>.+?(?:\s+in\s+[a-z]{1,3}[0-9]+)?)\s+scroll\s*$",
    re.IGNORECASE,
)

_OBSERVE_RE = re.compile(
    r"^OBSERVE(?:\s+(?P<window>[a-z]{1,3}[0-9]+))?"
    r"(?:\s+(?P<rest>windows|desktop|full|all))?"
    r"(?:\s+expand\s+(?P<expand>[a-z]{1,3}[0-9]+))?\s*$",
    re.IGNORECASE,
)

#: ``SWITCH main`` / ``SWITCH child`` - retarget the agent at another Windows
#: session. The agent drives one session at a time; this is how it moves.
#: ``SESSION open`` / ``SESSION close`` create and end the child session.
_SWITCH_RE = re.compile(
    r"^SWITCH\s+(?P<target>main|local|host|primary|child|other|remote|sub"
    r"|home|back|next|prev|recents|notifications|quick_settings)\s*$",
    re.IGNORECASE,
)

#: ``SWITCH launch <app>`` - open an app through the platform's own launch
#: API (PackageManager), never by clicking icons in a tree. The verb is
#: deliberately `launch`, not `open`: `+open` is a capability some controls
#: print, and the two must not read as the same gesture.
_SWITCH_LAUNCH_RE = re.compile(
    r"^SWITCH\s+launch\s+(?P<app>\S+)\s*$",
    re.IGNORECASE,
)
_SESSION_RE = re.compile(
    r"^SESSION\s+(?P<op>open|close|detach)\s*$",
    re.IGNORECASE,
)

#: ``SYSTEM back|home|recents|notifications`` - Android global actions
#: (SomnusDroid doc §6.1). Replaces the Windows ``SWITCH`` surface on the
#: Android provider; the parser is shared, so the action kinds are distinct.
_SYSTEM_RE = re.compile(
    r"^SYSTEM\s+(?P<op>back|home|recents|notifications|quick_settings)\s*$",
    re.IGNORECASE,
)

#: ``APP launch "<label or package>"`` / ``APP search "..."`` - launch an app
#: through the platform's own API, no tree needed. The verb is `launch`, not
#: `open`: `+open` is a capability some controls print, and the two must not
#: read as the same gesture.
_APP_RE = re.compile(
    r"^APP\s+(?P<op>launch|search)\s+(?P<query>\"(?:[^\"\\]|\\.)*\"|\S+)\s*$",
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

_ARG_RE = re.compile(
    r'^\s*(?:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*)?(.+?)\s*$', re.DOTALL
)


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

    # ``FIND "x" in l1 scroll`` (or scope-less ``FIND "x" scroll``): peel the
    # suffix and reparse the body.
    m = _FIND_SCROLL_RE.match(raw)
    if m:
        action = parse_action(m.group("body"), rev)
        if action.kind != "find":
            raise parse_error("FIND scroll requires FIND syntax")
        action.scope_scroll = True
        action.raw = raw
        return action

    m = _INSPECT_RE.match(raw)
    if m:
        return _parse_inspect(m, raw)

    m = _UNKNOWN_RE.match(raw)
    if m:
        reason = m.group("reason").strip('"')
        return Action(kind="unknown", query=reason, raw=raw)

    m = _SWITCH_LAUNCH_RE.match(raw)
    if m:
        return Action(
            kind="switch", query="launch", scope=m.group("app"), raw=raw
        )

    m = _SWITCH_RE.match(raw)
    if m:
        return Action(kind="switch", query=m.group("target").lower(), raw=raw)

    m = _SESSION_RE.match(raw)
    if m:
        return Action(kind="session", query=m.group("op").lower(), raw=raw)

    m = _SYSTEM_RE.match(raw)
    if m:
        return Action(kind="system", query=m.group("op").lower(), raw=raw)

    m = _APP_RE.match(raw)
    if m:
        query = m.group("query").strip()
        if query[:1] in ('"', "'"):
            query = query[1:-1]
        return Action(
            kind="app", query=query, scope=m.group("op").lower(), raw=raw
        )

    m = _OBSERVE_RE.match(raw)
    if m:
        rest = (m.group("rest") or "").strip().lower()
        # ``OBSERVE desktop`` lists the other windows; a bare OBSERVE (with or
        # without a window) is that window alone. The old ``all`` spelling is
        # accepted and treated as ``desktop`` - it carried the same meaning and
        # older prompts may still use it, but it is no longer documented.
        desktop = rest in ("desktop", "all")
        # ``OBSERVE w5 full`` (or OBSERVE full) renders the window's ENTIRE
        # tree: no folding, no budget. ``desktop`` means "this window plus the
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
    scroll = False
    m = _FIND_SCROLL_RE.match(raw)
    if m:
        raw = m.group("body")
        scroll = True
    m = _FIND_RE.match(raw)
    if not m:
        raise parse_error(f"invalid FIND: {text[:60]}")
    action = _parse_find(m, raw)
    action.scope_scroll = scroll
    return action


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


def _unescape(text: str) -> str:
    """Decode the two escapes a caller can actually mean inside quotes.

    Only ``\\\\`` -> ``\\`` and ``\\"`` -> ``"`` are decoded. Everything else -
    ``\\n``, ``\\t``, ``\\r``, ``\\q`` - is kept **verbatim**, backslash included.

    Why ``\\n`` is no longer a newline escape
    -----------------------------------------
    The MCP tool surface takes the call as a JSON string, and JSON has its own
    escapes: a newline arrives as an already-decoded line feed, and a literal
    backslash arrives as a single backslash. Decoding ``\\n`` *again* here would
    apply a second, unrelated escape pass to data that is already literal, and
    a Windows path is what dies first: ``D:\\notes.txt`` contains the characters
    ``\\`` ``n``, and turning those into a line feed corrupts the single most
    common argument this project passes (every Save-As dialog, every open-path).

    ``\\t`` and ``\\r`` were already exempt for exactly that reason; ``\\n`` is
    the same case and is now treated the same way. Multi-line text needs no
    escape here at all: it arrives through JSON as a real line feed, or through
    a shell as a real line feed. A caller that genuinely wants a backslash
    followed by ``n`` writes ``\\\\n``.

    This is deliberately *not* an exact inverse of
    :func:`somnus.sui.serialize._quote`. The serializer still emits ``\\n`` for
    a real newline, and the resulting text stays correct when it is only ever
    *displayed*. It stops being re-parseable as an argument, which is the right
    trade: the model reads serialized text far more often than it feeds
    serializer output back into a ``CALL``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "\\":
            out.append("\\")
            i += 2
        elif nxt == '"':
            out.append('"')
            i += 2
        else:
            # Every other escape - including \n, \t, \r - is kept verbatim so a
            # Windows path survives. Consume only the backslash: the following
            # character is ordinary text and may itself start another escape.
            out.append(ch)
            i += 1
    return "".join(out)


def _parse_value(text: str) -> Any:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return _unescape(text[1:-1])
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
