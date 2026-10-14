"""Session: the runtime orchestrator behind the four LLM tools.

Owns the provider, compiler, registry, resolver, settler, verifier and policy.
Exposes exactly the operations the protocol talks to:

* :meth:`observe`  -> L0 document
* :meth:`find`     -> matching entity lines
* :meth:`inspect`  -> L1/L2 document
* :meth:`call`     -> OK/ERR + delta

Everything returned here is SUI-Text. Native identity never leaves the session.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from somnus.compiler.compile import CompileResult, Compiler
from somnus.compiler.pack_schema import Pack, select_pack
from somnus.errors import ErrorCode, SomnusError, target_not_found
from somnus.ir.action import Action
from somnus.ir.node import RawTree, Relation, SemanticNode, WindowInfo
from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.projection.engine import ProjectionEngine
from somnus.projection.rules import apply_projection
from somnus.providers.base import AccessibilityProvider
from somnus.runtime.delta import Delta, diff_trees, index_by_fingerprint
from somnus.runtime.policy import Policy
from somnus.runtime.registry import SessionRegistry
from somnus.runtime.resolver import Resolver
from somnus.runtime.settle import Settler
from somnus.runtime.verifier import Verifier
from somnus.sui.serialize import INDENT, RenderOptions, render_document, render_node_line
from somnus.sui.parse import parse_action

log = logging.getLogger("somnus.session")

#: Set SOMNUS_TRACE_STATES=1 to trace a call end to end: which route a
#: capability resolved to, what each primitive did, and how the target's states
#: looked before and after the settle.
_TRACE_STATES = os.environ.get("SOMNUS_TRACE_STATES", "") not in ("", "0", "false")


@dataclass(slots=True)
class Observation:
    text: str
    rev: int
    tokens: int


@dataclass(slots=True)
class CallOutcome:
    status: str  # OK | OK_UNVERIFIED | EFFECT_NOT_CONFIRMED
    rev: int
    delta: Delta
    text: str
    settled: bool = True


@dataclass(slots=True)
class _FindHit:
    """A find match; ``is_entity`` marks domain entities (rendered first)."""

    node: SemanticNode
    is_entity: bool = False


class Session:
    def __init__(
        self,
        provider: AccessibilityProvider,
        *,
        pack: Pack | None = None,
        pack_id: str | None = None,
        policy: Policy | None = None,
        budget: ObservationBudget | None = None,
        settler: Settler | None = None,
        compiler: Compiler | None = None,
        allow_packs: bool = True,
    ) -> None:
        self.provider = provider
        self.explicit_pack = pack
        self.pack_id = pack_id
        #: When False the session never applies an App Pack: every window is
        #: compiled by the generic path alone. This is how the "no pack" mode is
        #: requested - it is a property of the session, not a trick played on
        #: ``_current_pack`` from outside.
        self.allow_packs = allow_packs
        self.policy = policy or Policy()
        self.budget = budget or ObservationBudget()
        self.settler = settler or Settler()
        self.registry = SessionRegistry()
        self.compiler = compiler or Compiler(allocator=self.registry.allocator)

        # Session state.
        self.rev = 0
        self.windows: list[WindowInfo] = []
        self.target_window: str | None = None
        self.result: CompileResult | None = None
        self._prev_index: dict[str, SemanticNode] = {}
        #: Window id -> title, as last reported, so opening/closing a window can
        #: be surfaced as a delta line instead of being silently absorbed.
        self._prev_windows: dict[str, str] = {}
        self._event_count = 0
        self._audit: list[dict] = []
        #: refs whose fingerprint is ambiguous in the current compilation.
        self._ambiguous_refs: set[str] = set()
        #: menu ref -> the ref of the node whose context menu it is. A context
        #: menu is a separate native popup with no structural link to the control
        #: that opened it, so the association has to be remembered here; the
        #: projection then re-parents the menu under its owner, which is how the
        #: menu reads as belonging to something instead of floating at window
        #: level. Cleared automatically once the menu disappears (see
        #: :meth:`_reattach_open_menu`).
        self._menu_owner: dict[str, str] = {}
        #: semantic projection digest; `rev` only moves when this changes.
        self._last_signature: tuple | None = None
        #: session ref -> native window_id, and reverse, for CALL w?.focus().
        self._window_refs: dict[str, str] = {}
        self._window_refs_by_id: dict[str, str] = {}

    # ------------------------------------------------------------------ windows

    def refresh_windows(self) -> list[WindowInfo]:
        self.windows = self.provider.list_windows()
        if self.target_window is None and self.windows:
            self.target_window = self._default_window()
        return self.windows

    def _default_window(self) -> str:
        """Pick the window to start from.

        Priority: the window with keyboard focus, else the first one that has a
        title. Windows with an empty title are usually invisible helpers, and
        starting on one of those would show the agent an empty tree.
        """
        for w in self.windows:
            if w.active:
                return w.window_id
        for w in self.windows:
            if w.title.strip():
                return w.window_id
        return self.windows[0].window_id

    def focus_window(self, ref: str) -> CallOutcome:
        """``CALL w2.focus()`` - switch the observation target.

        The window list is refreshed first: a window id is only meaningful for
        the enumeration that produced it, and apps open and close windows
        between the observe and the call. Focusing against a stale list can
        resolve the id to a *different* window and then allow-list-check the
        wrong process (found live: focusing Notepad was denied against
        `explorer.exe`).
        """
        self.refresh_windows()
        window = self._window_by_ref(ref)
        if window is None:
            raise target_not_found(ref)
        self.target_window = window.window_id
        self.policy.check_process(window.process)
        if hasattr(self.provider, "focus_window"):
            self.provider.focus_window(window.window_id)  # type: ignore[attr-defined]
        # Pin the ref the agent just used to this window: later header renders
        # must keep using it, otherwise "switch back" refs drift (w1 -> w7 ...).
        self._window_refs[ref] = window.window_id
        self._window_refs_by_id[window.window_id] = ref
        self._recompile()
        flags = " active" if window.active else ""
        # Never return an empty body: the agent must be able to confirm the
        # switch happened, and it needs the ref to switch back later.
        text = f"OK rev={self.rev}\nWINDOW {ref} {_q(window.title)}{flags}"
        return CallOutcome(status="OK", rev=self.rev, delta=Delta(), text=text)

    def _window_by_ref(self, ref: str) -> WindowInfo | None:
        # A session window ref maps to the native window id.
        window_id = self._window_refs.get(ref, ref)
        for w in self.windows:
            if w.window_id == window_id:
                return w
        return None

    # ------------------------------------------------------------------ compile

    def _current_pack(self) -> Pack | None:
        if not self.allow_packs:
            return None
        if self.explicit_pack is not None:
            return self.explicit_pack
        if self.target_window is None:
            self.refresh_windows()
        win = self._window_by_ref(self.target_window or "")
        if win is None:
            return None
        return select_pack(win.process, forced=self.pack_id)

    def _snapshot(self) -> RawTree:
        if self.target_window is None:
            self.refresh_windows()
        if self.target_window is None:
            raise SomnusError(ErrorCode.TARGET_NOT_FOUND, message="no window")
        return self.provider.snapshot(self.target_window)

    def _recompile(self) -> CompileResult:
        tree = self._snapshot()
        pack = self._current_pack()
        result = self.compiler.compile(tree, pack=pack)
        report = self.registry.reconcile(result)
        self.result = result
        # A fingerprint shared by several nodes cannot be resolved to one ref;
        # remember them so `call` can return TARGET_AMBIGUOUS (dev doc section 10).
        self._ambiguous_refs = {
            node.ref for node in result.root.walk() if node.fingerprint in set(report.ambiguous)
        }
        # A context menu is a floating popup: natively it is a sibling of the
        # whole window content, with nothing tying it to the control that opened
        # it. Re-parent it under that control so the tree says which node the
        # menu belongs to. Nothing to do until a menu has been opened.
        if self._menu_owner:
            self._reattach_open_menu(result)
            if _TRACE_STATES:
                log.info(
                    "menu mount: owner map=%s menus=%s",
                    self._menu_owner,
                    [n.ref for n in result.root.walk() if n.role == "menu"],
                )
        # `rev` increments only when the *semantic projection* changes (dev doc
        # section 9.1): a raw tree that repaints identically keeps the same rev.
        signature = self._projection_signature(result)
        if signature != self._last_signature:
            self._last_signature = signature
            self._bump_rev()
        return result

    # -------------------------------------------------------------- menu mount

    def _reattach_open_menu(self, result: CompileResult) -> None:
        """Move an open context menu under the node that opened it.

        The association is by *fingerprint*, not ref: a menu owner may be
        renamed or re-indexed between compilations (QQ's conversation rows carry
        the conversation title, which changes when the chat does), and a ref that
        drifted would silently mount the menu under the wrong node.

        A menu that is no longer present means the menu was dismissed, so the
        association is dropped here - the mount lasts exactly as long as the menu
        does, which is what makes "focus elsewhere to close the menu" work
        without any explicit unmount call.
        """
        present = {n.fingerprint for n in result.root.walk() if n.role == "menu"}
        # Drop owners whose menu is gone: a dismissed menu must not stay
        # associated with its node, or the next compilation would re-mount a
        # popup that no longer exists. Owners whose menu is still present are
        # kept, so opening a second menu does not forget the first.
        for fp in [fp for fp in self._menu_owner if fp not in present]:
            self._menu_owner.pop(fp, None)
        if not self._menu_owner:
            return

        by_fingerprint = {n.fingerprint: n for n in result.root.walk()}
        for menu in [n for n in result.root.walk() if n.role == "menu"]:
            owner_fp = self._menu_owner.get(menu.fingerprint)
            if owner_fp is None:
                continue
            owner = by_fingerprint.get(owner_fp)
            if owner is None:
                continue
            # Detach from wherever the popup was parented, then attach to the
            # owner. `owner` is looked up per menu, so two menus opened from two
            # different rows both end up under their own row.
            _detach(menu, result)
            menu.relations = [
                r for r in menu.relations if r.kind not in ("selected_in",)
            ]
            # The items' `selected_in` points at the menu container they were
            # enumerated under, which is the pre-mount ref. Re-point them at this
            # menu so the relation stays inside the mounted subtree and does not
            # reference a container that is no longer in the tree. `Relation` is
            # frozen, so the list is rebuilt rather than mutated.
            for item in menu.walk():
                item.relations = [
                    Relation(kind=r.kind, target=menu.ref)
                    if r.kind == "selected_in"
                    else r
                    for r in item.relations
                ]
            owner.children.append(menu)

    # ---------------------------------------------------------- popup (shell)

    def _render_popup_menu(self, owner_ref: str | None) -> str:
        """Render a shell-owned popup menu as if it hung under ``owner_ref``.

        Returns SUI-Text for the menu, indented under the owning node, so an
        agent sees where the menu came from. Empty string when no popup is
        present (the common case: the menu was drawn in-process and is already in
        the window's own tree).
        """
        getter = getattr(self.provider, "popup_menu_snapshot", None)
        if getter is None:
            return ""
        try:
            found = getter()
        except Exception as exc:  # noqa: BLE001 - a shell quirk must not fail the call
            log.debug("popup menu lookup failed: %s", exc)
            return ""
        if not found:
            return ""
        _window_id, tree = found

        # Compile the popup with the same pipeline as any window, so its nodes
        # get ordinary refs the agent can call.
        compiled = self.compiler.compile(tree, pack=None)
        # Register it with the session registry and mount it under the owning
        # node in *this* compilation. Without this the menu's refs would live
        # only inside the temporary compilation and the very next call would
        # come back STALE_TARGET - the menu would be printable but not usable.
        self.registry.reconcile(compiled)
        owner = None
        if owner_ref and self.result is not None:
            owner = self.result.find(owner_ref)
        if owner is not None:
            menu_nodes = [n for n in compiled.root.walk() if n.role == "menu"]
            if menu_nodes:
                menu = menu_nodes[0]
                # Fold the popup's refs/sources into the live result so the
                # resolver can find them.
                for node in compiled.root.walk():
                    src = compiled.sources.get(node.ref)
                    if src is not None:
                        self.result.sources[node.ref] = src
                    fp = compiled.fingerprints.get(node.ref)
                    if fp:
                        self.result.fingerprints[node.ref] = fp
                    if node.ref in compiled.members_by_ref:
                        self.result.members_by_ref[node.ref] = compiled.members_by_ref[node.ref]
                    raw = compiled.raw_by_ref.get(node.ref)
                    if raw is not None:
                        self.result.raw_by_ref[node.ref] = raw
                owner.children.append(menu)

        lines: list[str] = []
        for node in compiled.root.walk():
            if node is compiled.root:
                continue
            if node.role == "separator" or (node.role == "image" and not node.name):
                continue
            if not (node.name or node.role in ("menu", "menuitem")):
                continue
            depth = _depth_of(compiled.root, node)
            lines.append(
                INDENT * max(1, depth - 1)
                + render_node_line(node, RenderOptions(include_default_caps=False))
            )
        if not lines:
            return ""
        head = ""
        if owner is not None:
            head = INDENT + render_node_line(
                owner, RenderOptions(include_default_caps=False)
            ) + "\n"
        return head + "\n".join(lines)

    def _record_menu_owner(
        self,
        owner_ref: str | None,
        result: CompileResult,
        before_fingerprints: set[str],
    ) -> None:
        """Remember which node's context menu is now open (post-compile).

        Only a menu that was *not* in the tree before counts. A window may
        already contain menus that have nothing to do with this call - File
        Explorer's title bar exposes its system menu (`MENU "系统"`) permanently
        - and picking any menu would mount the window menu under the row that
        happened to be right-clicked (observed live).
        """
        if not owner_ref:
            return
        owner = result.find(owner_ref)
        if owner is None:
            return
        fresh = [
            n
            for n in result.root.walk()
            if n.role == "menu" and n.fingerprint not in before_fingerprints
        ]
        if _TRACE_STATES:
            log.info(
                "record-menu-owner ref=%s found=%s new_menus=%s",
                owner_ref,
                owner is not None,
                [n.ref for n in fresh],
            )
        for menu in fresh:
            self._menu_owner[menu.fingerprint] = owner.fingerprint
        if _TRACE_STATES:
            log.info("menu_owner now: %s", self._menu_owner)

    def _projection_signature(self, result: CompileResult) -> tuple:
        """A stable digest of the projected structure + the window set.

        The window list is part of it on purpose: when an application opens or
        closes a window (QQ opening a chat, a modal appearing), the target
        window's own tree may be unchanged, yet the screen the agent is looking
        at has changed and it must be told. Leaving the window set out of the
        signature means such a change never bumps ``rev`` and never shows up in
        a delta (dev doc sections 5, 9.1).
        """
        parts: list[tuple] = []
        for node in result.root.walk():
            attrs = tuple(sorted((k, str(v)) for k, v in node.attrs.items()))
            parts.append(
                (
                    node.fingerprint,
                    node.display_type,
                    node.name,
                    attrs,
                    tuple(sorted(node.states)),
                )
            )
        windows = tuple(
            sorted((w.window_id, w.title, w.process, w.active) for w in self.windows)
        )
        return (windows, tuple(parts))

    def _bump_rev(self) -> None:
        self.rev += 1

    # ------------------------------------------------------------------ observe

    def observe(
        self,
        window: str | None = None,
        *,
        level: int = 0,
        windows_only: bool = False,
        include_other_windows: bool = False,
    ) -> Observation:
        """Observe one window's content, or just the window list.

        The body is the target window only. Other windows are *not* listed by
        default: an agent that asked for one window's content should get that
        window, not a desktop dump it did not ask for and must pay tokens for
        (found live: 10 unrelated windows prefixed every observation). Use
        ``include_other_windows`` (or observe with ``windows_only``) when the
        desktop context is actually wanted.

        Observation is read-only and side-effect free, so an unknown window ref
        must not dead-end the agent: fall back to the default window and let the
        resulting overview show the real refs (dev doc section 6.4).

        ``windows_only`` answers "what is on my desktop right now" without
        dumping any tree, which is what a cross-window workflow needs before it
        decides where to go (dev doc section 5).
        """
        self.refresh_windows()
        if window:
            win = self._window_by_ref(window)
            if win is not None:
                self.target_window = win.window_id
        if self.target_window is None and self.windows:
            self.target_window = next(
                (w.window_id for w in self.windows if w.active), self.windows[0].window_id
            )

        if windows_only:
            # Report windows that appeared/vanished since the last look, and
            # bump rev so the agent notices the change (a bare list that looks
            # identical to the previous one carries no signal).
            window_delta = self._window_delta()
            if window_delta:
                self._bump_rev()
                text = f"@rev {self.rev}\n{window_delta}\n" + self._render_window_list()
            else:
                text = f"@rev {self.rev}\n" + self._render_window_list()
            return Observation(text=text, rev=self.rev, tokens=count_tokens(text))

        result = self._recompile()
        # Surface windows that appeared/vanished/renamed since the last look so
        # a cross-window workflow notices without a separate observation. The
        # first observation only establishes the baseline.
        had_baseline = bool(self._prev_windows)
        window_delta = self._window_delta()
        if not had_baseline:
            window_delta = ""
        # The desktop header is opt-in. When it is not requested the only
        # context carried is the window delta - a window appearing or vanishing
        # changes what the agent can act on, so it must still be reported even
        # in single-window mode.
        header: list[str] = []
        if include_other_windows:
            header = self._window_header(result)
        if window_delta:
            header.append(window_delta)
        # The header (other windows + deltas) consumes part of the observation
        # budget: reserve tokens for it so the total stays within the level's
        # cap instead of max_tokens + header (found live: Steam L0 came out at
        # 365 tokens against a 300 cap because the 74-token header was extra).
        budget = self.budget.for_level(level)
        header_tokens = count_tokens("\n".join(header)) if header else 0
        # +6 reserves room for the ``@rev N`` line the renderer prepends.
        body_budget = ObservationBudget(
            max_nodes=budget.max_nodes,
            max_depth=budget.max_depth,
            max_tokens=max(100, budget.max_tokens - header_tokens - 6),
            hard_tokens=budget.hard_tokens,
        )
        engine = ProjectionEngine(body_budget)
        projection = apply_projection(result.root, self._pack_projection())
        plan = engine.plan(result.root, projection)

        text = render_document(plan.lines, rev=self.rev, extra_header=header)
        return Observation(text=text, rev=self.rev, tokens=count_tokens(text))

    def _render_window_list(self) -> str:
        """The desktop as a flat list: every window, its ref and what it is.

        The refs are the same ones ``CALL wN.focus()`` accepts, so an agent can
        list, then switch, then observe - the way a person alt-tabs.
        """
        lines = [f"WINDOWS count={len(self.windows)}"]
        for w in self.windows:
            ref = self._header_window_ref(w.window_id)
            flags = []
            if w.window_id == self.target_window:
                flags.append("obs")
            if w.active:
                flags.append("active")
            suffix = (" " + " ".join(flags)) if flags else ""
            lines.append(f"  WINDOW {ref} {_q(w.title)}{suffix}")
        return "\n".join(lines)

    def _window_delta(self) -> str:
        """Delta lines for windows that appeared or disappeared.

        An application opening a window (QQ opening a chat, a modal appearing)
        changes what the agent can act on even when the target window's own tree
        is untouched. Reporting it as ``+ WINDOW w5 "Cart"`` / ``- WINDOW w3``
        is what lets a cross-window workflow continue without an explicit
        re-observation (dev doc section 5: L0 always lists windows).
        """
        current = {w.window_id: w.title for w in self.windows}
        previous = self._prev_windows
        if not previous:
            # First call only establishes the baseline. Allocating refs here
            # would hand the compiler's window ref (w1) to the wrong window and
            # break ``CALL w2.focus()`` resolution.
            self._prev_windows = current
            return ""
        # The target window is rendered under the tree's own window ref, not a
        # header-allocated one; use the same ref here so delta lines stay
        # consistent with the overview.
        target_ref = self.result.root.ref if self.result else None
        lines: list[str] = []
        for window_id, title in current.items():
            if window_id not in previous:
                if window_id == self.target_window and target_ref:
                    ref = target_ref
                else:
                    ref = self._header_window_ref(window_id)
                lines.append(f"+ WINDOW {ref} {_q(title)}")
        for window_id, title in previous.items():
            if window_id not in current:
                lines.append(f"- WINDOW {_q(title)}")
        for window_id, title in current.items():
            if window_id in previous and previous[window_id] != title:
                if window_id == self.target_window and target_ref:
                    ref = target_ref
                else:
                    ref = self._header_window_ref(window_id)
                lines.append(f"~ WINDOW {ref} {_q(previous[window_id])}→{_q(title)}")
        self._prev_windows = current
        return "\n".join(lines)

    def _window_header(self, result: CompileResult) -> list[str]:
        """Header lines for *other* windows (the target is rendered as the root).

        Window refs are assigned from the compiler's allocator so they never
        collide with node refs (dev doc section 5: window switching via
        ``CALL w2.focus()``). The target window uses the tree's root ref.
        """
        target_ref = result.root.ref if result.root else None
        out: list[str] = []
        for i, w in enumerate(self.windows):
            if w.window_id == self.target_window:
                # Register the target under the tree's window ref so CALL w?.focus
                # resolves to it. The compiler may have allocated a ref (its own
                # window node, e.g. `w2`) that the header already gave to another
                # window; rehome that window first, refs must stay unique.
                if target_ref:
                    owner = self._window_refs.get(target_ref)
                    if owner is not None and owner != w.window_id:
                        while True:
                            spare = self.registry.allocator.next("w")
                            if spare not in self._window_refs:
                                break
                        self._window_refs_by_id[owner] = spare
                        self._window_refs[spare] = owner
                    self._window_refs[target_ref] = w.window_id
                continue
            window_ref = self._header_window_ref(w.window_id)
            out.append(f"WINDOW {window_ref} {_q(w.title)}")
        return out

    def _header_window_ref(self, window_id: str) -> str:
        ref = self._window_refs_by_id.get(window_id)
        if ref is None:
            ref = self.registry.allocator.next("w")
            self._window_refs_by_id[window_id] = ref
            self._window_refs[ref] = window_id
        return ref

    def _pack_projection(self):
        """Projection rules for the current window: the generic defaults plus
        the selected pack's rules (pack rules win, they come later)."""
        from somnus.compiler.pack_schema import get_pack

        rules = []
        generic = get_pack("generic")
        if generic is not None:
            rules.extend(generic.projection)
        pack = self._current_pack()
        if pack is not None and pack.id != "generic":
            rules.extend(pack.projection)
        return rules

    # ------------------------------------------------------------------ find

    def find(self, action: Action, *, limit: int = 20) -> str:
        if self.result is None:
            self._recompile()
        assert self.result is not None
        nodes = list(self.result.root.walk())

        entity_filter = action.query_type
        scope = action.scope
        scope_ids: set[str] | None = None
        if scope:
            scope_node = self.result.find(scope)
            if scope_node is None:
                raise target_not_found(scope)
            scope_ids = {n.ref for n in scope_node.walk()}

        matches: list[_FindHit] = []
        query = action.query
        # Refs of nodes already covered by a matched entity.
        covered: set[str] = set()
        for node in nodes:
            if scope_ids is not None and node.ref not in scope_ids:
                continue
            if entity_filter and not (
                (node.entity or node.role) == entity_filter
                # A FIND by primitive role must also reach nodes that a pack
                # typed as domain entities (e.g. FIND button "Send" when the
                # qq pack typed the button as entity=send) - the agent may not
                # know the pack's entity vocabulary.
                or node.role == entity_filter
            ):
                continue
            if not _node_matches(node, query):
                continue
            # Without an explicit type, a matched entity hides its own text
            # children so the agent sees one line per entity, not the label
            # inside it (dev doc section 6.4).
            if node.entity is None and entity_filter is None:
                if node.role not in ("button", "input", "link", "menuitem") and not (
                    node.layer in ("screen", "dialog")
                ):
                    continue
            matches.append(_FindHit(node, is_entity=node.entity is not None))
            if node.entity is not None:
                covered.update(c.ref for c in node.children)
        matches = [m for m in matches if m.node.ref not in covered]

        if not matches:
            return "FOUND 0"
        out = [f"FOUND {len(matches)}"]
        opts = RenderOptions(include_default_caps=False)
        for hit in matches[:limit]:
            out.append(render_node_line(hit.node, opts))
        if len(matches) > limit:
            out.append(f"… +{len(matches) - limit} more")
        return "\n".join(out)

    # ------------------------------------------------------------------ inspect

    def inspect_direct(self, ref: str, *, depth: int | None = None) -> str:
        """Convenience wrapper: inspect a ref and return its SUI-Text."""
        return self.inspect(Action(kind="inspect", ref=ref, depth=depth)).text

    def inspect(self, action: Action) -> Observation:
        if self.result is None:
            self._recompile()
        assert self.result is not None
        node = self.result.find(action.ref or "")
        if node is None:
            raise target_not_found(action.ref or "")

        # Long text range read.
        if action.range and node.value is not None:
            start, end = action.range
            chunk = str(node.value)[start:end]
            text = (
                f"{node.display_type} {node.ref} "
                f"{render_node_line(node, RenderOptions(include_default_caps=True, show_relations=True))}\n"
                f'TEXT {node.ref} range={start}:{end} "{_esc(chunk)}"'
            )
            return Observation(text=text, rev=self.rev, tokens=count_tokens(text))

        opts = RenderOptions(include_default_caps=True, show_relations=True)
        depth = action.depth if action.depth is not None else self.budget.max_depth
        lines = [render_node_line(node, opts)]
        _render_subtree(node, lines, opts, depth, 1)
        text = f"@rev {self.rev}\n" + "\n".join(lines)
        return Observation(text=text, rev=self.rev, tokens=count_tokens(text))

    # ------------------------------------------------------------------ call

    def call(self, action: Action, *, confirmed: bool | None = None) -> CallOutcome:
        if self.result is None:
            self._recompile()
        assert self.result is not None

        # Window focus is a special session-level capability.
        win = self._window_by_ref(action.ref or "")
        if win is not None and action.capability == "focus":
            return self.focus_window(action.ref or "")

        # Stale check: the ref must still resolve in the current compilation.
        if self.result.find(action.ref or "") is None:
            self._recompile()
            assert self.result is not None
            if self.result.find(action.ref or "") is None:
                raise SomnusError(ErrorCode.STALE_TARGET, ref=action.ref)

        # Ambiguity check: the ref's fingerprint matched more than one element.
        if action.ref in self._ambiguous_refs:
            raise SomnusError(
                ErrorCode.TARGET_AMBIGUOUS,
                ref=action.ref,
                message="fingerprint matched multiple elements",
            )

        resolver = Resolver(self.result)
        before_index = index_by_fingerprint(self.result.root)

        resolution = self._resolve_with_hint(resolver, action)
        self.policy.check_process(self._target_process())
        self.policy.check_action(resolution, confirmed=confirmed)

        # Baseline for effect verification.
        verifier = Verifier(
            by_ref={n.ref: n for n in self.result.root.walk()},
            by_entity={
                n.entity: n for n in self.result.root.walk() if n.entity
            },
        )
        for name in (action.ref or "", *(e.target for e in resolution.effects)):
            if not name or name.startswith("$"):
                continue
            verifier.capture(name)
            entity_node = verifier.by_entity.get(name)
            if entity_node is not None:
                verifier.capture(entity_node.ref)

        if _TRACE_STATES:
            log.info(
                "call ref=%s cap=%s steps=%s effects=%s target_states=%s",
                action.ref,
                action.capability,
                [(s.primitive, s.value) for s in resolution.steps],
                [e.target for e in resolution.effects],
                sorted((self.result.find(action.ref or "").states)
                       if self.result.find(action.ref or "") else []),
            )

        # Execute steps in order, with a per-step timeout.
        app_api_values: list[Any] = []
        for step in resolution.steps:
            if step.primitive == "app_api":
                exec_result = self._invoke_app_api(step)
            else:
                exec_result = self.provider.execute(step.native, step.primitive, step.value)
            self._audit_action(action, step, exec_result)
            if not exec_result.ok and not exec_result.undetermined:
                code = exec_result.error or ErrorCode.NATIVE_EXECUTION_FAILED
                self.policy.check_native_permission(code)
                raise SomnusError(code, ref=action.ref, message=exec_result.message)
            if _TRACE_STATES:
                log.info(
                    "step primitive=%s ok=%s undetermined=%s error=%s msg=%s value=%r",
                    step.primitive,
                    exec_result.ok,
                    exec_result.undetermined,
                    exec_result.error,
                    (exec_result.message or "")[:120],
                    step.value,
                )
            if exec_result.undetermined:
                # A modal dialog may be opening; settle will capture it.
                break
            if step.primitive == "app_api" and exec_result.value is not None:
                # Keep the structured result so it can be reported back instead
                # of being scraped out of the UI (dev doc section 20).
                app_api_values.append(exec_result.value)

        # Settle, recompile, compute delta, verify. Refresh the window list
        # first: an action may have opened a dialog or replaced the target
        # window entirely (closing a document raises a save dialog), and the
        # delta must reflect the desktop as it is now.
        self.refresh_windows()
        try:
            tree, settle_result = self.settler.wait(
                lambda: self.provider.snapshot(self.target_window),  # type: ignore[arg-type]
                event_source=lambda: self._event_count,
            )
        except KeyError:
            # The target window vanished mid-action (e.g. the save dialog
            # replaced it). The action itself succeeded; report unverified
            # with a window delta instead of failing the whole call.
            self.refresh_windows()
            text = f"OK_UNVERIFIED rev={self.rev}\n{self._window_delta()}".strip()
            text += f"\n~ target replaced (window {self.target_window} closed)" if self._window_by_ref(self.target_window) is None else ""
            return CallOutcome(
                status="OK_UNVERIFIED",
                rev=self.rev,
                delta=Delta(),
                text=text,
                settled=False,
            )
        result = self.compiler.compile(tree, pack=self._current_pack())
        self.registry.reconcile(result)
        self.result = result
        self._bump_rev()

        if _TRACE_STATES:
            # The ref the agent used may be gone after a recompile; compare by
            # fingerprint so "did the button I was about to touch change state"
            # is answerable even when the ref shifted.
            before_node = verifier.by_ref.get(action.ref or "")
            after_node = None
            if before_node is not None:
                for node in result.root.walk():
                    if node.fingerprint == before_node.fingerprint:
                        after_node = node
                        break
            log.info(
                "after settled=%s timed_out=%s before_states=%s after_states=%s",
                settle_result.settled,
                settle_result.timed_out,
                sorted(before_node.states) if before_node else None,
                sorted(after_node.states) if after_node else "<ref gone>",
            )

        # A context_menu call opened a popup that has no native link to its
        # origin; remember the association and mount it *before* the delta is
        # computed, so the very response that opened the menu already shows it
        # under the node that owns it - the agent should not have to make a
        # second call to see what it just opened.
        popup_text = ""
        if any(s.primitive == "context_menu" for s in resolution.steps):
            self._record_menu_owner(
                action.ref,
                result,
                before_fingerprints=set(before_index),
            )
            if self._menu_owner:
                self._reattach_open_menu(result)
            else:
                # Nothing mounted, so the menu is not part of this window's own
                # tree. The classic Win32 Shell menu is a separate top-level
                # popup window (#32768), which would otherwise leave the agent
                # with "context_menu succeeded" and nothing to act on.
                popup_text = self._render_popup_menu(action.ref)

        after_index = index_by_fingerprint(result.root)
        delta = diff_trees(before_index, after_index)

        verify = verifier.verify(resolution.effects, action.ref or "", delta)
        if verify.status == "EFFECT_NOT_CONFIRMED":
            raise SomnusError(
                ErrorCode.EFFECT_NOT_CONFIRMED,
                ref=action.ref,
                message="; ".join(verify.failed),
            )

        text = f"{verify.status} rev={self.rev}"
        if not settle_result.settled:
            text += " settled=false"
        # An app-API step can return structured data; report it directly rather
        # than making the caller scrape it back out of the UI.
        if app_api_values:
            import json as _json

            for value in app_api_values:
                rendered = _json.dumps(value, ensure_ascii=False)
                if len(rendered) > 2000:
                    rendered = rendered[:2000] + "…"
                text += f"\n= {rendered}"
        body = self._window_delta()
        tree_body = self.delta_text(delta)
        if popup_text:
            # A popup menu is rendered as a subtree of the node that opened it,
            # so the agent reads one coherent structure either way - whether the
            # application draws its menu in-process or hands it to the shell.
            tree_body = f"{tree_body}\n{popup_text}" if tree_body else popup_text
        if tree_body:
            body = f"{body}\n{tree_body}" if body else tree_body
        if body:
            text += "\n" + body
        return CallOutcome(
            status=verify.status,
            rev=self.rev,
            delta=delta,
            text=text,
            settled=settle_result.settled,
        )

    def delta_text(self, delta: Delta) -> str:
        return delta.render()

    # ------------------------------------------------------------------ helpers

    def _resolve_with_hint(self, resolver: "Resolver", action: Action):
        """Resolve an action, turning a missing capability into a hint that names
        the nodes which do expose it (e.g. `add` lives on the product, not the
        cart), so the agent can retarget instead of looping."""
        from somnus.errors import ErrorCode as _E

        try:
            return resolver.resolve(action)
        except SomnusError as err:
            if err.code == _E.CAPABILITY_NOT_FOUND and action.capability:
                holders = self._capability_holders(action.capability)
                if holders:
                    err.message = (
                        f"{action.ref} has no {action.capability}; "
                        f"try one of: {', '.join(holders)}"
                    )
            raise

    def _invoke_app_api(self, step) -> "Any":
        """Route an ``app_api`` step to the target application's own interface.

        Resolution is by the session's process, so the same capability works
        whether the app is driven through its UI or its API; the LLM never sees
        which path was taken (dev doc section 20).
        """
        from somnus.appapi import REGISTRY
        from somnus.ir.action import ExecResult

        handler = REGISTRY.for_process(self._target_process())
        if handler is None:
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message=f"no app api for {self._target_process() or 'unknown'}",
            )
        return handler.invoke(step.app_action or "", dict(step.app_params))

    def _capability_holders(self, capability: str, *, limit: int = 3) -> list[str]:
        """Refs (with names) of nodes that expose ``capability``."""
        if self.result is None:
            return []
        out: list[str] = []
        for node in self.result.root.walk():
            if capability in node.cap_names():
                label = node.ref + (f" ({node.name})" if node.name else "")
                out.append(label)
            if len(out) >= limit:
                break
        return out

    def _target_process(self) -> str:
        """The process the *current compilation* belongs to.

        This must come from the compiled result, not from a fresh window lookup:
        a window id is only meaningful for the enumeration that produced it, and
        apps come and go between the observe and the call. Re-resolving the id
        could return a completely different window and make the policy check
        approve or deny the wrong process (found live: a Notepad call failed
        PERMISSION_DENIED against `explorer.exe` because the id had moved).
        """
        if self.result is not None and self.result.process:
            return self.result.process
        win = self._window_by_ref(self.target_window or "")
        return win.process if win else ""

    def _audit_action(self, action: Action, step, exec_result) -> None:
        self._audit.append(
            {
                "ts": time.time(),
                "rev": self.rev,
                "ref": action.ref,
                "capability": action.capability,
                "primitive": step.primitive,
                "ok": exec_result.ok,
            }
        )

    def audit_log(self) -> list[dict]:
        return list(self._audit)

    def close(self) -> None:
        self.provider.close()

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, text: str, *, confirmed: bool | None = None) -> str:
        """Parse and execute a single SUI-Text command, returning SUI-Text."""
        try:
            action = parse_action(text)
        except SomnusError as err:
            return err.render()
        return self.run(action, confirmed=confirmed)

    def dispatch_lines(
        self, block: str, *, confirmed: bool | None = None
    ) -> str:
        """Execute a plain-text block of SUI-Text commands, one per line.

        This is the transport for models without function calling (dev doc
        section 6.4): the same syntax and the same parser, just line-delimited
        instead of a tool call. Blank lines and ``#`` comments are ignored.
        """
        out: list[str] = []
        for raw in block.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            out.append(self.dispatch(line, confirmed=confirmed))
        return "\n".join(out)

    def run(self, action: Action, *, confirmed: bool | None = None) -> str:
        try:
            if action.kind == "observe":
                return self.observe(
                    action.window,
                    windows_only=(action.query == "windows"),
                    include_other_windows=(action.scope == "desktop"),
                ).text
            if action.kind == "find":
                return self.find(action)
            if action.kind == "inspect":
                return self.inspect(action).text
            if action.kind == "call":
                return self.call(action, confirmed=confirmed).text
            if action.kind == "unknown":
                return f'ERR {ErrorCode.UNKNOWN_CONTENT} hint=report-unknown msg="{action.query}"'
            raise SomnusError(ErrorCode.PARSE_ERROR, message=action.kind)
        except SomnusError as err:
            return err.render()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("dispatch failed")
            return SomnusError(
                ErrorCode.NATIVE_EXECUTION_FAILED, message=str(exc)
            ).render()


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------


