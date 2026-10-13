"""App Packs: domain vocabulary on top of the primitive role tree.

v0.1 ships packs as Python modules (dev doc section 15). A pack declares
*entities* - named domain objects matched by a deterministic rule set - and the
semantic capabilities each entity exposes.

The match language is intentionally tiny and data-driven so it can be ported to
YAML in v0.2 without touching the compiler::

    MatchRule:
      role: "group"                    # required portable role
      has: [MatchRule, ...]            # required descendants, named via `as`
      name: "regex"                    # optional regex on the node name
      min_children: 1
      where: callable                  # escape hatch, deterministic only

    EntityDef:
      name: "product"
      match: MatchRule
      key: "$title.automation_id"      # fingerprint primary key expression
      attrs: {"name": "$title.name", "price": {"from": "...", "regex": "..."}}
      caps: {"add": CapabilityDef}
      aliases: {"Big Mac": ["巨无霸"]}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from somnus.ir.capability import Capability, Effect, ParamSpec, PrimitiveStep, Risk
from somnus.ir.node import RawNode

# ---------------------------------------------------------------------------
# Match language
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MatchRule:
    """A deterministic structural predicate over a :class:`RawNode` subtree."""

    #: Portable role, or a tuple of acceptable roles (``({"group", "listitem"})``).
    role: str | tuple[str, ...] | None = None
    name: str | None = None  # regex
    value: str | None = None  # regex
    has_name: bool | None = None
    min_children: int | None = None
    #: Pattern names that must all be present (e.g. ``("value", "text")``).
    has_patterns: tuple[str, ...] = ()
    #: Required descendant matches; ``as`` binds the matched node as a member.
    has: list["MatchRule"] = field(default_factory=list)
    #: Extra predicate over the node (must be pure).
    where: Callable[[RawNode], bool] | None = None

    def matches(self, node: RawNode) -> bool:
        if self.role:
            roles = self.role if isinstance(self.role, tuple) else (self.role,)
            if node.role not in roles:
                return False
        if self.name and not _re_search(self.name, node.name):
            return False
        if self.value and not _re_search(self.value, node.value):
            return False
        if self.has_name is not None and bool(node.name) != self.has_name:
            return False
        if self.min_children is not None and len(node.children) < self.min_children:
            return False
        for pattern in self.has_patterns:
            if pattern not in node.patterns:
                return False
        if self.where and not self.where(node):
            return False
        for sub in self.has:
            if not any(sub.matches(c) for c in _descendants(node) if c is not node):
                return False
        return True

    def bind(self, node: RawNode) -> dict[str, RawNode]:
        """Bind ``as`` names to matching descendants."""
        out: dict[str, RawNode] = {}
        for sub in self.has:
            for cand in _descendants(node):
                if cand is node:
                    continue
                if sub.matches(cand):
                    if sub.alias:
                        out[sub.alias] = cand
                    out.update(sub.bind(cand))
                    break
        return out

    #: Set when this rule is used inside ``has``.
    alias: str | None = None


def _descendants(node: RawNode) -> Iterable[RawNode]:
    yield node
    for child in node.children:
        yield from _descendants(child)


def regex_search(pattern: str, text: str | None) -> bool:
    """Match ``text`` against ``pattern``.

    Accepts a bare regex, or the documented ``/regex/flags`` form
    (dev doc section 15: ``name: "/^(add|\\u52a0\\u5165)$/i"``).
    """
    if text is None:
        return False
    # A bare regex is case-insensitive (historical behaviour, and the right
    # default for UI labels); the ``/regex/flags`` form is explicit.
    flags = re.IGNORECASE
    if len(pattern) >= 2 and pattern.startswith("/"):
        end = pattern.rfind("/")
        if end > 0:
            body = pattern[1:end]
            flags = 0
            for f in pattern[end + 1 :]:
                if f == "i":
                    flags |= re.IGNORECASE
                elif f == "s":
                    flags |= re.DOTALL
                elif f == "m":
                    flags |= re.MULTILINE
            pattern = body
    try:
        return re.search(pattern, text, flags) is not None
    except re.error:
        return pattern.lower() in text.lower()


_re_search = regex_search


# ---------------------------------------------------------------------------
# Entity definitions
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EntityCapDef:
    """Declaration of a semantic capability on an entity."""

    name: str
    via: list[str] = field(default_factory=list)  # e.g. ["$add_btn.activate"]
    effects: list[str] = field(default_factory=list)  # "cart.count += 1"
    params: dict[str, ParamSpec] = field(default_factory=dict)
    risk: Risk = "normal"
    aliases: tuple[str, ...] = ()


@dataclass(slots=True)
class EntityDef:
    """A domain entity matched by a pack."""

    name: str
    match: MatchRule
    key: str | None = None  # fingerprint primary key expression
    attrs: dict[str, Any] = field(default_factory=dict)
    caps: dict[str, EntityCapDef] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=dict)
    confidence: float = 1.0
    #: Optional role upgrade: entity nodes are rendered with this role.
    role_override: str | None = None


@dataclass(slots=True)
class ProjectionRule:
    select: str  # e.g. "role=toolbar", "name=Navigation"
    rule: str  # keep | flatten | prune | collapse


@dataclass(slots=True)
class Pack:
    """A bundle of entity definitions, projection rules and window matching."""

    id: str
    match_process: tuple[str, ...] = ()
    match_window_class: tuple[str, ...] = ()
    entities: list[EntityDef] = field(default_factory=list)
    projection: list[ProjectionRule] = field(default_factory=list)
    #: Named refs the pack expects to find (e.g. "cart" -> rule).
    named_refs: dict[str, MatchRule] = field(default_factory=dict)
    version: str = "0.1.0"

    def matches_window(self, process: str, window_class: str = "") -> bool:
        if self.match_process:
            proc = process.lower()
            if not any(p == "*" or p.lower() in proc for p in self.match_process):
                return False
        if self.match_window_class:
            wc = window_class.lower()
            if not any(c == "*" or c.lower() in wc for c in self.match_window_class):
                return False
        return bool(self.match_process or self.match_window_class)

    def find_entity_def(self, name: str) -> EntityDef | None:
        for e in self.entities:
            if e.name == name:
                return e
        return None


# ---------------------------------------------------------------------------
# Pack registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Pack] = {}


def register(pack: Pack) -> Pack:
    _REGISTRY[pack.id] = pack
    return pack


def get_pack(pack_id: str) -> Pack | None:
    return _REGISTRY.get(pack_id)


def all_packs() -> list[Pack]:
    return list(_REGISTRY.values())


def select_pack(process: str, window_class: str = "", forced: str | None = None) -> Pack | None:
    """Pick the most specific pack for a window.

    Exact (non-wildcard) process matches win over the catch-all generic pack, so
    a dedicated app pack is never shadowed by it (dev doc sections 11.3, 15).
    """
    if forced:
        return get_pack(forced)
    candidates = [
        p
        for p in _REGISTRY.values()
        if "*" not in p.match_process and p.matches_window(process, window_class)
    ]
    if not candidates:
        return None
    return candidates[0]


def _load_builtin() -> None:
    # v0.1 Python packs (kept for the acceptance demo) ...
    from somnus.packs import generic, restaurant_demo  # noqa: F401

    # ... plus v0.2 YAML packs (dev doc section 15). Loading is best-effort:
    # a missing PyYAML simply leaves the Python packs in place.
    try:
        from somnus.compiler.yaml_pack import load_builtin_packs

        load_builtin_packs()
    except Exception:  # pragma: no cover - optional dependency
        import logging

        logging.getLogger("somnus.packs").debug("YAML packs unavailable", exc_info=True)


_load_builtin()
