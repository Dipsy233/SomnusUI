"""Core IR node types.

Two trees exist:

* :class:`RawNode` - the platform provider's normalized output. It is the only
  place native identity (``NativeRef``) is allowed to live.
* :class:`SemanticNode` - the compiled node seen by projection and the runtime.
  It carries a session ``ref`` (LLM visible) plus a hidden ``source`` reference
  used exclusively by the resolver.

Nothing in this module imports providers, projections or an LLM.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Native identity (never serialized to the LLM)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class NativeRef:
    """Opaque handle to a native element.

    ``runtime_id`` is UIA's ``RuntimeId`` (or an equivalent stable native id on
    other platforms). It is a *fast path* for resolution only; identity is
    established by :attr:`SemanticNode.fingerprint`.
    """

    window_id: str
    runtime_id: tuple[int, ...] = ()
    automation_id: str | None = None
    class_name: str | None = None
    control_type: str | None = None
    path: tuple[int, ...] = ()

    def as_key(self) -> str:
        """A stable debug/repr key (safe for logs, never for the LLM)."""
        rid = ".".join(str(p) for p in self.runtime_id)
        return f"{self.window_id}/{self.automation_id or ''}/{self.class_name or ''}/{rid}"


@dataclass(slots=True)
class WindowInfo:
    """A top level window discovered by the provider."""

    window_id: str
    title: str
    process: str
    pid: int
    active: bool = False
    visible: bool = True
    rect: tuple[int, int, int, int] | None = None  # x, y, w, h (resolver only)


# ---------------------------------------------------------------------------
# Raw (provider) tree
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class RawNode:
    """Platform independent, provider-normalized node.

    ``role`` is already mapped to the small portable vocabulary (see
    :mod:`somnus.compiler.normalize`). ``states`` and ``patterns`` are
    lower-case strings so the raw layer stays serializable.
    """

    native: NativeRef
    role: str
    name: str | None = None
    value: str | None = None
    description: str | None = None
    states: frozenset[str] = frozenset()
    patterns: frozenset[str] = frozenset()
    children: list["RawNode"] = field(default_factory=list)
    #: True when the provider had to stop descending (virtualization, budget).
    truncated: bool = False
    #: Total child count when known and larger than the realized children.
    virtual_count: int | None = None

    def walk(self) -> Iterable["RawNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def find_native(self, native: NativeRef) -> "RawNode | None":
        for node in self.walk():
            if node.native == native:
                return node
        return None

@dataclass(slots=True)
class RawTree:
    """A full snapshot of a window (or scope) at one point in time."""

    window: WindowInfo
    root: RawNode
    #: Monotonic provider-side counter; bumps on any *raw* change.
    raw_revision: int = 0

    def walk(self) -> Iterable[RawNode]:
        return self.root.walk()


# ---------------------------------------------------------------------------
# Semantic graph
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class SourceRef:
    """Hidden link from a semantic node back to its native element."""

    native: NativeRef
    #: Native refs of the nodes a capability may act on (entity members).
    members: tuple[NativeRef, ...] = ()


@dataclass(slots=True, frozen=True)
class Relation:
    """A directed, semantic edge between two nodes."""

    kind: str  # contains | label_for | acts_on | controls | selected_in | opens
    target: str  # ref of the other node

    def render(self) -> str:
        return f"{self.kind}={self.target}"


@dataclass(slots=True)
class SemanticNode:
    """A compiled node - the unit of the LLM-facing semantic API."""

    ref: str
    role: str
    layer: str | None = None
    entity: str | None = None
    name: str | None = None
    value: Any | None = None
    states: frozenset[str] = frozenset()
    attrs: dict[str, Any] = field(default_factory=dict)
    caps: list["Any"] = field(default_factory=list)  # list[Capability] (avoid cycle)
    relations: list[Relation] = field(default_factory=list)
    confidence: float = 1.0
    fingerprint: str = ""
    source: SourceRef | None = None
    children: list["SemanticNode"] = field(default_factory=list)
    truncated: bool = False
    virtual_count: int | None = None

    # -- convenience --------------------------------------------------------

    @property
    def display_type(self) -> str:
        """The uppercase token used as the leading SUI-Text node type."""
        if self.entity:
            return self.entity.upper()
        if self.layer:
            return self.layer.upper()
        return self.role.upper()

    def cap_names(self) -> set[str]:
        return {c.name for c in self.caps if hasattr(c, "name")}

    def get_cap(self, name: str):
        for cap in self.caps:
            if getattr(cap, "name", None) == name:
                return cap
        return None

    def walk(self) -> Iterable["SemanticNode"]:
        yield self
        for child in self.children:
            yield from child.walk()

    def iter_refs(self) -> Iterable[str]:
        for node in self.walk():
            yield node.ref


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def compute_fingerprint(
    *,
    entity: str | None,
    role: str,
    identity: str | None,
    name: str | None,
    ancestor_path: str = "",
    role_index: int | None = None,
) -> str:
    """Build a stable identity hash for a semantic node.

    Identity evidence, strongest first:

    1. ``identity`` - pack primary key or ``automation_id`` (locale proof).
    2. ``name`` - normalized (casefold, whitespace collapsed), **only when no
       stable identity exists**. A stable identity makes the name volatile
       evidence and it is deliberately excluded, so mutable labels such as a
       cart total never invalidate the ref.
    3. ``ancestor_path`` - semantic ancestor path as a last differentiator.
    4. ``role_index`` - position among same-role siblings, used only when the
       evidence above is empty. Without it, unnamed containers (every message
       list, every wrapper pane) hash identically and the registry reports them
       as ambiguous, which makes a whole app unoperable. It is role-relative,
       not absolute, so adding a sibling of another role does not shift it.

    The runtime deliberately excludes ``value`` and mutable state: those change
    constantly and would make every ref stale (see dev doc section 8).
    """

    def norm(value: str | None) -> str:
        if not value:
            return ""
        return " ".join(value.split()).casefold()

    stable = bool(identity)
    # A caller-supplied index means "these siblings would otherwise collide", so
    # it takes part regardless of the name: two buttons both named "更多" are
    # distinct elements, and without this they would share a fingerprint and make
    # each other unaddressable.
    if role_index is not None:
        index_part = str(role_index)
    elif stable or norm(name):
        index_part = ""
    else:
        index_part = ""
    payload = "\x1f".join(
        (
            entity or "",
            role or "",
            norm(identity),
            "" if stable else norm(name),
            norm(ancestor_path),
            index_part,
        )
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
