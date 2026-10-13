"""Relation inference (dev doc section 4.6).

Exactly six relations are produced, all derived deterministically from the raw
tree - no heuristics, no LLM:

* ``contains``    - structural parent/child (rendered implicitly by indentation,
  so it is only emitted for entities/collections that a pack cares about).
* ``label_for``   - a text sibling immediately preceding an input (Win32/WPF use
  the "(Label) (Edit)" layout when ``LabeledBy`` is not exposed).
* ``acts_on``     - a verb button ("Add"/"Remove"/...) acts on its enclosing
  entity/row.
* ``controls``    - an ``expandcollapse`` control owns the container it toggles.
* ``selected_in`` - an item inside its list/tree/table/menu/tab container.
* ``opens``       - a button/menuitem whose name implies navigation opens a
  target screen (matched by name against a sibling screen/region).

This module owns the whole derivation so ``compile.py`` stays a pure tree walk.
"""

from __future__ import annotations

from somnus.ir.node import RawNode, Relation, SemanticNode

#: Roles that are interactive controls.
_INTERACTIVE = frozenset(
    {"button", "input", "checkbox", "radio", "switch", "slider", "select", "link"}
)

#: Roles that contain selectable items.
_CONTAINERS = frozenset({"list", "tree", "table", "menu", "tab"})

#: Verb-button names that imply "acts on the enclosing entity".
_ACT_VERBS = (
    "add",
    "buy",
    "remove",
    "delete",
    "select",
    "choose",
    "apply",
    "subscribe",
    "加入",
    "添加",
    "购买",
    "删除",
    "选择",
    "应用",
)

#: Names that imply the control opens/switches to another surface.
_OPEN_VERBS = (
    "open",
    "view",
    "show",
    "details",
    "settings",
    "打开",
    "查看",
    "详情",
    "设置",
)


def build_relations(
    node: SemanticNode,
    raw: RawNode,
    parent: SemanticNode | None,
    ancestors: list[tuple[SemanticNode, str]],
    members: dict[str, str],
    sibling_screens: dict[str, str],
) -> list[Relation]:
    """Compute all relations for ``node``.

    ``members`` maps a pack member name to the member's ref (so ``label_for``
    can point at the actual label node rather than a name string).
    ``sibling_screens`` maps a normalised screen name to its ref for ``opens``.
    """
    rels: list[Relation] = []

    # selected_in: item belongs to the nearest container ancestor.
    if raw.role in ("listitem", "treeitem", "row", "tab", "menuitem"):
        for anc, ref in reversed(ancestors):
            if anc.role in _CONTAINERS:
                rels.append(Relation(kind="selected_in", target=ref))
                break

    # acts_on: verb button -> enclosing entity.
    target = acts_on_ref(raw, ancestors)
    if target:
        rels.append(Relation(kind="acts_on", target=target))

    # label_for: text label names the input it precedes.
    label_ref = _label_for_ref(raw, parent, ancestors)
    if label_ref:
        rels.append(Relation(kind="label_for", target=label_ref))

    # controls: an expand/collapse control owns its child container.
    if "expandcollapse" in raw.patterns and raw.children:
        rels.append(Relation(kind="controls", target="$children"))

    # opens: a navigation control whose name matches another screen.
    opens_ref = opens_ref_for(raw, node, sibling_screens)
    if opens_ref:
        rels.append(Relation(kind="opens", target=opens_ref))

    # contains: emitted only for entities (their member refs), since structural
    # containment is already expressed by indentation (dev doc section 7.2).
    if node.entity:
        for member_ref in members.values():
            if member_ref == node.ref:
                continue
            rels.append(Relation(kind="contains", target=member_ref))

    return _dedupe(rels)


# ---------------------------------------------------------------------------
# individual rules
# ---------------------------------------------------------------------------


def acts_on_ref(
    raw: RawNode, ancestors: list[tuple[SemanticNode, str]]
) -> str | None:
    """Return the ref of the entity a verb button acts on, if any."""
    if raw.role != "button" or not raw.name:
        return None
    lowered = raw.name.strip().casefold()
    if not any(lowered == v or lowered.startswith(v) for v in _ACT_VERBS):
        return None
    for anc, ref in reversed(ancestors):
        if anc.entity:
            return ref
    for anc, ref in reversed(ancestors):
        if anc.role in ("group", "row", "listitem"):
            return ref
    return None


def opens_ref_for(
    raw: RawNode,
    node: SemanticNode,
    sibling_screens: dict[str, str],
) -> str | None:
    """A control that opens/navigates to another named surface."""
    if raw.role not in ("button", "menuitem", "link") or not raw.name:
        return None
    lowered = raw.name.strip().casefold()
    if not any(lowered == v or lowered.startswith(v) for v in _OPEN_VERBS):
        return None
    # Look for a screen/region whose name the control mentions.
    for name, ref in sibling_screens.items():
        if ref == node.ref:
            continue
        if name and name in lowered:
            return ref
    return None


def _label_for_ref(
    raw: RawNode,
    parent: SemanticNode | None,
    ancestors: list[tuple[SemanticNode, str]],
) -> str | None:
    """Find the text sibling that immediately precedes an input."""
    if raw.role not in ("input", "select", "slider"):
        return None
    if parent is None:
        return None
    children = parent.children
    for idx, child in enumerate(children):
        if child.role != raw.role or child.name != raw.name:
            continue
        for j in range(idx - 1, max(-1, idx - 3), -1):
            prev = children[j]
            if prev.role == "text" and prev.name:
                return prev.ref
        return None
    return None


def selectable_container_of(
    ancestors: list[tuple[SemanticNode, str]]
) -> str | None:
    for anc, ref in reversed(ancestors):
        if anc.role in _CONTAINERS:
            return ref
    return None


def _dedupe(relations: list[Relation]) -> list[Relation]:
    seen: set[tuple[str, str]] = set()
    out: list[Relation] = []
    for rel in relations:
        key = (rel.kind, rel.target)
        if key in seen:
            continue
        seen.add(key)
        out.append(rel)
    return out
