"""Projection engine: turn a compiled tree + rules + budget into a render plan.

The engine does not serialize; it produces a list of :class:`Line` records that
the SUI serializer formats. Keeping rendering separate is what makes golden
tests stable and lets us compute token budgets incrementally (we stop adding
lines once the budget is exhausted, emitting a ``… +N more`` marker).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

from somnus.ir.node import SemanticNode
from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.projection.rules import Projection, Rule

#: Roles that stay visible at L0 even when anonymous: inputs carry values the
#: agent may set, and these document/structural roles carry meaning.
_L0_KEEP_ROLES = frozenset({"input", "document", "list", "tree", "table"})

#: The fields the viewport collector attaches to a container it had to cut at
#: the viewport edge (docs/viewport-capture-proposal.md, "数据契约"). They are
#: *metadata about what was not captured*, never rendered as raw attributes:
#: projection turns them into the `rows=/visible=/scroll=` summary and the
#: `+more` marker. A node without them renders exactly as it always has.
_VIEWPORT_KEYS = (
    "items_total",
    "items_visible",
    "items_truncated",
    "scroll_pct",
)


class _RenderedInt(int):
    """An ``int`` whose rendered form is custom text.

    Used for the summary pair `visible=12 scroll=5%`: the serializer renders
    numeric attributes through ``str(value)``, so a combined token keeps the
    documented field order (`rows`, `visible`, `scroll`) and the bare `%`
    without touching the serializer or inventing a new attribute name. The
    value remains an ``int`` for anyone comparing it numerically.
    """

    def __new__(cls, value: int, text: str) -> "_RenderedInt":
        obj = super().__new__(cls, value)
        obj._text = text
        return obj

    def __str__(self) -> str:
        return self._text


@dataclass(slots=True)
class Line:
    """One renderable node line plus its indent depth."""

    node: SemanticNode
    depth: int
    #: When set, the node itself is hidden and only this summary is rendered.
    collapsed: bool = False
    #: Number of children hidden behind a collapse.
    hidden_count: int = 0
    #: True when this line is a truncation marker.
    marker: bool = False
    marker_text: str = ""
    #: Name/numeric attribute overrides (entity attrs already on node).
    extra_flags: tuple[str, ...] = ()


@dataclass(slots=True)
class RenderPlan:
    lines: list[Line] = field(default_factory=list)
    tokens: int = 0
    truncated: bool = False
    hidden_total: int = 0


class ProjectionEngine:
    #: Homogeneous collections render this many children as a preview; the rest
    #: is folded into a single marker. Showing 2-3 examples is enough for the
    #: agent to tell what the collection holds without spending a line each.
    preview_children = 3
    #: A viewport-truncated container renders at most this many *complete*
    #: example rows. Three is the same "enough to recognise the shape" budget
    #: as `preview_children`; the actual count is lowered further when the
    #: remaining token budget cannot hold them (see `_plan_viewport`).
    viewport_examples = 3

    def __init__(
        self,
        budget: ObservationBudget | None = None,
        *,
        auto_collapse_threshold: int = 8,
        l0: bool = False,
    ) -> None:
        self.budget = budget or ObservationBudget()
        self.auto_collapse_threshold = auto_collapse_threshold
        #: When True, anonymous actionless leaves are folded at overview level
        #: (the L0 noise floor). The engine's caller passes level == 0.
        self.l0 = l0
        #: Root ref used by the viewport `+more` marker; set by ``plan``.
        self._window_ref: str = ""

    def plan(
        self,
        root: SemanticNode,
        projection: Projection | None = None,
        *,
        focus: SemanticNode | None = None,
        depth_limit: int | None = None,
        full: bool = False,
        expanded: set[int] | None = None,
        desktop_summary: list[dict] | None = None,
    ) -> RenderPlan:
        """Build the render plan.

        ``full`` disables every budget/collapse: the whole tree is rendered
        (``OBSERVE w5 full``). ``expanded`` is the set of ids the agent asked
        to expand this round, overriding both the projection's collapse rule
        and the auto-collapse heuristic for those nodes only.

        ``desktop_summary`` is the capture layer's window list: one line
        describing the desktop is prepended so the model does not need a
        separate ``OBSERVE windows`` turn. ``None`` (the default) renders
        exactly as before - no line, no blank, no fragment.
        """
        projection = projection or Projection()
        expanded = expanded or set()
        max_depth = depth_limit if depth_limit is not None else (
            None if full else self.budget.max_depth
        )
        plan = RenderPlan()
        # The `+more` marker names the window the agent must ask for
        # (`OBSERVE w1 expand l1`), so the plan needs the root's ref.
        self._window_ref = root.ref
        counter = {"nodes": 0, "tokens": 0}

        # Desktop summary first: it replaces a whole `observe windows` turn, so
        # it is planned before the tree and its tokens are charged to the same
        # budget. It is never skipped by the budget - the line is a couple of
        # tokens and losing it sends the model on an extra round trip.
        desktop_line = _desktop_summary_line(desktop_summary)
        if desktop_line:
            plan.lines.append(
                Line(node=root, depth=0, marker=True, marker_text=desktop_line)
            )
            counter["tokens"] += count_tokens(desktop_line)

        self._walk(
            root, 0, max_depth, projection, plan, counter,
            top=True, full=full, expanded=expanded,
        )

        # Depth/entity focus: if a focus node is given, also plan its subtree
        # (used by INSPECT which bypasses the L0 collapse).
        if focus is not None and focus is not root:
            self._walk(
                focus, 0, max_depth, projection, plan, counter,
                top=True, full=full, expanded=expanded,
            )
        plan.tokens = counter["tokens"]
        if plan.truncated and plan.hidden_total:
            plan.lines.append(
                Line(
                    node=root,
                    depth=0,
                    marker=True,
                    marker_text=f"… +{plan.hidden_total} more",
                )
            )
        return plan

    # -- walking ------------------------------------------------------------

    def _walk(
        self,
        node: SemanticNode,
        depth: int,
        max_depth: int | None,
        projection: Projection,
        plan: RenderPlan,
        counter: dict[str, int],
        *,
        top: bool = False,
        full: bool = False,
        expanded: set[int] | None = None,
    ) -> None:
        expanded = expanded or set()
        rule = projection.rule_for(node) if not top else Rule.KEEP
        if rule is Rule.PRUNE:
            return
        if rule is Rule.FLATTEN:
            for child in node.children:
                self._walk(
                    child, depth, max_depth, projection, plan, counter,
                    full=full, expanded=expanded,
                )
            return

        # Viewport-aware container: the collector kept every visible row but
        # cut the rest. Its summary (`rows=/visible=/scroll=`) and `+more`
        # marker are *metadata the agent needs to know the list exists*, so
        # they are planned before any budget/depth refusal can swallow them.
        viewport = _viewport_of(node) if not top else None
        if viewport is not None:
            self._plan_viewport(
                node, depth, max_depth, projection, plan, counter,
                viewport=viewport, full=full, expanded=expanded,
            )
            return
        # A node that carries the fields but is not actually cut (all rows
        # visible / a stale flag) must render exactly as it did before: strip
        # the collector's raw names from the render-facing copy. `count=` etc.
        # stay untouched here, because the fallback is "today's output".
        render_node = node
        if any(key in node.attrs for key in _VIEWPORT_KEYS):
            render_node = dataclasses.replace(
                node,
                attrs={k: v for k, v in node.attrs.items() if k not in _VIEWPORT_KEYS},
            )

        # In full mode the budget does not apply.
        if not full and counter["nodes"] >= self.budget.max_nodes:
            plan.truncated = True
            plan.hidden_total += 1
            return

        # Budget by depth.
        if max_depth is not None and depth > max_depth:
            plan.truncated = True
            plan.hidden_total += self._count(node)
            return

        if full or id(node) in expanded:
            collapsed = False
            folded_leaf = False
        else:
            collapsed = rule is Rule.COLLAPSE or self._auto_collapse(node)
            # L0 noise floor: an anonymous leaf (a TEXT/IMAGE the app gave no
            # name to) carries no actionable signal at overview level - the
            # bulk of a CEF page is such lines, and an unnamed icon button
            # cannot be told apart from decoration anyway. Folded leaves are
            # counted and reported as one summary line after the siblings;
            # INSPECT / expand / full reveal them. `input` stays visible: its
            # value may be what the agent needs to set or read.
            folded_leaf = (
                not collapsed
                and self.l0
                and depth > 0
                and not node.children
                and not node.name
                and not node.entity
                and node.role not in _L0_KEEP_ROLES
            )
            if folded_leaf:
                counter["folded_leaves"] = counter.get("folded_leaves", 0) + 1
                return
        line = Line(node=render_node, depth=depth, collapsed=collapsed)
        if collapsed:
            line.hidden_count = self._count(node)

        if not self._emit(line, plan, counter, full=full):
            return

        if collapsed:
            return
        # Homogeneous preview: render the first `preview_children` children of
        # a large same-role collection, fold the rest into one marker. Every
        # example matters (they say what the collection contains) without one
        # line per row (a 300-game Steam list would eat the whole budget).
        children = node.children
        preview_limit = len(children)
        if (
            not full
            and id(node) not in expanded
            and rule is not Rule.COLLAPSE
            and len(children) > self.auto_collapse_threshold
            and len({c.role for c in children}) <= 2
        ):
            preview_limit = self.preview_children
        for i, child in enumerate(children):
            if i == preview_limit:
                hidden = len(children) - preview_limit
                plan.lines.append(
                    Line(
                        node=child,
                        depth=depth + 1,
                        marker=True,
                        marker_text=f"\u2026 +{hidden} more",
                    )
                )
                counter["tokens"] += count_tokens(f"  \u2026 +{hidden} more")
                self._flush_folded_leaves(counter, plan, depth + 1)
                return
            self._walk(
                child, depth + 1, max_depth, projection, plan, counter,
                full=full, expanded=expanded,
            )
        self._flush_folded_leaves(counter, plan, depth + 1)

    def _plan_viewport(
        self,
        node: SemanticNode,
        depth: int,
        max_depth: int | None,
        projection: Projection,
        plan: RenderPlan,
        counter: dict[str, int],
        *,
        viewport: tuple[int, int, int | None],
        full: bool,
        expanded: set[int],
    ) -> None:
        """Plan a container the collector cut at the viewport edge.

        Shape::

            LIST l1 "商品" rows=240 visible=12 scroll=5%
              LISTITEM li1 "Item 0" ...
              LISTITEM li2 "Item 1" ...
              LISTITEM li3 "Item 2" ...
              +more items=228 use FIND "<name>" or OBSERVE w1 expand l1

        * the summary line is **forced** - it is the only thing that tells the
          agent a large list exists, so neither the node cap, the token cap nor
          the depth cap may drop it;
        * the example rows are complete lines (attributes/capabilities/states
          intact), never compressed, and their count is `viewport_examples`
          lowered by what the remaining token budget can hold after the
          summary and the marker are reserved;
        * the marker is forced too, and it is the only place the hidden count
          is stated.
        """
        total, visible, scroll = viewport
        from somnus.sui.serialize import render_line

        display = _viewport_display(node, total, visible, scroll)
        self._emit(Line(node=display, depth=depth), plan, counter, full=full, force=True)

        hidden = total - visible
        marker_line = Line(
            node=display,
            depth=depth + 1,
            marker=True,
            marker_text=self._viewport_more_text(display, hidden),
        )
        marker_cost = count_tokens(render_line(marker_line))

        # `OBSERVE ... expand l1` means "give me this page"; when the agent has
        # already asked, render every captured row instead of the 3 examples.
        # `full` is the same request stated more broadly.
        limit = len(node.children) if (full or id(node) in expanded) else self.viewport_examples
        for child in node.children[:limit]:
            if not full:
                # A lower bound on the row's cost (its own line; descendants
                # add more). It is enough to stop *before* spending the budget
                # that the marker needs.
                estimate = count_tokens(render_line(Line(node=child, depth=depth + 1)))
                if counter["tokens"] + estimate + marker_cost > self.budget.max_tokens:
                    break
            before = counter["tokens"]
            self._walk(
                child, depth + 1, max_depth, projection, plan, counter,
                full=full, expanded=expanded,
            )
            if counter["tokens"] == before:
                # The child was pruned/folded and produced no text; further
                # examples would not either.
                break

        # The marker's own cost was reserved above, so it is emitted even when
        # the examples used the rest of the budget. The hidden count is
        # `total - visible` ("rows outside the viewport") rather than
        # `total - rendered_examples`, which is what makes it agree with the
        # `visible=` summary.
        plan.lines.append(marker_line)
        counter["tokens"] += marker_cost

    def _viewport_more_text(self, node: SemanticNode, hidden: int) -> str:
        """`+more items=N use FIND "<name>" or OBSERVE wN expand <ref>`.

        Each route is named **only when it is really available on this node**:

        * `FIND` is printed only when the container carries the `find`
          capability. Compile grants it to list/table/tree and to containers of
          one entity type; a viewport-cut `pane` of mixed content has no such
          capability, and promising a name search the node does not expose - or
          printing a bare `+find` that is not there - would be a lie.
        * `OBSERVE wN expand <ref>` is the session's existing expanded-set
          mechanism; it needs no capability and no new grammar.

        Both routes already exist in the protocol; nothing here invents syntax.
        """
        head = f"+more items={hidden}"
        parts: list[str] = []
        if "find" in node.cap_names():
            parts.append('FIND "<name>"')
        if self._window_ref:
            parts.append(f"OBSERVE {self._window_ref} expand {node.ref}")
        if not parts:
            return head
        return f"{head} use " + " or ".join(parts)

    def _emit(
        self,
        line: Line,
        plan: RenderPlan,
        counter: dict[str, int],
        *,
        full: bool = False,
        force: bool = False,
    ) -> bool:
        # Pre-render through the shared formatter to measure tokens.
        from somnus.sui.serialize import render_line

        text = render_line(line)
        cost = count_tokens(text)
        if (
            not force
            and not full
            and counter["tokens"] + cost > self.budget.max_tokens
            and counter["nodes"] > 0
        ):
            plan.truncated = True
            plan.hidden_total += 1
            return False
        plan.lines.append(line)
        counter["tokens"] += cost
        counter["nodes"] += 1
        return True

    def _flush_folded_leaves(self, counter: dict[str, int], plan: RenderPlan, depth: int) -> None:
        """Emit one summary line for the anonymous leaves folded this round.

        The line is what makes the folding *discoverable*: an agent that sees
        `… +11 unnamed` knows the window has more content and can INSPECT the
        parent or re-observe with expand/full, instead of believing the tree
        really is that small.
        """
        n = counter.pop("folded_leaves", 0)
        if not n:
            return
        text = f"\u2026 +{n} unnamed"
        plan.lines.append(
            Line(node=plan.lines[-1].node if plan.lines else None, depth=depth,
                 marker=True, marker_text=text)
        )
        counter["tokens"] += count_tokens("  " + text)

    def _auto_collapse(self, node: SemanticNode) -> bool:
        # Homogeneous collections over the threshold collapse automatically.
        if node.layer in ("screen", "window"):
            return False
        if len(node.children) >= self.auto_collapse_threshold:
            roles = {c.role for c in node.children}
            if len(roles) <= 2:
                return True
        return False

    @staticmethod
    def _count(node: SemanticNode) -> int:
        return len(node.children)


# ---------------------------------------------------------------------------
# viewport metadata helpers
# ---------------------------------------------------------------------------


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return False


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _scroll_text(value: object) -> str | None:
    """`scroll_pct` (0-100) as the bare token `5%`, or None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        pct = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if pct != pct or pct < 0 or pct > 100:  # NaN or out of range
        return None
    if pct.is_integer():
        return f"{int(pct)}%"
    return f"{pct:g}%"


