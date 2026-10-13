"""The compiler orchestrator: RawTree -> SemanticNode tree (+ ref bindings).

Deterministic, LLM-free. Owns the ref allocation order and the fingerprint
computation so that the runtime registry can reconcile refs across revisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from somnus.compiler.caps import primitive_capabilities
from somnus.compiler.match import (
    EntityMatch,
    entity_capabilities,
    heuristic_entities,
    match_entities,
)
from somnus.compiler.normalize import normalize
from somnus.compiler.pack_schema import Pack
from somnus.compiler.relations import build_relations
from somnus.compiler.simplify import auto_collapse_groups, simplify
from somnus.ir.capability import Capability
from somnus.ir.node import (
    RawNode,
    RawTree,
    Relation,
    SemanticNode,
    SourceRef,
    compute_fingerprint,
)

#: Ref prefix per entity/role, chosen so refs are readable and stable.
REF_PREFIX = {
    "product": "p",
    "cart": "c",
    "cartitem": "i",
    "checkout": "b",
    "item": "p",
    "dialog": "d",
    "message": "m",
    "email": "e",
    "file": "f",
}
ROLE_REF_PREFIX = {
    "button": "b",
    "input": "q",
    "checkbox": "cb",
    "radio": "r",
    "switch": "sw",
    "slider": "sl",
    "select": "sel",
    "menu": "mn",
    "menuitem": "mi",
    "tab": "t",
    "tree": "tr",
    "treeitem": "ti",
    "list": "l",
    "listitem": "li",
    "table": "tb",
    "row": "rw",
    "link": "lnk",
    "text": "tx",
    "image": "im",
    "canvas": "cv",
    "group": "g",
    "pane": "pn",
    "window": "w",
    "dialog": "d",
    "toolbar": "tl",
    "progressbar": "pg",
}

REVERSED_REF_PREFIX = {v: k for k, v in {**ROLE_REF_PREFIX, **REF_PREFIX}.items()}


@dataclass(slots=True)
class RefAllocator:
    """Allocates short refs like ``p1``, ``cb2`` with per-prefix counters."""

    counters: dict[str, int] = field(default_factory=dict)

    def next(self, prefix: str) -> str:
        n = self.counters.get(prefix, 0) + 1
        self.counters[prefix] = n
        return f"{prefix}{n}"

    def observe_existing(self, ref: str) -> None:
        """Make sure a reused ref's counter stays ahead (refs never collide)."""
        import re

        m = re.fullmatch(r"([a-z]+)(\d+)", ref)
        if not m:
            return
        prefix, num = m.group(1), int(m.group(2))
        if self.counters.get(prefix, 0) < num:
            self.counters[prefix] = num


