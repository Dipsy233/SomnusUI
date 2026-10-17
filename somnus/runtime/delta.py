"""Delta computation between two compiled revisions.

Three line kinds: ``~`` changed, ``+`` added, ``-`` removed. Identity is the
fingerprint, so a node that merely got re-referenced does not show as add/remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from somnus.ir.node import SemanticNode, SemanticNode as _SN  # noqa: F401

#: Fields compared between structurally identical nodes.
WATCHED_ATTRS = ("count", "total", "qty", "price", "value", "len")

#: Rendering limits for the delta body. A single call can add or remove
#: hundreds of nodes (a navigation switch in Steam swings ~150 lines); without
#: a cap the delta costs more tokens than the observation it replaced. Same
#: role-and-cap-shape runs are folded into one line plus a count, and the
#: whole body is truncated with a `… +N more` marker.
DELTA_MAX_LINES = 24


def _brief_node(node: SemanticNode) -> str:
    """One compact line per added/removed node: type, ref, name only.

    Capabilities and states are intentionally omitted: the agent already knows
    the grammar's default table, and the point of a delta is "what appeared",
    not a full re-render of each line.
    """
    from somnus.sui.serialize import render_node_line, RenderOptions

    return render_node_line(
        node,
        RenderOptions(include_default_caps=False, max_string=40),
    )


@dataclass(slots=True)
class Delta:
    changed: list[tuple[str, str, Any, Any]] = field(default_factory=list)
    added: list[SemanticNode] = field(default_factory=list)
    #: refs of removed nodes worth an individual `-` line.
    removed: list[str] = field(default_factory=list)
    #: how many removed nodes were folded into a count instead.
    removed_folded: int = 0

    @property
    def empty(self) -> bool:
        return not (self.changed or self.added or self.removed)

    def render(self) -> str:
        from somnus.sui.serialize import render_delta

        # ~ lines are cheap and informative; keep them all. + / - lines are
        # folded and capped.
        out = render_delta(self.changed, (), ())
        lines = [l for l in out.splitlines() if l.startswith("~")]

        for node in self._fold_added():
            lines.append(node)
        for ref in self.removed:
            lines.append(f"- {ref}")
        if self.removed_folded:
            lines.append(f"- {self.removed_folded} anonymous/label nodes")
        if len(lines) > DELTA_MAX_LINES:
            hidden = len(lines) - DELTA_MAX_LINES
            lines = lines[:DELTA_MAX_LINES] + [f"… +{hidden} more (delta folded)"]
        return "\n".join(lines)

    def _fold_added(self) -> list[str]:
        """Added nodes as lines, with same-shape runs folded into one line.

        A run is consecutive added nodes sharing a display type and cap set
        ("60 TEXT lines" or "40 IMAGE lines" after a page switch). One line
        with `xN` carries the same signal - what kind of content arrived and
        how much - at a fraction of the tokens. Named nodes always get their
        own line: they are the things the agent can target.
        """
        out: list[str] = []
        run_type: str | None = None
        run_count = 0

        def flush() -> None:
            nonlocal run_count
            if run_count:
                suffix = f" x{run_count}" if run_count > 1 else ""
                out.append(f"+ {run_type}{suffix} (unnamed)")

        for node in self.added:
            if node.name:
                flush()
                run_count = 0
                out.append("+ " + _brief_node(node))
                continue
            t = node.display_type
            if t == run_type:
                run_count += 1
            else:
                flush()
                run_type = t
                run_count = 1
        flush()
        return out


def compute_delta(before: dict[str, SemanticNode], after: dict[str, SemanticNode]) -> Delta:
    """Compute the delta between two ref->node maps.

    Both maps are keyed by *fingerprint* here so re-references are invisible.
    """
    delta = Delta()
    for fp, new_node in after.items():
        old_node = before.get(fp)
        if old_node is None:
            if _worth_reporting(new_node):
                delta.added.append(new_node)
            continue
        delta.changed.extend(_diff_node(old_node, new_node))
        for child in new_node.children:
            delta.added.extend([])  # children handled by full walk below
    # Children of added nodes are represented implicitly; only report added
    # standalone nodes that have a ref different from their parent.
    for fp, old_node in before.items():
        if fp not in after:
            delta.removed.append(old_node.ref)
    return delta


def diff_trees(
    before: dict[str, SemanticNode],
    after: dict[str, SemanticNode],
) -> Delta:
    """Flat comparison over all nodes (preferred entry point)."""
    delta = Delta()
    for fp, new_node in after.items():
        old_node = before.get(fp)
        if old_node is None:
            if _worth_reporting(new_node):
                delta.added.append(new_node)
            continue
        delta.changed.extend(_diff_node(old_node, new_node))
    for fp, old_node in before.items():
        if fp not in after:
            if _worth_removal(old_node):
                delta.removed.append(old_node.ref)
            else:
                delta.removed_folded += 1
    return delta


#: Roles whose *name* is state rather than a label, and which therefore
#: belong in the delta when it changes. A control that reports its current
#: value only through its accessible name - Paint's colour swatch reads
#: `颜色 1: 红色` and then `颜色 1: 深红色` - has no other channel to show that
#: anything happened, so excluding its role from the name comparison made a
#: successful click indistinguishable from a no-op (the delta came back empty
#: and the runtime reported "no effect" for an action that had worked).
#:
#: Deliberately not "every leaf with a name": buttons, tabs and list rows
#: re-title themselves for reasons that are not state (a window title appearing
#: in a tab), and the point of the delta is to be read, not scanned.
_STATEFUL_NAME_ROLES = frozenset(
    {"radio", "checkbox", "check", "toggle", "switch", "option"}
)


def _diff_node(old: SemanticNode, new: SemanticNode) -> list[tuple[str, str, Any, Any]]:
    out: list[tuple[str, str, Any, Any]] = []
    for key in WATCHED_ATTRS:
        ov = old.attrs.get(key)
        nv = new.attrs.get(key)
        if ov != nv and nv is not None:
            out.append((new.ref, key, ov if ov is not None else "?", nv))
    # value changes only for leaves / inputs (avoid noise from entity groups).
    if old.role in ("input", "slider", "text", "statusbar") and old.value != new.value:
        if new.value is not None:
            out.append((new.ref, "value", old.value or "", new.value))
    # A label's *name* is its text, and for a status/summary line that text is
    # the state: `0 items $0.00` becoming `1 items $9.50` is the application
    # reporting what happened. UIA puts that string in the Name property, so
    # comparing only `value`/attrs missed it entirely and the most informative
    # line of the delta ("the cart total moved") never appeared - the agent saw
    # a new row appear with no statement of what it did to the totals.
    #
    # Restricted to named leaves: on a container the name is a stable label
    # ("购物车", "商品") whose churn is noise, and entities carry attrs instead.
    elif (
        (old.role in ("text", "statusbar", "link") or old.role in _STATEFUL_NAME_ROLES)
        and not old.children
        and old.name
        and new.name
        and old.name != new.name
    ):
        out.append((new.ref, "name", old.name, new.name))
    return out


def _worth_reporting(node: SemanticNode) -> bool:
    """Filter structural noise: only entities and top-level nodes."""
    return node.entity is not None or node.layer in ("screen", "dialog") or bool(node.name)


def _worth_removal(node: SemanticNode) -> bool:
    """Which disappearing nodes deserve a `-` line.

    Named/interactive nodes are signal (the row the agent was looking at went
    away). Anonymous text/image leaf noise - the bulk of a page switch - is
    not: the agent can see the page changed from the `+` side alone, and 120
    `- txN` lines carry zero actionable information.
    """
    return bool(node.name) or node.role in (
        "button", "input", "link", "menuitem", "list", "table", "tab",
        "screen", "dialog", "window", "select", "checkbox", "slider",
    )


def index_by_fingerprint(root: SemanticNode) -> dict[str, SemanticNode]:
    out: dict[str, SemanticNode] = {}
    for node in root.walk():
        out[node.fingerprint] = node
    return out