def _viewport_of(node: SemanticNode) -> tuple[int, int, object] | None:
    """The (total, visible, scroll) summary of a viewport-cut container, or None.

    Requirements for this rendering to engage, all from the metadata contract:

    * ``items_truncated`` is truthy;
    * both ``items_total`` and ``items_visible`` are readable integers;
    * more rows exist than are visible. ``visible >= total`` means the screen
      shows the whole list, so the node renders exactly as before - the fields
      are stripped and no summary/marker is emitted;
    * anything missing (old data, mock fixtures, a non-list node) returns
      ``None`` and the node keeps today's rendering byte for byte.
    """
    attrs = node.attrs
    if not _truthy(attrs.get("items_truncated")):
        return None
    total = _as_int(attrs.get("items_total"))
    visible = _as_int(attrs.get("items_visible"))
    if total is None or visible is None:
        return None
    if total <= 0 or visible >= total:
        return None
    return total, visible, attrs.get("scroll_pct")


def _viewport_display(
    node: SemanticNode, total: int, visible: int, scroll: object
) -> SemanticNode:
    """A render-only copy carrying `rows=`, `visible=`, `scroll=` and nothing else.

    The collector's raw field names (`items_total`, …) are stripped so they
    never leak into the observation, and the legacy `count`/`count_min`
    annotation is removed: `rows=` is the authoritative total now, and printing
    both `count=12` and `rows=240` would contradict itself.

    A shallow copy is used on purpose: the compiled node keeps its attrs
    untouched, so delta/signature/verifier never see the display tokens.
    """
    attrs = {
        key: value
        for key, value in node.attrs.items()
        if key not in _VIEWPORT_KEYS and key not in ("count", "count_min")
    }
    attrs["rows"] = total
    percent = _scroll_text(scroll)
    attrs["visible"] = (
        _RenderedInt(visible, f"{visible} scroll={percent}")
        if percent is not None
        else visible
    )
    return dataclasses.replace(node, attrs=attrs)