def _node_matches(node: SemanticNode, query: str) -> bool:
    if not query:
        return True
    q = query.casefold().strip()
    # Match name, value, attrs, and aliases.
    haystacks = [node.name or "", str(node.value or "")]
    haystacks.extend(str(v) for v in node.attrs.values())
    if any(q in h.casefold() for h in haystacks):
        return True
    # Token subset match ("big mac" matches "Big Mac Meal").
    tokens = [t for t in q.split() if t]
    joined = " ".join(haystacks).casefold()
    return bool(tokens) and all(t in joined for t in tokens)


def _render_subtree(node: SemanticNode, lines: list[str], opts: RenderOptions, depth: int, indent: int) -> None:
    if depth <= 0:
        if node.children:
            lines.append("  " * indent + f"… +{len(node.children)} more")
        return
    for child in node.children:
        lines.append("  " * indent + render_node_line(child, opts))
        _render_subtree(child, lines, opts, depth - 1, indent + 1)


def _detach(node: SemanticNode, result: CompileResult) -> None:
    """Remove ``node`` from whatever parent currently holds it."""
    for parent in result.root.walk():
        if node in parent.children:
            parent.children.remove(node)
            return


def _depth_of(root: SemanticNode, target: SemanticNode) -> int:
    """Depth of ``target`` below ``root`` (root itself is 0)."""
    stack: list[tuple[SemanticNode, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        if node is target:
            return depth
        for child in node.children:
            stack.append((child, depth + 1))
    return 0


def _q(text: str | None) -> str:
    if text is None:
        return '""'
    return '"' + text.replace('"', '\\"') + '"'


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
