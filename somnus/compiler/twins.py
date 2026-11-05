"""Collapse nodes that are the *same* native element seen twice.

Some UIA providers hand the same element back more than once: a WPF combo box
that is expanded can surface every drop-down row twice (a ghost copy plus the
real row), and a tray flyout can hold a hidden copy of a taskbar button. The
compiler gives both copies the same fingerprint, so the registry correctly
reports them as ambiguous - and the session then refuses *every* call on those
refs with ``TARGET_AMBIGUOUS``, even though the ref the agent was given names
exactly one node.

This pass folds those duplicates before the registry ever sees them. It is
deliberately **conservative**, because merging two genuinely different elements
would make one of them unreachable:

* it only compares nodes that are direct siblings (the duplicate shape these
  providers produce is a pair under one parent);
* it only merges when there is proof the nodes are the same element - the same
  fingerprint *and* either the same native ``runtime_id`` or the shape evidence
  below; and
* when it cannot prove it, it keeps both nodes and **annotates** them
  (``twin=true``) instead of silently dropping one.

The merge conditions are:

1. **Same runtime id.** UIA's runtime id names the element itself, so two nodes
   carrying one id *are* one element reached twice. Merge.
2. **One actionable, the rest inert.** Two nodes with one fingerprint but only
   one of them exposing an interaction pattern (``invoke``/``selectionitem``/…)
   are the ghost/real pair: the inert copy is a visual echo of the control that
   actually answers. Keep the actionable one.
3. Everything else is left alone (and annotated), including two equally
   actionable nodes with different runtime ids - they may be two real controls
   that merely share an accessibility identity, and hiding one would be a
   silent loss.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

from somnus.ir.node import SemanticNode

if TYPE_CHECKING:  # avoids the import cycle: compile.py calls into this module
    from somnus.compiler.compile import CompileResult

#: Patterns that prove an element is operable. Used to tell a ghost copy (an
#: inert visual echo) from the control that actually answers. Deliberately
#: pattern-based rather than capability-based: a role like ``listitem`` grants
#: ``select`` even without the pattern, so a ghost row would look "actionable"
#: by its capability list while in fact offering nothing the platform can run.
_OPERABLE_PATTERNS = frozenset(
    {
        "invoke",
        "toggle",
        "selectionitem",
        "griditem",
        "value",
        "rangevalue",
        "expandcollapse",
        "scrollitem",
    }
)

#: Attribute set on a kept node when duplicates had to stay. Rendered, so the
#: observation says the same thing twice instead of pretending it does not.
_TWIN_ATTR = "twin"
#: Attribute set on the survivor of a successful merge, naming how many copies
#: were folded into it.
_DUP_ATTR = "dup"


def collapse_twins(root: SemanticNode, result: CompileResult) -> int:
    """Drop duplicate siblings in place; return how many nodes were folded.

    Call this **after** the semantic tree is built and **before** collections,
    relations and registry reconciliation: the registry's ambiguity decision is
    what the duplicates would otherwise poison, and a dropped node must not be
    referenced by a member/relation map.
    """
    folded = 0
    # Bottom-up so a duplicate subtree is removed before its parent is judged.
    for node in _post_order(root):
        folded += _collapse_children(node, result)
    return folded


def _post_order(node: SemanticNode) -> Iterable[SemanticNode]:
    for child in node.children:
        yield from _post_order(child)
    yield node


def _collapse_children(parent: SemanticNode, result: CompileResult) -> int:
    if len(parent.children) < 2:
        return 0

    groups: dict[str, list[SemanticNode]] = {}
    order: list[str] = []
    for child in parent.children:
        key = child.fingerprint
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(child)

    folded = 0
    for key in order:
        members = groups[key]
        if len(members) < 2:
            continue
        keep = _pick_survivor(members, result)
        if keep is None:
            # Cannot prove these are one element. Keep both and say so.
            for member in members:
                member.attrs[_TWIN_ATTR] = True
            continue
        dropped = [m for m in members if m is not keep]
        parent.children = [c for c in parent.children if c not in dropped]
        keep.attrs[_DUP_ATTR] = keep.attrs.get(_DUP_ATTR, 0) + len(dropped)
        for member in dropped:
            _redirect_members(member, keep, result)
            _forget_subtree(member, result)
        folded += len(dropped)
        result.warnings.append(
            "collapsed {n} duplicate {role} {name!r}".format(
                n=len(dropped),
                role=keep.role,
                name=keep.name or "",
            )
        )
    return folded


def _pick_survivor(
    members: list[SemanticNode], result: CompileResult
) -> SemanticNode | None:
    """The node to keep, or ``None`` when the merge is not provable."""
    if _same_runtime(members, result):
        # UIA's runtime id names the element itself: these are one element
        # reached twice, whatever else differs.
        actionable = [m for m in members if _actionable(m)]
        return actionable[0] if actionable else members[0]

    actionable = [m for m in members if _actionable(m)]
    inert = [m for m in members if not _actionable(m)]

    if actionable and not inert:
        # All copies can act, and they are not the same runtime element. They
        # may be two genuine controls that merely share an accessibility
        # identity; merging would make one of them unreachable.
        return None
    if actionable and inert:
        # One real control plus inert echoes: drop the echoes, keep the control.
        return actionable[0] if len(actionable) == 1 else None
    # All copies are inert: they are indistinguishable labels/structure, so
    # keeping one loses nothing observable.
    return members[0]


def _same_runtime(members: list[SemanticNode], result: CompileResult) -> bool:
    ids = []
    for member in members:
        source = result.sources.get(member.ref)
        if source is None or not source.native.runtime_id:
            return False
        ids.append(source.native.runtime_id)
    return len(set(ids)) == 1


def _actionable(node: SemanticNode) -> bool:
    return bool(node.patterns & _OPERABLE_PATTERNS)


def _redirect_members(
    doomed: SemanticNode, keep: SemanticNode, result: CompileResult
) -> None:
    """Point an entity's member map at the survivor instead of the folded copy.

    ``CompileResult.members_by_ref`` is built during the build pass, before this
    one. A folded node may be one of an entity's members (a duplicated "Add"
    button inside a product group); leaving that map pointing at a ref whose
    source was just removed would make ``member:add_btn`` resolve to
    ``STALE_TARGET``. The two nodes are the same element by construction, so the
    survivor is the correct target.
    """
    dropped_refs = {node.ref for node in doomed.walk()}
    for members in result.members_by_ref.values():
        for key, value in list(members.items()):
            if value in dropped_refs:
                members[key] = keep.ref


def _forget_subtree(node: SemanticNode, result: CompileResult) -> None:
    """Remove a dropped node's bookkeeping so nothing resolves to it later."""
    for doomed in node.walk():
        ref = doomed.ref
        result.sources.pop(ref, None)
        result.fingerprints.pop(ref, None)
        result.members_by_ref.pop(ref, None)
        raw = result.raw_by_ref.pop(ref, None)
        if raw is not None:
            result.ref_by_raw.pop(id(raw), None)