@dataclass(slots=True)
class CompileResult:
    """Output of one compilation pass."""

    window_id: str
    process: str
    window_title: str
    root: SemanticNode
    #: ref -> native element(s), visible to the resolver only.
    sources: dict[str, SourceRef] = field(default_factory=dict)
    #: ref -> fingerprint
    fingerprints: dict[str, str] = field(default_factory=dict)
    #: id(raw_node) -> ref, used for relation + member resolution.
    ref_by_raw: dict[int, str] = field(default_factory=dict)
    #: ref -> members' refs (entity internals), resolver only.
    members_by_ref: dict[str, dict[str, str]] = field(default_factory=dict)
    #: ref -> the raw node it came from, used by the relation pass.
    raw_by_ref: dict[str, RawNode] = field(default_factory=dict)
    pack_id: str | None = None
    #: ids of nodes that were auto-collapsed -> count.
    collapsed: dict[int, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def find(self, ref: str) -> SemanticNode | None:
        for node in self.root.walk():
            if node.ref == ref:
                return node
        return None

    def all_nodes(self) -> list[SemanticNode]:
        return list(self.root.walk())


class Compiler:
    """Stateless compiler; one instance may be reused across revisions."""

    def __init__(
        self,
        *,
        use_heuristics: bool = True,
        max_depth: int = 12,
        allocator: RefAllocator | None = None,
    ) -> None:
        self.use_heuristics = use_heuristics
        self.max_depth = max_depth
        self.allocator = allocator or RefAllocator()

    def compile(self, tree: RawTree, pack: Pack | None = None) -> CompileResult:
        raw_root = tree.root

        # 1. Normalize (in place is fine; the tree is a fresh snapshot).
        normalize(raw_root)

        # 2. Structural simplify.
        simplify(raw_root)

        # 3. Entity matching (pack first, then heuristics).
        matches: dict[int, EntityMatch] = {}
        if pack is not None:
            matches.update(match_entities(raw_root, pack))
        if self.use_heuristics:
            for nid, em in heuristic_entities(raw_root).items():
                matches.setdefault(nid, em)

        # 4. Auto collapse detection.
        collapsed = _collect_collapses(raw_root)

        result = CompileResult(
            window_id=tree.window.window_id,
            process=tree.window.process,
            window_title=tree.window.title,
            root=SemanticNode(ref="", role="window", layer="window"),
            pack_id=pack.id if pack else None,
            collapsed=collapsed,
        )

        # 5. Build semantic tree. When the provider's raw root is itself the
        #    window element, use it directly (renamed to window); otherwise wrap
        #    it in a synthetic window node carrying the window title.
        if raw_root.role == "window":
            root = self._build(
                raw_root,
                parent=None,
                result=result,
                matches=matches,
                ancestors=[],
                depth=0,
                ancestor_path="",
            )
            root.layer = "window"
            root.role = "window"
            if not root.name:
                root.name = _clip(tree.window.title)
            if tree.window.active:
                root.states = root.states | {"active"}
            result.root = root
            self._annotate_collections(result.root)
            self._attach_relations(result)
            return result

        window_node = SemanticNode(
            ref=self.allocator.next(ROLE_REF_PREFIX["window"]),
            role="window",
            layer="window",
            name=_clip(tree.window.title),
            states=frozenset({"active"}) if tree.window.active else frozenset(),
        )
        window_node.fingerprint = compute_fingerprint(
            entity=None, role="window", identity=None, name=tree.window.title
        )
        result.root = window_node
        result.sources[window_node.ref] = SourceRef(native=raw_root.native)
        result.fingerprints[window_node.ref] = window_node.fingerprint
        result.members_by_ref[window_node.ref] = {}

        self._build(
            raw_root,
            parent=window_node,
            result=result,
            matches=matches,
            ancestors=[(window_node, window_node.ref)],
            depth=0,
            ancestor_path="window",
        )

        # 6. Collection annotation: a container whose children are all entities
        #    of one type becomes a collection: entity=<type> count=N +find.
        self._annotate_collections(result.root)
        self._attach_relations(result)
        return result

    def _annotate_collections(self, root: SemanticNode) -> None:
        for node in root.walk():
            if node.entity or not node.children:
                continue
            child_entities = {c.entity for c in node.children if c.entity}
            if len(child_entities) == 1 and len(node.children) >= 1:
                entity = next(iter(child_entities))
                # Only annotate containers, not arbitrary parents.
                if node.role in (
                    "list",
                    "tree",
                    "table",
                    "group",
                    "pane",
                    "screen",
                    "row",
                ):
                    node.attrs["entity"] = entity
                    node.attrs["count"] = len(node.children)
                    from somnus.ir.capability import Capability, PrimitiveStep

                    if not node.get_cap("find"):
                        node.caps.append(
                            Capability(
                                name="find",
                                level="semantic",
                                via=[PrimitiveStep(primitive="read", target="$self")],
                                risk="safe",
                            )
                        )

    # -- tree building ------------------------------------------------------

    def _build(
        self,
        raw: RawNode,
        parent: SemanticNode | None,
        result: CompileResult,
        matches: dict[int, EntityMatch],
        ancestors: list[tuple[SemanticNode, str]],
        depth: int,
        ancestor_path: str,
    ) -> SemanticNode:
        em = matches.get(id(raw))
        node = SemanticNode(
            ref="",
            role=raw.role,
            entity=em.entity if em else None,
            name=_clip(raw.name),
            value=raw.value,
            states=raw.states,
            attrs=dict(em.attrs) if em else {},
            confidence=em.confidence if em else 1.0,
            truncated=raw.truncated,
            virtual_count=raw.virtual_count,
            layer=self._layer(raw, depth),
        )
        node.ref = self.allocator.next(self._prefix(raw, em, node.layer))
        ref = node.ref
        if node.entity:
            node.confidence = em.confidence
        # An entity's `name` attribute is the authoritative display name (from
        # the pack rule), overriding the raw UIA name which for e.g. a WPF
        # dataitem is the CLR type name ("RestaurantDemo.Product").
        if node.entity and "name" in node.attrs:
            pack_name = _clip(node.attrs.pop("name"))
            if pack_name:
                node.name = pack_name

        # Capabilities: primitive first, then entity (semantic) caps.
        caps: list[Capability] = primitive_capabilities(raw)
        if em is not None and em.def_ is not None:
            caps.extend(entity_capabilities(em))
        elif em is not None:
            caps.extend(_heuristic_caps(em))
        node.caps = _dedupe_caps(caps)

        # Fingerprint.
        identity = em.identity if em else raw.native.automation_id
        node.fingerprint = compute_fingerprint(
            entity=node.entity,
            role=node.role,
            identity=identity,
            name=node.name,
            ancestor_path=ancestor_path,
        )
        result.fingerprints[ref] = node.fingerprint
        result.ref_by_raw[id(raw)] = ref

        # Source binding (resolver only).
        members: dict[str, str] = {}
        member_natives = []
        if em is not None:
            for mname, mnode in em.members.items():
                member_natives.append(mnode.native)
        result.sources[ref] = SourceRef(native=raw.native, members=tuple(member_natives))
        result.members_by_ref[ref] = members

        if parent is not None:
            parent.children.append(node)
        result.raw_by_ref[ref] = raw

        # Recurse.
        child_path = f"{ancestor_path}/{node.entity or node.role}"
        new_ancestors = ancestors + [(node, ref)]
        for child in raw.children:
            if depth + 1 > self.max_depth:
                node.truncated = True
                break
            self._build(
                child,
                parent=node,
                result=result,
                matches=matches,
                ancestors=new_ancestors,
                depth=depth + 1,
                ancestor_path=child_path,
            )

        # Fill member refs now that children exist.
        if em is not None:
            result.members_by_ref[ref] = self._member_refs(em, result)

        return node

    def _member_refs(self, em: EntityMatch, result: CompileResult) -> dict[str, str]:
        out: dict[str, str] = {}
        for name, node in em.members.items():
            ref = result.ref_by_raw.get(id(node))
            if ref:
                out[name] = ref
        return out

    def _attach_relations(self, result: CompileResult) -> None:
        """Second pass: relations need the fully built tree + member refs."""
        # Map normalised screen/region names to their refs for `opens`.
        screens: dict[str, str] = {}
        for node in result.root.walk():
            if node.layer in ("screen", "dialog") or node.role in ("dialog",):
                if node.name:
                    screens[node.name.strip().casefold()] = node.ref

        def walk(node: SemanticNode, parent: SemanticNode | None, ancestors):
            raw = result.raw_by_ref.get(node.ref)
            if raw is not None:
                node.relations = build_relations(
                    node,
                    raw,
                    parent,
                    ancestors,
                    result.members_by_ref.get(node.ref, {}),
                    screens,
                )
            new_ancestors = ancestors + [(node, node.ref)]
            for child in node.children:
                walk(child, node, new_ancestors)

        walk(result.root, None, [])

    def _prefix(self, raw: RawNode, em: EntityMatch | None, layer: str | None) -> str:
        if em is not None:
            return REF_PREFIX.get(em.entity, ROLE_REF_PREFIX.get(raw.role, "n"))
        if layer == "screen":
            return "s"
        if layer == "dialog":
            return "d"
        if layer == "window":
            return "w"
        return ROLE_REF_PREFIX.get(raw.role, "n")
    def _layer(self, raw: RawNode, depth: int) -> str | None:
        if raw.role == "window":
            return "window"
        if raw.role == "dialog" or "modal" in raw.states:
            return "dialog"
        if raw.role in ("group", "pane") and raw.name and depth <= 1:
            return "screen"
        return None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _clip(text: str | None, limit: int = 80) -> str | None:
    if text is None:
        return None
    text = text.replace("\n", "\\n")
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _dedupe_caps(caps: list[Capability]) -> list[Capability]:
    out: list[Capability] = []
    seen: set[str] = set()
    # Semantic caps win over primitive caps of the same name.
    for cap in sorted(caps, key=lambda c: 0 if c.level == "semantic" else 1):
        if cap.name in seen:
            continue
        seen.add(cap.name)
        out.append(cap)
    # Drop the generic 'read'/'focus' if we already have richer caps? No - they
    # are useful and cheap; keep deterministic order: semantic, then the rest.
    out.sort(key=lambda c: (0 if c.level == "semantic" else 1, c.name))
    return out


def _heuristic_caps(em: EntityMatch) -> list[Capability]:
    from somnus.ir.capability import PrimitiveStep

    caps = []
    if "add_btn" in em.members:
        caps.append(
            Capability(
                name="add",
                level="semantic",
                via=[PrimitiveStep(primitive="activate", target="member:add_btn")],
                risk="normal",
            )
        )
    return caps


def _collect_collapses(root: RawNode) -> dict[int, int]:
    out: dict[int, int] = {}
    for node in root.walk():
        if not node.children:
            continue
        collapses = auto_collapse_groups(node.children)
        out.update(collapses)
    return out


def compile_tree(tree: RawTree, pack: Pack | None = None, **kwargs: Any) -> CompileResult:
    """Functional wrapper used by tests and the CLI."""
    return Compiler(**kwargs).compile(tree, pack=pack)
