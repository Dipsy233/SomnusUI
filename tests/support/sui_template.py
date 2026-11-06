"""SUI-Text snapshot templates for tests.

A template is the same SUI-Text the runtime emits, with two additions:

* a ref may be ``?`` (any ref) or ``x?`` (any ref starting with ``x``);
* a name may be ``/regex/`` instead of a quoted literal;
* a line containing only ``...`` allows any remaining children.

Everything written in the template is asserted; everything omitted is not:

* node type must be equal;
* written attributes, states (flags) and capabilities must be present;
* child lists are matched in order, and extra children are allowed (``contain``);
* a template with a single top-level node matches that node at any depth.

This is a test-only safety net. It lives under ``tests/`` on purpose: nothing
in ``somnus/`` imports it.

``strict=True`` degenerates to the existing whole-file golden comparison.

``UPDATE_SUI_TEMPLATES=1`` rewrites a template from the current output. It
exists for bootstrapping a new screen; a curated template loses its wildcards
when that runs, so normal changes go through a reviewed template edit.

Lexical rules are not reinvented here: quoted strings are decoded by the
SUI-Text parser's own escape table, and states come from the serializer's
``FLAG_ORDER``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from somnus.sui.parse import _unescape
from somnus.sui.serialize import FLAG_ORDER

_FLAGS = frozenset(FLAG_ORDER)
_REF_RE = re.compile(r"^[a-z]{1,3}\d+$")
_TEMPLATE_REF_RE = re.compile(r"^[a-z]{1,3}\?$")
_REV_RE = re.compile(r"^@rev\s+(\d+|\?)\s*$")
_COLLAPSE_RE = re.compile(r"^\+(?P<count>\d+|\?) more$")
_CAP_RE = re.compile(r"^\+([A-Za-z_][A-Za-z0-9_]*)(?:\(.*\))?!?$")


# ---------------------------------------------------------------------------
# model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Attr:
    op: str  # "=", ">=", "?"
    value: object


@dataclass(slots=True)
class _Node:
    kind: str
    ref: str | None = None
    name: str | None = None
    name_pattern: re.Pattern[str] | None = None
    role: str | None = None  # set on an UNKNOWN (`?`) line
    attrs: dict[str, _Attr] = field(default_factory=dict)
    flags: set[str] = field(default_factory=set)
    caps: set[str] = field(default_factory=set)
    collapse: tuple[str, int | None] | None = None
    children: list["_Node"] = field(default_factory=list)
    extras: list[str] = field(default_factory=list)
    depth: int = 0


@dataclass(slots=True)
class _Snapshot:
    rev: int | None
    nodes: list[_Node]


# ---------------------------------------------------------------------------
# lexing
# ---------------------------------------------------------------------------


def _tokenize(line: str) -> list[str]:
    """Split a node line into tokens, respecting quotes, parens and /regex/."""
    tokens: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    depth = 0
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and i + 1 < n:
                buf.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ('"', "'"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and not buf:
            # A name regex may contain spaces; keep it one token.
            token = [ch]
            j = i + 1
            while j < n:
                token.append(line[j])
                if line[j] == "\\" and j + 1 < n:
                    token.append(line[j + 1])
                    j += 2
                    continue
                if line[j] == "/":
                    j += 1
                    break
                j += 1
            tokens.append("".join(token))
            i = j
            continue
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch.isspace() and depth == 0:
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _looks_like_ref(token: str) -> bool:
    return bool(_REF_RE.fullmatch(token) or _TEMPLATE_REF_RE.fullmatch(token)) or token == "?"


def _is_regex_token(token: str) -> bool:
    return len(token) >= 2 and token.startswith("/") and token.endswith("/")


def _find_unquoted(text: str, needle: str) -> int:
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in ('"', "'"):
            quote = ch
        elif ch == needle:
            return i
        i += 1
    return -1


def _parse_scalar(text: str) -> object:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return _unescape(text[1:-1])
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        if "." in text or "e" in lowered:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _split_attr(token: str) -> tuple[str, _Attr] | None:
    eq = _find_unquoted(token, "=")
    if eq <= 0:
        return None
    key = token[:eq]
    op = "="
    if key.endswith(">"):
        key = key[:-1]
        op = ">="
    raw = token[eq + 1 :]
    if raw == "?":
        return key, _Attr("?", None)
    return key, _Attr(op, _parse_scalar(raw))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def _parse_line(content: str, depth: int) -> _Node:
    if content == "...":
        return _Node(kind="...", depth=depth)

    tokens = _tokenize(content)
    if not tokens:
        raise ValueError("empty SUI-Text line")

    if tokens[0] == "?":
        node = _Node(
            kind="?",
            ref=tokens[1] if len(tokens) > 1 else None,
            role=tokens[2] if len(tokens) > 2 else None,
            depth=depth,
        )
        _consume(node, tokens[3:])
        return node

    if tokens[0].startswith("\u2026"):
        return _Node(kind="\u2026", depth=depth, extras=tokens[1:])

    node = _Node(kind=tokens[0].upper(), depth=depth)
    index = 1
    if index < len(tokens) and _looks_like_ref(tokens[index]):
        node.ref = tokens[index]
        index += 1

    if index < len(tokens):
        token = tokens[index]
        if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
            node.name = _unescape(token[1:-1])
            index += 1
        elif _is_regex_token(token):
            try:
                node.name_pattern = re.compile(token[1:-1])
            except re.error as exc:
                raise ValueError(f"bad name regex {token!r}: {exc}") from exc
            index += 1

    _consume(node, tokens[index:])
    return node


def _consume(node: _Node, tokens: list[str]) -> None:
    index = 0
    while index < len(tokens):
        token = tokens[index]
        # The tokenizer splits `+240 more` at its space; the pair is one marker.
        if index + 1 < len(tokens) and tokens[index + 1] == "more":
            collapse = re.fullmatch(r"\+(\d+|\?)", token)
            if collapse:
                raw = collapse.group(1)
                node.collapse = ("?", None) if raw == "?" else ("=", int(raw))
                index += 2
                continue
        cap = _CAP_RE.fullmatch(token)
        if cap and token.startswith("+"):
            node.caps.add(cap.group(1))
            index += 1
            continue
        attr = _split_attr(token)
        if attr is not None:
            node.attrs[attr[0]] = attr[1]
            index += 1
            continue
        if token in _FLAGS:
            node.flags.add(token)
            index += 1
            continue
        node.extras.append(token)
        index += 1


def _parse(text: str) -> _Snapshot:
    rev: int | None = None
    roots: list[_Node] = []
    stack: list[_Node] = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        depth = indent // 2
        content = raw.strip()
        if content.startswith("@rev"):
            match = _REV_RE.fullmatch(content)
            if not match:
                raise ValueError(f"bad header {content!r}: want `@rev N` or `@rev ?`")
            rev = None if match.group(1) == "?" else int(match.group(1))
            continue
        node = _parse_line(content, depth)
        while stack and stack[-1].depth >= depth:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return _Snapshot(rev=rev, nodes=roots)


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------


def _ref_matches(template: str | None, actual: str | None) -> bool:
    if template is None:
        return True
    if template == "?":
        return actual is not None
    if template.endswith("?"):
        return actual is not None and actual.startswith(template[:-1])
    return actual == template


def _name_matches(template: _Node, actual: _Node) -> bool:
    if template.name_pattern is not None:
        return actual.name is not None and bool(
            template.name_pattern.search(actual.name)
        )
    if template.name is not None:
        return actual.name == template.name
    return True


def _attr_matches(template: _Attr, actual: _Attr | None) -> bool:
    if actual is None:
        return False
    if template.op == "?":
        return True
    if template.op == ">=":
        left = _as_number(template.value)
        right = _as_number(actual.value)
        if left is None or right is None:
            return False
        return actual.op in ("=", ">=") and right >= left
    if actual.op != "=":
        return False
    left = _as_number(template.value)
    right = _as_number(actual.value)
    if left is not None and right is not None:
        return left == right
    return template.value == actual.value


def _as_number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _collapse_matches(
    template: tuple[str, int | None] | None,
    actual: tuple[str, int | None] | None,
) -> bool:
    if template is None:
        return True
    if actual is None:
        return False
    if template[0] == "?":
        return True
    return actual[1] == template[1]


def _node_matches(template: _Node, actual: _Node) -> bool:
    if template.kind != actual.kind:
        return False
    if not _ref_matches(template.ref, actual.ref):
        return False
    if template.kind == "?":
        if template.role is not None and template.role != "?":
            if actual.role != template.role:
                return False
    elif not _name_matches(template, actual):
        return False
    for key, expected in template.attrs.items():
        if not _attr_matches(expected, actual.attrs.get(key)):
            return False
    if not template.flags <= actual.flags:
        return False
    if not template.caps <= actual.caps:
        return False
    if not _collapse_matches(template.collapse, actual.collapse):
        return False
    return _sequence_matches(template.children, actual.children)


def _sequence_matches(template: list[_Node], actual: list[_Node]) -> bool:
    """Ordered contain match; ``...`` consumes any run of actual nodes."""
    memo: dict[tuple[int, int], bool] = {}

    def match(ti: int, ai: int) -> bool:
        if ti == len(template):
            return True
        key = (ti, ai)
        if key in memo:
            return memo[key]
        item = template[ti]
        if item.kind == "...":
            result = any(match(ti + 1, j) for j in range(ai, len(actual) + 1))
        else:
            result = False
            for j in range(ai, len(actual)):
                if _node_matches(item, actual[j]) and match(ti + 1, j + 1):
                    result = True
                    break
        memo[key] = result
        return result

    return match(0, 0)


def _walk(nodes: list[_Node]):
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _matches(actual_text: str, template_text: str) -> bool:
    template = _parse(template_text)
    actual = _parse(actual_text)
    if template.rev is not None and template.rev != actual.rev:
        return False
    rendered = [node for node in template.nodes if node.kind != "..."]
    if len(rendered) == 1:
        # A single top-level node is a fragment: it may sit at any depth.
        return any(_node_matches(rendered[0], node) for node in _walk(actual.nodes))
    return _sequence_matches(template.nodes, actual.nodes)


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def matches(actual: str, template: str) -> bool:
    """True when ``actual`` satisfies ``template`` (no assertion, no file I/O)."""
    return _matches(actual, template)


def assert_sui(actual: str, template: str | Path, *, strict: bool = False) -> None:
    """Assert that ``actual`` SUI-Text satisfies ``template``.

    ``template`` may be the template text or a path. With ``strict=True`` the
    text must match verbatim, which is the existing whole-file golden rule.

    When ``UPDATE_SUI_TEMPLATES`` is set and ``template`` is a path, the file
    is rewritten from ``actual`` and the assertion passes.
    """
    path = template if isinstance(template, Path) else None
    if path is not None and os.environ.get("UPDATE_SUI_TEMPLATES", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        path.write_text(actual.rstrip("\n") + "\n", encoding="utf-8")
        return

    expected = path.read_text(encoding="utf-8") if path else str(template)
    if strict:
        if actual.rstrip("\n") != expected.rstrip("\n"):
            raise AssertionError(_message("SUI-Text is not identical to the golden", actual, expected))
        return
    if not _matches(actual, expected):
        raise AssertionError(_message("SUI-Text does not satisfy the template", actual, expected))


def _message(title: str, actual: str, expected: str) -> str:
    return f"{title}\n--- expected ---\n{expected.rstrip()}\n--- actual ---\n{actual.rstrip()}"