# ---------------------------------------------------------------------------
# desktop summary line
# ---------------------------------------------------------------------------

#: At most this many windows are listed; the rest becomes `+N more`. Six is
#: what fits in one readable line at L0 and is more than enough to tell an
#: agent what desktop it is on.
_MAX_DESKTOP_WINDOWS = 6
#: A title longer than this is truncated. It is a *label*, not content; the
#: full string is not needed to choose a window.
_DESKTOP_TITLE_MAX = 40


def _field(item: object, key: str, default: object = None) -> object:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _desktop_title(text: str) -> str:
    """Quote a window title so the summary line stays one parseable line.

    Collapses whitespace (a real newline would split the line), truncates to
    `_DESKTOP_TITLE_MAX`, then escapes backslash/quote and the `|` segment
    separator. Truncating *before* escaping matters: cutting an escape in half
    would leave a dangling backslash.
    """
    flat = " ".join(text.split())
    if len(flat) > _DESKTOP_TITLE_MAX:
        flat = flat[: _DESKTOP_TITLE_MAX - 1] + "…"
    escaped = flat.replace("\\", "\\\\").replace('"', '\\"').replace("|", "\\|")
    return f'"{escaped}"'


def _window_id_token(value: object) -> str:
    text = " ".join(str(value).split())
    if not text:
        return "?"
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("|", "\\|")


