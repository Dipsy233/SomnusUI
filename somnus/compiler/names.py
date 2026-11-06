"""Deterministic name synthesis.

Runs after structural simplification and before entity matching.

UIA often reports a control with an empty ``Name`` even though the label the
user reads is present in the tree - as the text sibling in front of a field, or
as the caption text inside a button. Without this pass that information exists
but is unusable: the node cannot be named by ``FIND``, entity matching cannot
see a title on a row, and the observation prints ``BUTTON b1`` followed by a
separate ``TEXT`` line.

Sources, in order (the accessible-name precedence UIA actually publishes, not
Playwright's full ARIA chain):

1. the node's own name - never touched;
2. the text sibling that precedes a field (the Win32/WPF "(Label) (Edit)"
   layout, which is also what ``label_for`` derives);
3. the node's *only* named leaf text descendant;
4. a control's ``Description`` (the placeholder/help text UIA exposes on an
   input).

Never from ``value``, and never from mutable state: a synthesised name is a
label, not a reading of what the control currently contains.

Every synthesised name is flagged :attr:`RawNode.name_synthetic`, which travels
to the semantic node and is excluded from fingerprint evidence - see
:func:`somnus.ir.node.compute_fingerprint`. A name taken from text that changes
(a counter, a price) must not re-mint the ref of a node that never moved.

The pass is deliberately conservative: containers are not named (a pane would
inherit a whole paragraph of child text), a long leaf is not named (it is
content, not a label), and only the first three sources are consulted.
"""

from __future__ import annotations

from somnus.ir.node import RawNode

#: Roles a name may be synthesised for. Two groups, for two different reasons:
#:
#: * interactive controls - a name is what ``FIND`` and ``CALL`` need to
#:   address them;
#: * row-shaped nodes - a name is what entity matching and ``FIND`` use to
#:   treat the row as one thing.
#:
#: Deliberately absent: containers (``list``/``tree``/``table``/``pane``/
#: ``screen``/``window``/``toolbar``) whose text belongs to their children,
#: and ``text`` itself (it *is* the text).
_TARGET_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "link",
        "menuitem",
        "tab",
        "checkbox",
        "radio",
        "switch",
        "slider",
        "input",
        "select",
        "listitem",
        "treeitem",
        "row",
        "group",
    }
)

#: Fields whose label is a preceding text sibling rather than a child.
_FIELD_ROLES: frozenset[str] = frozenset({"input", "select", "slider"})

#: Unnamed wrappers a leaf text may sit behind (a WPF template nests a caption
#: one or two levels down). Only these are looked through - a *named* container
#: is structure, not a caption holder.
_WRAPPER_ROLES: frozenset[str] = frozenset({"group", "pane", "unknown"})

#: Roles where a caption is a child of the control, not a separate concept.
#: When the name came from that child, the child is dropped so the tree keeps
#: one line per concept (the same rule :mod:`somnus.compiler.simplify` applies
#: to a control that already had a name). Row roles are **not** here: entity
#: matching reads those child texts and must keep seeing them.
_CAPTION_CHILD_ROLES: frozenset[str] = frozenset(
    {"button", "link", "menuitem", "tab", "checkbox", "radio", "switch", "treeitem"}
)

#: Longest text accepted as a name. Beyond it the text is content (a message
#: preview, a paragraph), not a label, and a clipped name would be a lie.
_MAX_NAME_CHARS = 80


def synthesize_names(root: RawNode) -> RawNode:
    """Name unnamed controls and rows in place; returns ``root``."""
    _synthesize(root, None)
    return root


def _synthesize(node: RawNode, parent: RawNode | None) -> None:
    # Post-order so a child is settled before its parent is looked at. (The
    # sources never read a synthesised name, so this is about determinism, not
    # dependence.)
    for child in node.children:
        _synthesize(child, node)

    if node.name or node.role not in _TARGET_ROLES:
        return

    text = _preceding_label(node, parent) or _only_leaf_text(node) or _description(node)
    if not text:
        return
    name = " ".join(text.split())
    if not name or len(name) > _MAX_NAME_CHARS:
        return
    node.name = name
    node.name_synthetic = True
    _drop_caption_child(node)


def _preceding_label(node: RawNode, parent: RawNode | None) -> str | None:
    """The text sibling immediately before a field, as ``label_for`` reads it."""
    if node.role not in _FIELD_ROLES or parent is None:
        return None
    for index, child in enumerate(parent.children):
        if child is not node:
            continue
        # Same window as relations._label_for_ref: up to two siblings back,
        # which covers the "(Label) (Edit)" and "(Label) (Hint) (Edit)" shapes.
        for step in range(index - 1, max(-1, index - 3), -1):
            prev = parent.children[step]
            if prev.role == "text" and prev.name and not prev.name_synthetic:
                return prev.name
        return None
    return None


def _only_leaf_text(node: RawNode) -> str | None:
    """The node's single distinct leaf text, or None when it is ambiguous."""
    texts = _leaf_texts(node)
    distinct = {text for text in texts if text and not text.isspace()}
    if len(distinct) != 1:
        return None
    return distinct.pop()


def _leaf_texts(node: RawNode) -> list[str]:
    out: list[str] = []
    for child in node.children:
        if child.role == "text" and not child.children and child.name:
            if not child.name_synthetic:
                out.append(child.name)
        elif (
            child.role in _WRAPPER_ROLES
            and not child.name
            and not child.value
            and child.children
        ):
            # An unnamed wrapper is a template layer, not a concept; look
            # through it, but never through a node that carries its own name.
            out.extend(_leaf_texts(child))
    return out


def _description(node: RawNode) -> str | None:
    """A field's Description (placeholder/help text), never its value."""
    if node.role not in _FIELD_ROLES or not node.description:
        return None
    text = node.description
    # A Description that merely repeats the value is the content read back,
    # not a label.
    if node.value and text.strip() == node.value.strip():
        return None
    return text


def _drop_caption_child(node: RawNode) -> None:
    """Remove a child text that only repeats the name just synthesised."""
    if node.role not in _CAPTION_CHILD_ROLES or not node.name:
        return
    name = node.name.strip()
    node.children = [
        child
        for child in node.children
        if not (
            child.role == "text"
            and not child.children
            and child.name
            and child.name.strip() == name
        )
    ]
