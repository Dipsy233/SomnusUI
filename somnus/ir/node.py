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
    #: The element's own visible label (text or contentDescription) at
    #: snapshot time. Android's Portal re-resolves a node key by walking the
    #: live tree by index - after a re-render the path can land on a stranger
    #: - so the label rides along as the identity the Portal verifies before
    #: acting (a mismatch is STALE_TARGET, never a mis-fire). Not part of the
    #: fingerprint: fingerprint evidence rules live in compute_fingerprint.
    label: str | None = None
    #: The element's own Win32 window handle, or 0 when it maps none.
    #:
    #: This is the precondition for the ``WM_SETTEXT`` / ``EM_REPLACESEL`` write
    #: channel, and it is *per element* - not "does the process have an Edit
    #: child window", which is the wrong question that made the capability
    #: report rule the channel out for whole applications at once
    #: (docs/text-input-channels.md 3.2). Measured zero-focus: Notepad
    #: stays in the background and its text is still replaced, 4/4 runs
    #: including CJK and a 300-character string.
    #:
    #: Only a small minority of elements carry one - measured 25 of 357 across
    #: all open windows - and they are mostly window-level scaffolding. It
    #: matters precisely because an editable control that has one is writable
    #: without focus, and one that does not is not.
    native_window_handle: int = 0

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
    #: The window's native handle, 0 when the provider cannot report one.
    #:
    #: Identity for a window id: two windows of one process can share a title
    #: (two Explorer windows on the same folder), and enumeration order is not
    #: stable - raising a window for rendering can reorder the walk. The hwnd
    #: is what makes a window id keep naming the same window across both.
    hwnd: int = 0


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
        """The node a native ref names, or None.

        Identity is compared on the whole ref, which makes the *path* the
        tie-breaker. That matters because a fixture routinely gives every node
        the same runtime id (the compiler's refs are what distinguish them),
        so matching on the runtime id alone would return the first node that
        happens to share it - the root, typically - and a caller would act on
        the wrong element. The most specific match wins.
        """
        best: RawNode | None = None
        best_score = -1
        for node in self.walk():
            if node.native != native:
                continue
            score = (
                (1 if node.native.path == native.path else 0)
                + (1 if node.native.automation_id == native.automation_id else 0)
                + (1 if node.native.class_name == native.class_name else 0)
            )
            if score > best_score:
                best, best_score = node, score
        return best

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
    #: Native pattern names the source element exposed, carried through so the
    #: resolver can gate alternative `via` steps (Value pattern vs keyboard)
    #: without re-reading the raw tree. Hidden from the LLM.
    patterns: frozenset[str] = frozenset()
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
    runtime_id: tuple[int, ...] = (),
) -> str:
    """Build a stable identity hash for a semantic node.

    Identity evidence, strongest first:

    1. ``identity`` - pack primary key or ``automation_id`` (locale proof).
    2. ``name`` - normalized (casefold, whitespace collapsed), **only when no
       stable identity exists**. A stable identity makes the name volatile
       evidence and it is deliberately excluded, so mutable labels such as a
       cart total never invalidate the ref.
    3. ``runtime_id`` - the element's own native id, as the *last* resort before
       falling back to position. It is stable for as long as the element lives
       and, unlike a sibling index, it does not change when the enumeration
       order does.
    4. ``role_index`` - position among same-role siblings, used only when the
       evidence above is empty. Without it, unnamed containers (every message
       list, every wrapper pane) hash identically and the registry reports them
       as ambiguous, which makes a whole app unoperable. It is role-relative,
       not absolute, so adding a sibling of another role does not shift it.
    5. ``ancestor_path`` - semantic ancestor path as a last differentiator.

    **Why (3) exists.** For an element with no identity and no name, the only
    remaining evidence used to be its position. That is not an identity: UIA
    returns children in whatever order the application's own provider chooses,
    and raising a window for rendering (which the runtime does before every
    snapshot, so that self-drawn trees are complete at all) can change that
    order. Measured on a synthesised tree: swapping two unnamed buttons kept
    the refs ``b1``/``b3`` but pointed each at the other's element - a silent
    retarget, and the registry reported it as "kept" because the fingerprints
    matched each other. The runtime-id suffix removes the position dependence
    entirely where the provider supplies one; position remains as the final
    fallback for elements that expose no id at all.

    The runtime deliberately excludes ``value`` and mutable state: those change
    constantly and would make every ref stale.
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
    # The native id's last segment distinguishes siblings without depending on
    # where they sit in the enumeration. Only used when nothing stronger exists,
    # so a node with a stable identity or a name keeps its position-independent
    # fingerprint and an element that merely *repaints* (same runtime id) keeps
    # its ref.
    rid_part = ""
    if not stable and not norm(name) and runtime_id:
        rid_part = ".".join(str(p) for p in runtime_id[-1:])
    # When the element's own id is available, position evidence is *dropped*:
    # `ancestor_path` embeds each ancestor's own sibling index, so a reordered
    # sibling list would change the path - and therefore the fingerprint - of
    # every descendant, which is precisely the retargeting this guards against.
    # The id is the better evidence and the two must not be mixed.
    position_free = bool(rid_part)
    payload = "\x1f".join(
        (
            entity or "",
            role or "",
            norm(identity),
            "" if stable else norm(name),
            "" if position_free else norm(ancestor_path),
            "" if position_free else index_part,
            rid_part,
        )
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()