def _desktop_segment(entry: object) -> str:
    segment = _window_id_token(_field(entry, "window_id"))
    title = _field(entry, "title")
    if isinstance(title, str) and title.strip():
        segment += " " + _desktop_title(title)
    ops = _field(entry, "ops")
    if isinstance(ops, int) and not isinstance(ops, bool):
        segment += f" ops={ops}"
    if _field(entry, "active"):
        segment += " active"
    return segment


def _desktop_summary_line(summary: object) -> str:
    """`WINDOWS w1 "QQ" ops=12 | w2 "设置" ops=31 | w3 "…" active`.

    * ``None``/empty -> ``""`` and the caller adds nothing (byte-identical to
      today's observation);
    * ``active`` is appended to the active window's own segment - no extra
      marker;
    * ``ops`` is omitted when absent (never ``ops=None``);
    * more than six windows -> first six plus ``+N more``; if the active window
      fell outside the first six, it replaces the sixth so it is always present
      and the list still has six entries.
    """
    if not summary:
        return ""
    items = [entry for entry in summary if _field(entry, "window_id")]
    if not items:
        return ""

    active = next(
        (index for index, entry in enumerate(items) if _field(entry, "active")),
        None,
    )
    shown = items[:_MAX_DESKTOP_WINDOWS]
    if active is not None and active >= _MAX_DESKTOP_WINDOWS:
        shown = shown[: _MAX_DESKTOP_WINDOWS - 1] + [items[active]]

    segments = [_desktop_segment(entry) for entry in shown]
    hidden = len(items) - len(shown)
    if hidden > 0:
        segments.append(f"+{hidden} more")
    return "WINDOWS " + " | ".join(segments)
