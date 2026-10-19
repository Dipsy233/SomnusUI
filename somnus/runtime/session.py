"""Session: the runtime orchestrator behind the LLM tools.

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
import re
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

#: Primitives that prepare the element for the step after them. They are only
#: preparatory when another step follows: `focus` then `type_text` means "focus
#: it, then type", but a capability whose *last* step is `focus` (or
#: `scroll_into_view`) really is doing that thing. Treating a successful
#: preparation as completion reported success for an action that never ran.
_PREPARATORY_PRIMITIVES = frozenset({"focus", "scroll_into_view"})

#: Primitives that write text into a control. After one of these succeeds the
#: response echoes the exact value written, so the agent can confirm what
#: landed without a follow-up `inspect`: the delta only shows a `value` change
#: when the value actually differed, and a synthetic write (`paste_text`,
#: `type_text`) may not be readable back through UIA at all.
_WRITE_PRIMITIVES = frozenset({"set_text", "paste_text", "type_text"})


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
        #: A provider that declares itself deterministic cannot move between the
        #: action and the settle snapshot, so the quiet window would sleep for
        #: nothing. Only opt the settler into skipping when the provider says so;
        #: an explicit ``settler=`` argument still wins, so a test can pin the
        #: behaviour either way.
        self._settle_deterministic = bool(
            getattr(provider, "is_deterministic", False)
        )
        self.settler = settler or Settler(
            skip_when_deterministic=self._settle_deterministic
        )
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
        #: fingerprint -> the primitive this element actually answered to.
        #: Learned at execution time, because which native interface works is
        #: not knowable from the patterns alone (UIA reports interfaces the
        #: element then refuses). Kept for the session, so a repeated call on
        #: the same element skips the interfaces it already declined.
        self._route_memory: dict[str, str] = {}
        #: menu ref -> the ref of the node whose context menu it is. A context
        #: menu is a separate native popup with no structural link to the control
        #: that opened it, so the association has to be remembered here; the
        #: projection then re-parents the menu under its owner, which is how the
        #: menu reads as belonging to something instead of floating at window
        #: level. Cleared automatically once the menu disappears (see
        #: :meth:`_reattach_open_menu`).
        self._menu_owner: dict[str, str] = {}
        #: Window list snapshot taken right before each call executes.
        self._windows_before_action: list = []
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
        # The target window is also known through its pinned root ref: the
        # recompile path registers id -> ref but deliberately never writes
        # ref -> id (that map belongs to focus_window/observe). Reverse-lookup
        # here so `CALL w1.focus()` resolves on Android, whose native ids are
        # never ref-shaped ("0/6835"). Every by-id entry is a ref the session
        # itself allocated, so a hit is trusted regardless of which window is
        # currently the target (that is the whole point of switching back).
        for window_id, pinned_ref in self._window_refs_by_id.items():
            if pinned_ref == ref:
                return next((w for w in self.windows if w.window_id == window_id), None)
        return None

    def _peek(self, window_id: str) -> WindowInfo | None:
        """Refresh one window's metadata, raising its window first.

        The provider's ``peek`` already puts the window in the foreground (a
        covered or minimized self-drawn window renders nothing, so its tree
        would come back as a bare shell). A provider without ``peek`` (a
        third-party one, a test double) is asked to focus the window
        explicitly, which keeps the guarantee without a second provider API.
        """
        peek = getattr(self.provider, "peek", None)
        if peek is not None:
            try:
                return peek(window_id)
            except KeyError:
                return None
        focus = getattr(self.provider, "focus_window", None)
        if focus is not None:
            try:
                focus(window_id)
            except Exception:  # noqa: BLE001 - advisory, the read still happens
                pass
        try:
            windows = self.provider.list_windows()
        except Exception:  # noqa: BLE001 - a dead provider is reported by observe/call
            return None
        return next((w for w in windows if w.window_id == window_id), None)

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
        # A single-screen provider (a phone) is always showing the user
        # exactly one screen: observe/find/inspect/call must describe *that*
        # screen, not a window pinned earlier. Observing a background window
        # returns a bare shell (MIUI home behind another app publishes an
        # empty tree), which read as "the observation lost everything". The
        # provider declares this behaviour with ``follows_active_window``; a
        # desktop keeps the pinned target (a desktop really does have several
        # visible windows at once).
        if getattr(self.provider, "follows_active_window", False):
            self.refresh_windows()
            active = next((w for w in self.windows if w.active), None)
            if active is not None:
                self.target_window = active.window_id
        if self.target_window is None:
            self.refresh_windows()
        if self.target_window is None:
            raise SomnusError(ErrorCode.TARGET_NOT_FOUND, message="no window")
        try:
            return self.provider.snapshot(self.target_window)
        except KeyError:
            # The target window closed (Android window ids are per-activity:
            # BACK from a settings subpage retires the id). Windows-side the
            # same situation resolves through refresh_windows; here the id is
            # simply gone from the enumeration, so re-pick the active window
            # instead of dead-ending the agent with a bare KeyError.
            self.refresh_windows()
            if self.target_window in {w.window_id for w in self.windows}:
                return self.provider.snapshot(self.target_window)
            active = next((w.window_id for w in self.windows if w.active), None)
            if active is None:
                raise SomnusError(ErrorCode.TARGET_NOT_FOUND, message="no window") from None
            self.target_window = active
            return self.provider.snapshot(active)

    def _recompile(self) -> CompileResult:
        tree = self._snapshot()
        pack = self._current_pack()
        # Pin the window's ref: the session owns the wN namespace (the window
        # list and CALL wN.focus() hand it to the agent), so recompiling the
        # same window must not mint a fresh name for it.
        pinned = self._window_refs_by_id.get(self.target_window or "")
        result = self.compiler.compile(tree, pack=pack, window_ref=pinned)
        report = self.registry.reconcile(result)
        self.result = result
        # Keep the id -> ref pin fresh: `CALL wN.focus()` needs the ref the
        # tree's root was compiled under, and on Android the native window id
        # ("0/6835") is never ref-shaped, so the Windows flow's accidental
        # pass-through (window ids literally being "wN") does not carry over
        # (found live: CALL w1.focus() errored with CAPABILITY_NOT_FOUND on a
        # phone). Only the id -> ref direction is written here; the ref -> id
        # map belongs to focus_window/observe, which own the agent-facing
        # namespace - writing it from a recompile would hijack a ref the agent
        # already used for another window.
        if self.target_window and self.target_window not in self._window_refs.values():
            self._window_refs_by_id[self.target_window] = result.root.ref
        # A fingerprint shared by several nodes cannot be resolved to one ref;
        # remember them so `call` can return TARGET_AMBIGUOUS.
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
        # `rev` increments only when the *semantic projection* changes: a raw
        # tree that repaints identically keeps the same rev.
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

    def _mount_popup_content(self, owner_ref: str | None) -> str:
        """Capture a shell-owned popup (menu or tray panel) and mount it.

        Returns SUI-Text for the popup, indented under the owning node, so an
        agent sees where it came from. Empty string when no popup is present
        (the common case: the menu was drawn in-process and is already in the
        window's own tree).

        The popup's nodes are folded into the live result even when there is no
        clean ``menu`` node to hang (the tray overflow panel is a list of
        buttons), so find/inspect/call keep working on the popup's refs after
        this call - not just in this one response.
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
            # Fold the popup's refs/sources into the live result so the
            # resolver can find them - regardless of shape. Menus are then
            # additionally re-parented under the owner; the tray panel's
            # buttons are reachable through their folded refs alone.
            popup_roots: list = []
            menu_nodes = [n for n in compiled.root.walk() if n.role == "menu"]
            if menu_nodes:
                popup_roots.append(menu_nodes[0])
            else:
                # No menu node: mount the whole popup subtree under the owner
                # so the delta/observe shows a coherent structure.
                popup_roots.extend(c for c in compiled.root.children)
            for top in popup_roots:
                for node in top.walk():
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
                owner.children.append(top)

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
        a delta.
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
        full: bool = False,
        expanded: set[int] | None = None,
    ) -> Observation:
        """Observe one window's content, or just the window list.

        The body is the target window only. Other windows are *not* listed by
        default: an agent that asked for one window's content should get that
        window, not a desktop dump it did not ask for and must pay tokens for
        (found live: 10 unrelated windows prefixed every observation). Use
        ``include_other_windows`` (or observe with ``windows_only``) when the
        desktop context is actually wanted.

        Observing a window brings it to the foreground. A window that is behind
        another one - or minimized - is not reliably *rendered*, and a
        self-drawn surface (Chromium/Electron/Qt Quick) publishes an empty
        tree until it is: the observation would silently be missing most of the
        UI. Taking the foreground is the price of a complete tree; an agent
        that must not disturb the user belongs in the child session, which has
        its own foreground window (see docs/child-session.md).

        Observation is read-only and side-effect free, so an unknown window ref
        must not dead-end the agent: fall back to the default window and let the
        resulting overview show the real refs.

        ``windows_only`` answers "what is on my desktop right now" without
        dumping any tree, which is what a cross-window workflow needs before it
        decides where to go. It is also the one path that
        raises nothing: a window *list* needs no window rendered, so it does not
        seize the foreground just to answer.

        ``full`` (``OBSERVE w5 full``) renders the whole window tree without
        any collapse or budget. ``expanded`` is the set of node ids the agent
        unfolded this round (``OBSERVE w5 expand g3``); only those nodes are
        opened, the rest keeps its folded rendering.
        """
        self.refresh_windows()
        if window:
            win = self._window_by_ref(window)
            if win is not None:
                self.target_window = win.window_id
                # Remember the name the agent used. It came from the window
                # list, so it is the name this window goes by: recompiling
                # without it handed the same window a fresh ref and left the
                # one the agent was told to use pointing nowhere.
                self._window_refs[window] = win.window_id
                self._window_refs_by_id[win.window_id] = window
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

        # Raise the target before reading it (and before the header reports its
        # active flag, which is only true after the raise). `_recompile` then
        # snapshots a window that is actually on screen.
        win = self._peek(self.target_window) if self.target_window else None
        if win is not None:
            # Refresh in place: the window list is ordered (refs and window
            # numbering are allocated from it), so the target keeps its slot.
            self.windows = [win if w.window_id == win.window_id else w for w in self.windows]

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
        header: list[str] = [self._screen_line()]
        if include_other_windows:
            header.extend(self._window_header(result))
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
        engine = ProjectionEngine(body_budget, l0=(level == 0))
        projection = apply_projection(result.root, self._pack_projection())
        plan = engine.plan(
            result.root,
            projection,
            full=full,
            expanded=expanded,
        )

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

    def _screen_line(self) -> str:
        """``SCREEN <id>`` - what the phone is showing right now, in one token.

        The agent should not have to parse package names to know where it is:
        `SCREEN home` says the launcher, `SCREEN qq` says QQ. The id is the
        app's launcher label when the Portal knows it (lower-cased, spaces
        folded), the package's last segment otherwise, and `home` for any
        launcher package. This is also the id `SWITCH launch` accepts back.
        """
        win = next(
            (w for w in self.windows if w.window_id == self.target_window), None
        )
        pkg = (win.process if win else "") or ""
        if not pkg:
            return 'SCREEN unknown ""'
        if "launcher" in pkg.lower() or pkg.endswith(".home") or pkg == "com.miui.home":
            return "SCREEN home"
        label = None
        lister = getattr(self.provider, "list_apps", None)
        if lister is not None:
            try:
                apps = lister()
            except Exception:  # noqa: BLE001 - a dead app list must not kill observe
                apps = []
            label = next(
                (a.get("label") for a in apps if a.get("package") == pkg), None
            )
        if not label:
            label = pkg.rsplit(".", 1)[-1]
        return "SCREEN " + "_".join(label.split()).lower()

    def _window_delta(self) -> str:
        """Delta lines for windows that appeared or disappeared.

        An application opening a window (QQ opening a chat, a modal appearing)
        changes what the agent can act on even when the target window's own tree
        is untouched. Reporting it as ``+ WINDOW w5 "Cart"`` / ``- WINDOW w3``
        is what lets a cross-window workflow continue without an explicit
        re-observation (L0 always lists windows).
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
        collide with node refs (window switching via
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

    def find(self, action: Action, *, limit: int = 50) -> str:
        # Find always re-snapshots: the screen may have moved since the last
        # observation (the user navigated, or the app refreshed itself), and
        # find's whole job is to hand out refs the very next call can use -
        # serving them from the previous compilation meant find returned a
        # node that call then rejected as STALE_TARGET (found live: TIM's
        # conversation rows kept being handed out after the chat page had
        # replaced the list). Refs survive a recompile when the element does
        # (the registry reconciles by fingerprint), so the refresh cannot
        # invalidate a ref that is still valid.
        self._recompile()
        assert self.result is not None
        nodes = list(self.result.root.walk())

        entity_filter = action.query_type
        scope = action.scope
        scope_ids: set[str] | None = None
        if scope:
            scope_node = self.result.find(scope)
            if scope_node is None:
                # ``FIND w5 "text"`` - a window ref scopes the search to that
                # window. If the ref is not a node in the current compilation,
                # switch the target window instead (the searched window may not
                # be the one being observed).
                win = self._window_by_ref(scope)
                if win is not None:
                    if win.window_id != self.target_window:
                        self.target_window = win.window_id
                        self._recompile()
                        nodes = list(self.result.root.walk())  # type: ignore[union-attr]
                    else:
                        scope_ids = None  # whole target window
                else:
                    raise target_not_found(scope)
            else:
                scope_ids = {n.ref for n in scope_node.walk()}

        matches: list[SemanticNode] = []
        seen_fingerprints: set[str] = set()
        query = action.query
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
            # Full-text search: the query may name a text label, so anonymous
            # structural nodes are skipped, but a named node of ANY role is a
            # valid hit - restricting to a handful of roles made "FIND \"库\""
            # miss a TEXT navigation tab that was right there (found live:
            # Steam's library tab is a TEXT node, FIND returned 0).
            if node.entity is None and not node.name:
                continue
            # The same element can appear as an entity row AND as a named
            # child (a game row and its title text both contain the name);
            # one hit per identity, parents preferred.
            if node.fingerprint in seen_fingerprints:
                continue
            seen_fingerprints.add(node.fingerprint)
            matches.append(node)
        # Drop nodes whose parent is also a match: the row line already carries
        # the name, so reporting both is noise, not information.
        by_fp = {n.fingerprint: n for n in nodes}
        parent_fps: set[str] = set()
        for node in matches:
            parent = _find_parent(result_root(self), node)
            if parent is not None and parent.fingerprint in seen_fingerprints:
                parent_fps.add(node.fingerprint)
        matches = [m for m in matches if m.fingerprint not in parent_fps]

        if not matches:
            # A bare `FOUND 0` dead-ends the agent. Name the top-level
            # containers of the current window so the next query can be
            # scoped (e.g. `FIND "..." in l2`) or an observe can re-orient.
            containers = [
                f"{n.display_type} {n.ref}"
                + (f" {_q(n.name)}" if n.name else "")
                for n in self.result.root.walk()
                if n.children and (n.name or n.role in ("list", "tree", "table", "pane", "screen"))
            ][:5]
            hint = "\n".join(f"  {c}" for c in containers)
            return "FOUND 0\n" + (
                "containers you can scope into (FIND \"\u2026\" in REF) or INSPECT:\n" + hint
                if hint
                else "window appears empty; OBSERVE to re-orient"
            )
        out = [f"FOUND {len(matches)}"]
        # Full capability line: with default caps omitted (the grammars' implicit
        # table), a bare `+context_menu`-only line made the agent believe
        # activate was unavailable (found live: the Steam game row showed only
        # context_menu and the agent right-clicked instead of opening).
        opts = RenderOptions(include_default_caps=True, show_relations=False)
        root = result_root(self)
        prev_chain: list[str] | None = None
        for hit in matches[:limit]:
            out.append(render_node_line(hit, opts))
            # Breadcrumb: where this hit lives in the tree. A bare entity line
            # answers *what* matched but not *where* - without it, every
            # "确定" (OK) on screen is indistinguishable. The chain names the
            # window down to the parent (e.g. WINDOW w1 "系统设置" > t4 "是否删除"),
            # and carries refs so the next action can scope into an ancestor
            # (FIND "…" in t4) without another inspect.
            chain = _ancestor_chain(root, hit)
            chain_str = " > ".join(_breadcrumb_entry(n) for n in chain)
            if chain_str:
                # Consecutive hits under the same parent repeat the same
                # chain; collapsing the duplicates keeps a 30-row list find
                # from doubling in size while still telling the agent which
                # subtree each hit came from. Hits that differ only in the
                # tail (siblings) show just the shared prefix elided.
                if prev_chain is not None:
                    shared = 0
                    for a, b in zip(prev_chain, chain):
                        if a != b:
                            break
                        shared += 1
                    # Siblings differ only in the tail: show just the shared
                    # prefix elided. An identical chain still repeats in full -
                    # a hit with no breadcrumb at all reads as "unknown
                    # parent", which is exactly what the chain exists to fix.
                    if 2 <= shared < len(chain):
                        tail = " > ".join(_breadcrumb_entry(n) for n in chain[shared:])
                        out.append(f"  in: … > {tail}")
                        prev_chain = chain
                        continue
                out.append(f"  in: {chain_str}")
                prev_chain = chain
            else:
                prev_chain = None
        if len(matches) > limit:
            out.append(f"… +{len(matches) - limit} more")
        return "\n".join(out)

    # ------------------------------------------------------------------ inspect

    def inspect_direct(self, ref: str, *, depth: int | None = None) -> str:
        """Convenience wrapper: inspect a ref and return its SUI-Text."""
        return self.inspect(Action(kind="inspect", ref=ref, depth=depth)).text

    def inspect(self, action: Action) -> Observation:
        # Same rule as find: inspect re-snapshots first, so what it renders
        # (and the refs it names) describe the screen as it is now, not as it
        # was when the agent last observed. The ref the agent asked for is
        # resolved after the refresh; a ref that died with the old screen is
        # reported as STALE_TARGET rather than silently showing a stranger.
        was = self.result.find(action.ref or "") if self.result is not None else None
        self._recompile()
        assert self.result is not None
        node = self.result.find(action.ref or "")
        if node is None:
            if was is not None:
                raise SomnusError(
                    ErrorCode.STALE_TARGET,
                    ref=action.ref,
                    message=(
                        f"was {was.display_type} "
                        + (f"{was.name!r} " if was.name else "")
                        + "; the screen changed - re-observe"
                    ),
                )
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
        # Re-snapshot before acting. find/inspect already refresh for the same
        # reason; call is where a stale ref does damage: the Portal resolves a
        # node key by walking the live tree by index, so a key captured before
        # the screen moved lands on whatever occupies that path NOW (found
        # live: a tap aimed at a folder icon opened an unrelated app after the
        # desktop re-paged between the observation and the call). The
        # registry reconciles by fingerprint, so a ref whose element survives
        # still points at it and carries a FRESH node key; one whose element
        # died raises STALE_TARGET below instead of mis-firing. rev only
        # moves when the semantic projection changes, so an unchanged screen
        # costs one snapshot and nothing else.
        self._recompile()
        assert self.result is not None

        # Window focus is a special session-level capability. The ref -> id
        # map only knows refs the agent used for switching; the target window
        # is always focusable by its current root ref (w1 below) even before
        # any switch happened, so fall back to the pinned by-id ref.
        win = self._window_by_ref(action.ref or "")
        if (
            win is None
            and action.capability == "focus"
            and self._window_refs_by_id.get(self.target_window or "") == action.ref
        ):
            win = next((w for w in self.windows if w.window_id == self.target_window), None)
        if win is not None and action.capability == "focus":
            return self.focus_window(action.ref or "")

        # Stale check: the ref must resolve in the FRESH compilation (the
        # snapshot above); the registry kept refs whose element survived.
        if self.result.find(action.ref or "") is None:
            # Tell the agent what the ref *was*, so it can decide whether
            # the element moved (find again) or is gone (give up).
            record = self.registry.records.get(action.ref or "")
            if record is not None:
                raise SomnusError(
                    ErrorCode.STALE_TARGET,
                    ref=action.ref,
                    message=(
                        f"was {record.role} "
                        + (f"{record.name!r} " if record.name else "")
                        + "; refind by name (FIND) or observe"
                    ),
                )
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
        #: Window list before the action: a context_menu may open its menu as
        #: a separate top-level window, and comparing the two lists is the only
        #: reliable way to see that (the in-window delta cannot).
        self._windows_before_action = list(self.windows)

        resolution = self._resolve_with_hint(resolver, action)
        self.policy.check_process(self._target_process())
        self.policy.check_action(resolution, confirmed=confirmed)

        # Raise the target before acting. The primitive runs against the window
        # and whatever it has rendered; a covered or minimized self-drawn window
        # may have rendered nothing, and some patterns (ShowContextMenu, Invoke
        # on a self-drawn control) act on the rendering. The provider performs
        # the raise itself for every primitive; doing it here as well means the
        # window is already in front while the effect baseline is captured, so
        # the "did anything change" comparison is not fooled by the raise.
        if self.target_window:
            self._peek(self.target_window)

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

        # Execute the declared routes in order. A capability may carry several
        # `via` steps (write through the Value pattern, *else* focus and type)
        # and the resolver keeps the ones the node's patterns allow. Which one
        # actually works is a fact about the live element that no amount of
        # static inspection can settle - UIA reports patterns the element then
        # refuses (measured: a Chromium editable advertises `value` and its
        # setter is a no-op shell) - so the chain is walked here, at execution
        # time, and the first route that *works* wins.
        #
        # Only a missing interface advances the chain. A pattern that exists
        # and fails (disabled, UIPI, a raised COM error) is a real answer about
        # this element and is reported instead: silently trying something else
        # would be the runtime overruling what it was told to do.
        exec_result = None
        route = ""
        written: Any = None
        ordered_steps = self._order_steps(action.ref or "", resolution.steps)
        denied: list[str] = []
        for index, step in enumerate(ordered_steps):
            # Authorisation is per step: a capability may declare a native
            # route plus a synthetic-input fallback, and only the fallback
            # needs the host's consent.
            #
            # A refused step is *skipped*, not fatal. A capability declares its
            # routes as alternatives - paste, else type - and a host that
            # enabled neither has authorised none of them; failing the call on
            # the first one would hide the routes further down the chain and
            # report "unsupported" for a capability whose last declared route is
            # perfectly allowed. The refusals are collected instead, so that a
            # chain where *every* route was denied still names what was denied
            # and how to enable it.
            try:
                self.policy.check_step(step.primitive)
            except SomnusError as err:
                denied.append((step.primitive, err))
                continue
            exec_result = self.provider.execute(step.native, step.primitive, step.value)
            self._audit_action(action, step, exec_result)
            is_last = index == len(ordered_steps) - 1
            if _TRACE_STATES:
                log.info(
                    "step primitive=%s ok=%s unsupported=%s undetermined=%s "
                    "error=%s msg=%s value=%r last=%s",
                    step.primitive,
                    exec_result.ok,
                    exec_result.unsupported,
                    exec_result.undetermined,
                    exec_result.error,
                    (exec_result.message or "")[:120],
                    step.value,
                    is_last,
                )
            if exec_result.undetermined:
                # The call may still be in flight (a modal dialog opening); no
                # later step can be judged yet, so this is the end of the chain.
                route = step.primitive
                break
            if exec_result.ok:
                if step.primitive in _PREPARATORY_PRIMITIVES and not is_last:
                    # Preparation for the step that follows: focus succeeded,
                    # so now the actual action can run. Stopping here would
                    # report success for an action that never took place.
                    if _TRACE_STATES:
                        log.info("preparatory step %s done, continuing", step.primitive)
                    continue
                route = step.primitive
                self._remember_route(action.ref or "", step.primitive)
                if step.primitive in _WRITE_PRIMITIVES:
                    written = step.value
                break
            if exec_result.unsupported:
                # This element does not offer this interface; try the next
                # declared route rather than reporting a dead end.
                continue
            code = exec_result.error or ErrorCode.NATIVE_EXECUTION_FAILED
            self.policy.check_native_permission(code)
            raise SomnusError(code, ref=action.ref, message=exec_result.message)
        else:
            # Every declared route was absent. That is the honest
            # ACTION_NOT_SUPPORTED - the element offers none of them - and it
            # carries the primitives that were tried so the failure is
            # diagnosable.
            if exec_result is not None and exec_result.unsupported:
                tried = "/".join(s.primitive for s in ordered_steps)
                raise SomnusError(
                    ErrorCode.ACTION_NOT_SUPPORTED,
                    ref=action.ref,
                    message=f"no usable interface on this element (tried {tried})",
                )
            if exec_result is None:
                # Nothing was even attempted: every route in the chain was
                # refused by policy. Report the refusal that names the *last*
                # route (the most capable one the chain ends on), and list what
                # was refused so the agent knows the whole chain, not just one
                # flag.
                if denied:
                    # Name the *last* route's refusal: the chain ends on the most
                    # capable fallback (typing), so that is the flag whose absence
                    # is the real obstacle. Every refused route is then listed with
                    # its own message, so a host that enabled neither sees both
                    # flags rather than only the last one.
                    _primitive, last_err = denied[-1]
                    detail = "; ".join(
                        f"{primitive}: {err.message}" for primitive, err in denied
                    )
                    raise SomnusError(
                        ErrorCode.ACTION_NOT_SUPPORTED,
                        ref=action.ref,
                        message=f"{last_err.message} (refused routes: {detail})",
                    )
                raise SomnusError(ErrorCode.ACTION_NOT_SUPPORTED, ref=action.ref)
            code = exec_result.error or ErrorCode.NATIVE_EXECUTION_FAILED
            self.policy.check_native_permission(code)
            raise SomnusError(code, ref=action.ref, message=exec_result.message)

        if exec_result is not None and exec_result.undetermined:
            # A modal dialog may be opening; settle will capture it.
            pass

        # Settle, recompile, compute delta, verify. Refresh the window list
        # first: an action may have opened a dialog or replaced the target
        # window entirely (closing a document raises a save dialog), and the
        # delta must reflect the desktop as it is now.
        self.refresh_windows()
        # Re-raise before the settle snapshot: the action (or the user) may have
        # moved the foreground away, and the settled tree is only complete for a
        # window that is rendered.
        if self.target_window:
            self._peek(self.target_window)
        try:
            tree, settle_result = self.settler.wait(
                lambda: self.provider.snapshot(self.target_window),  # type: ignore[arg-type]
                event_source=lambda: self._event_count,
                deterministic=self._settle_deterministic,
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
        # Keep the projection digest in step with the rev this call minted.
        # The digest is what decides whether a *later* observe/find bumps rev
        # again; leaving it at the pre-call value made the next read-only
        # operation report a change the call had already been counted for
        # (found live: CALL p1.add() returned rev=2, the next FIND returned
        # rev=3 without anything else having changed, and the second add was
        # expected at rev=3 but arrived at rev=4).
        self._last_signature = self._projection_signature(result)

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
                popup_text = self._mount_popup_content(action.ref)

        after_index = index_by_fingerprint(result.root)
        delta = diff_trees(before_index, after_index)

        verify = verifier.verify(resolution.effects, action.ref or "", delta)
        if verify.status == "EFFECT_NOT_CONFIRMED":
            raise SomnusError(
                ErrorCode.EFFECT_NOT_CONFIRMED,
                ref=action.ref,
                message="; ".join(verify.failed),
            )

        # Status honesty: `OK_UNVERIFIED` means "ran, could not confirm". When
        # the UI settled *and* the tree did not change at all, the action ran
        # against a UI that did not react - report plain OK with the (empty)
        # delta rather than an alarming UNVERIFIED, which makes agents retry
        # actions that already succeeded.
        #
        # `context_menu` is the one exception, and it exists because the
        # platform cannot answer "does this element have a menu":
        # `ShowContextMenu` is callable everywhere and silently does nothing
        # when the app registered none. An empty delta therefore does *not*
        # mean "ran, nothing changed" the way it does for activate - it means
        # the gesture may well have been a no-op, and flattening that to `OK`
        # told the agent it had opened a menu it never got (observed live: the
        # Notepad title-bar buttons answered `OK` and produced nothing).
        #
        # The decision is deferred to the end of this method: whether a menu
        # really appeared is only known once the in-window tree, the shell
        # popup hook and the window list have all been consulted.
        status = verify.status
        no_effect = settle_result.settled and delta.empty
        is_context_menu = any(s.primitive == "context_menu" for s in resolution.steps)
        body = self._window_delta()
        tree_body = self.delta_text(delta)
        if popup_text:
            # A popup menu is rendered as a subtree of the node that opened it,
            # so the agent reads one coherent structure either way - whether the
            # application draws its menu in-process or hands it to the shell.
            tree_body = f"{tree_body}\n{popup_text}" if tree_body else popup_text
        if tree_body:
            body = f"{body}\n{tree_body}" if body else tree_body
        # Echo the value a write actually put into the control. The delta shows
        # a `value` change only when it differs from what was there, and a
        # synthetic write (`paste_text`, `type_text`) may leave a value UIA
        # cannot read back - so without this line the agent cannot tell "wrote
        # the same text" from "wrote nothing", nor spot that a bad argument
        # became the literal text. This is data, so it is quoted, like every
        # other string the runtime reports.
        if written is not None:
            echo = f"~ {action.ref} text={_q(str(written))}"
            body = f"{body}\n{echo}" if body else echo
        # A context_menu on a tray icon may open its menu as a *separate
        # top-level window* (QQ's CEF tray menu), invisible to the in-window
        # delta. Compare the window list captured after the action against
        # the one before it: a new window whose process matches the target
        # means the gesture DID open something reachable via the windows list.
        menu_opened_as_window = False
        if any(s.primitive == "context_menu" for s in resolution.steps):
            before_keys = {w.window_id for w in self._windows_before_action} if self._windows_before_action else set()
            target_proc = self._target_process()
            for w in self.windows:
                if (
                    w.window_id not in before_keys
                    and w.window_id != self.target_window
                    and (
                        w.process == target_proc
                        # The owning process may be a shell host (explorer
                        # owns the taskbar while QQ hosts the menu window), so
                        # also accept a window whose *title* matches a window
                        # that vanished... none did: accept any new window
                        # when the target lives in a shell process.
                        or (target_proc in ("explorer.exe",)
                            and w.process not in ("explorer.exe", "ApplicationFrameHost.exe"))
                    )
                ):
                    menu_opened_as_window = True
                    break
        # A menu counts as "produced" only when it was not there before this
        # call. A window may already contain menus that have nothing to do with
        # it - File Explorer's title bar exposes its system menu (`MENU "系统"`)
        # permanently, and Notepad's tree carries a `MENU "File"` - so testing
        # for *any* menu node made every context_menu look successful on those
        # windows, which is the same lie the status check exists to remove.
        menu_appeared = any(
            n.role == "menu" and n.fingerprint not in before_index
            for n in result.root.walk()
        )
        if is_context_menu and not menu_appeared:
            # Android's long-press menu is not a `menu` node: MIUI renders the
            # popup as ordinary tappable items inside the launcher's own tree
            # (found live: long-pressing a launcher icon added 扫一扫/加好友
            # as TEXT+tap lines in the delta while the verdict claimed no
            # menu). Added interactive nodes right after the gesture ARE the
            # menu, so they count; a gesture that produced nothing adds
            # nothing and is still reported as such.
            menu_appeared = any(
                node.fingerprint not in before_index
                and (node.name or node.entity)
                and ({"tap", "activate", "select", "toggle"} & node.cap_names())
                for node in delta.added
            )
        # Did the gesture really open a menu? Any of the three routes counts:
        # the menu is in this window's tree, it was handed to the shell as a
        # popup, or it became a separate top-level window.
        menu_produced = bool(popup_text) or menu_appeared or menu_opened_as_window
        if status == "OK_UNVERIFIED":
            # For `context_menu` the outcome is decided by whether a menu
            # actually appeared, not by whether the delta is empty: a menu that
            # opened *is* the effect, and one that did not is not a success.
            # Every other action keeps the rule that a settled no-op UI is a
            # plain `OK`, which is what stops agents retrying work that already
            # succeeded.
            if not is_context_menu and no_effect:
                status = "OK"
            elif is_context_menu and menu_produced:
                status = "OK"

        if is_context_menu and not menu_produced:
            # The menu gesture ran but produced nothing anywhere: not in this
            # window's tree, not as a shell popup, not as a new window.
            direct = sorted(
                c
                for c in _node_caps(self.result, action.ref)
                if c in ("activate", "select", "toggle", "expand", "open")
            )
            hint = (
                f"; {action.ref} does expose {'/'.join(direct)} - "
                f"try CALL {action.ref}.{direct[0]}()"
                if direct
                else ""
            )
            # Say it plainly: a context_menu that produced no menu is not a
            # success. The element registered no menu, which the platform
            # cannot predict up front - so the agent is told here rather than
            # left believing a menu is open somewhere.
            body = (body + "\n" if body else "") + (
                "~ context_menu produced no menu (this element registers "
                f"none){hint}"
            )
        elif (
            action.capability in ("activate", "open", "select")
            and no_effect
            and not popup_text
        ):
            # activate ran, settled, and the window tree did not change. The
            # gesture may have opened a shell panel (tray overflow) that lives
            # outside the window - probe the popup hook once before giving up.
            popup_text = self._mount_popup_content(action.ref)
            if popup_text:
                tree_body = popup_text
                body = f"{body}\n{tree_body}" if body else tree_body
            elif "context_menu" in _node_caps(self.result, action.ref):
                # activate ran, settled, and changed nothing - on a row that
                # also exposes context_menu. This is the QQ conversation-row
                # shape: the Invoke only highlights and the real command lives
                # in the menu. Say it once, here, so the agent does not
                # conclude the row is inert and give up.
                body = (body + "\n" if body else "") + (
                    f"~ no effect; {action.ref} exposes context_menu - "
                    f'the command may be in the menu: CALL {action.ref}.context_menu()'
                )
        return CallOutcome(
            status=status,
            rev=self.rev,
            delta=delta,
            text=f"{status} rev={self.rev}"
            + ("" if settle_result.settled else " settled=false")
            + ("\n" + body if body else ""),
            settled=settle_result.settled,
        )

    def delta_text(self, delta: Delta) -> str:
        return delta.render()

    # ------------------------------------------------------------------ helpers

    def _resolve_with_hint(self, resolver: "Resolver", action: Action):
        """Resolve an action, turning a missing capability into a hint that
        names the nodes which do expose it (e.g. `add` lives on the product,
        not the cart), so the agent can retarget instead of looping - and
        what the target itself offers, so a wrong verb (``click`` vs
        ``activate`` vs the always-available ``tap``) is corrected in one
        round-trip instead of an INSPECT.
        """
        from somnus.errors import ErrorCode as _E

        try:
            return resolver.resolve(action)
        except SomnusError as err:
            if err.code == _E.CAPABILITY_NOT_FOUND and action.capability:
                node = self.result.find(action.ref or "") if self.result else None
                own = node.cap_names() if node else []
                if own:
                    err.message = (
                        f"no {action.capability} on {action.ref}; it has: "
                        f"{', '.join(own)} (tap always works via gesture)"
                    )
                holders = self._capability_holders(action.capability)
                if holders:
                    err.message += f"; {action.capability} on: {', '.join(holders)}"
            raise

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
                # Whether the element declined the interface, as opposed to
                # offering it and failing. The two look the same in `ok` and
                # mean very different things when reading a session back.
                "unsupported": exec_result.unsupported,
            }
        )

    # ---------------------------------------------------------- route memory

    def _remember_route(self, ref: str, primitive: str) -> None:
        """Record which interface actually worked for this element.

        Keyed by fingerprint, not ref: a ref is reallocated when the tree is
        recompiled, while the fingerprint names the element itself. The memory
        lasts as long as the element does in this session, so the next call on
        it skips the interfaces this session already saw it refuse.
        """
        node = self.result.find(ref) if self.result is not None else None
        if node is None or not node.fingerprint:
            return
        self._route_memory[node.fingerprint] = primitive

    def _preferred_route(self, ref: str) -> str | None:
        """The interface this element answered to last time, if known."""
        node = self.result.find(ref) if self.result is not None else None
        if node is None or not node.fingerprint:
            return None
        return self._route_memory.get(node.fingerprint)

    def _order_steps(self, ref: str, steps: list) -> list:
        """Put the remembered route first, leaving the rest as fallbacks.

        This is what makes repeated calls cheap on an element whose usable
        interface is not the one its patterns suggest. The order is only a
        preference: a remembered route that no longer applies simply fails and
        the chain continues, so the memory can never make a call fail that
        would otherwise have succeeded.
        """
        preferred = self._preferred_route(ref)
        if not preferred:
            return steps
        return sorted(steps, key=lambda s: 0 if s.primitive == preferred else 1)

    def audit_log(self) -> list[dict]:
        return list(self._audit)

    # ------------------------------------------------------------------ switching

    def switch_session(self, target: str, *, app: str | None = None) -> str:
        """Navigate the phone itself - screens, panels and apps.

        Targets:

        * ``home`` / ``back``     - platform global actions;
        * ``next`` / ``prev``     - launcher page turn (synthesized swipe);
        * ``recents``             - the task switcher;
        * ``notifications`` /
          ``quick_settings``      - the pull-down shades. On devices with one
          merged shade both open the same panel; on split-shade devices they
          are the two distinct panels, matching what the user's finger does
          from the left vs the top-right corner;
        * ``launch`` (with ``app=``) - open an app through the platform's own
          launch API. No tree, no icon clicking: what it reaches (a not-yet-
          instantiated app, a folder page away) is exactly what no tree can
          name;
        * ``open`` (with ``app=`` a scheme URI) - deep link: jump straight to
          an in-app page (``taobao://home``). The fast lane the vendor
          assistants ride - one intent replaces a whole navigation
          conversation. ERR APP_NOT_FOUND means no app handles the URI.

        Every switch discards every ref - the screen they named is gone.
        """
        target = (target or "").strip().lower()
        android_global = getattr(self.provider, "global_action", None)
        if android_global is None:
            return _err(
                "ACTION_NOT_SUPPORTED",
                "switch is an Android screen action; this provider has no "
                "global actions",
            )
        if target == "launch":
            if not app:
                return _err(
                    "INVALID_ARGUMENT",
                    "SWITCH launch needs an app: SWITCH launch <label-or-package>",
                )
            return self.app_action(Action(kind="app", query=app, scope="launch"))
        if target == "open":
            if not app:
                return _err(
                    "INVALID_ARGUMENT",
                    "SWITCH open needs a scheme URI: SWITCH open <uri>",
                )
            return self.app_action(Action(kind="app", query=app, scope="open"))
        if target in ("home", "back", "recents", "notifications", "quick_settings"):
            result = android_global(target)
        elif target in ("next", "prev"):
            pager = getattr(self.provider, "page", None)
            if pager is None:
                return _err(
                    "ACTION_NOT_SUPPORTED",
                    "this provider cannot turn launcher pages",
                )
            result = pager(target)
        else:
            return _err(
                "INVALID_ARGUMENT",
                f"unknown switch target {target!r}; use "
                "home|back|next|prev|recents|notifications|quick_settings|launch <app>|open <uri>",
            )
        if not result.ok:
            return SomnusError(
                result.error or ErrorCode.NATIVE_EXECUTION_FAILED,
                message=result.message or target,
            ).render()
        # The screen just changed wholesale: every ref named the old one.
        self.registry = SessionRegistry()
        self.result = None
        self._prev_windows = {}
        self._window_refs = {}
        self._window_refs_by_id = {}
        self._last_signature = None
        self._bump_rev()
        return (
            f"OK switched={target} rev={self.rev}\n"
            "~ refs discarded; OBSERVE to re-orient"
        )

    def close(self) -> None:
        """Release the provider."""
        self.provider.close()

    # ------------------------------------------------------------------ Android

    def system_action(self, op: str) -> str:
        """``SYSTEM back|home|recents|notifications`` (SomnusDroid doc §6.1).

        Routed to the provider's ``global_action``; a provider without one is
        not Android and the action is refused with the standard shape, so a
        model that mixes surfaces gets a precise error rather than a crash.
        """
        getter = getattr(self.provider, "global_action", None)
        if getter is None:
            return _err(
                "ACTION_NOT_SUPPORTED",
                "SYSTEM is an Android action; this provider has no global actions",
            )
        result = getter(op)
        if result.ok:
            # A global action moves the whole screen; refs may all be stale.
            self.registry = SessionRegistry()
            self.result = None
            self._bump_rev()
            return f"OK system={op} rev={self.rev}\n~ refs discarded; OBSERVE to re-orient"
        return SomnusError(
            result.error or ErrorCode.NATIVE_EXECUTION_FAILED,
            message=result.message or op,
        ).render()

    def app_action(self, action: Action) -> str:
        """``APP launch "<label or package>"`` / ``APP open <uri>`` /
        ``APP search "..."``.

        Launch goes through the platform's own API (PackageManager launch
        intent), never by clicking icons in a tree: what it reaches is
        exactly what no tree can name - an app whose launcher icon is pages
        deep in a folder, or not instantiated at all. ``open`` is the same
        lane one level deeper: a deep-link URI lands on an in-app page
        directly, skipping every navigation step (the fast lane the vendor
        assistants ride).
        """
        lister = getattr(self.provider, "list_apps", None)
        launcher = getattr(self.provider, "launch_app", None)
        if launcher is None:
            return _err(
                "ACTION_NOT_SUPPORTED",
                "APP is an Android action; this provider cannot launch apps",
            )
        query = action.query or ""
        if action.scope == "open":
            opener = getattr(self.provider, "open_uri", None)
            if opener is None:
                return _err(
                    "ACTION_NOT_SUPPORTED",
                    "APP open needs a provider with the deep-link channel",
                )
            result = opener(query)
            if result.ok:
                self.registry = SessionRegistry()
                self.result = None
                self._bump_rev()
                return f"OK app={query} rev={self.rev}\n~ refs discarded; OBSERVE"
            return SomnusError(
                result.error or ErrorCode.NATIVE_EXECUTION_FAILED,
                message=result.message or query,
            ).render()
        if action.scope == "search":
            apps = lister() if lister else []
            hits = [
                a for a in apps
                if query.casefold() in (a.get("label") or "").casefold()
                or query.casefold() in (a.get("package") or "").casefold()
            ][:10]
            if not hits:
                return "FOUND 0"
            lines = [f"FOUND {len(hits)}"]
            lines += [f"  APP {a.get('package')} {_q(a.get('label') or '')}" for a in hits]
            return "\n".join(lines)
        # launch: label first, package as fallback.
        apps = lister() if lister else []
        package = next(
            (a.get("package") for a in apps
             if (a.get("label") or "") == query or a.get("package") == query),
            query if "." in query else None,
        )
        if package is None:
            return SomnusError(
                ErrorCode.APP_NOT_FOUND, message=f'no app {query!r}'
            ).render()
        result = launcher(package)
        if result.ok:
            self.registry = SessionRegistry()
            self.result = None
            self._bump_rev()
            return f"OK app={package} rev={self.rev}\n~ refs discarded; OBSERVE"
        return SomnusError(
            result.error or ErrorCode.NATIVE_EXECUTION_FAILED,
            message=result.message or package,
        ).render()

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

        This is the transport for models without function calling: the same
        syntax and the same parser, just line-delimited instead of a tool call.
        Blank lines and ``#`` comments are ignored.
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
                # ``OBSERVE w5 full`` - the window's whole tree, no folding.
                # ``OBSERVE w5 expand g3`` - unfold one collapsed node this
                # round, everything else stays folded (tokens stay bounded
                # while still revealing exactly what the agent asked for).
                expanded: set[int] | None = None
                full = action.scope == "full"
                if action.depth is not None and action.depth:
                    expanded = set()
                    result = self._recompile() if self.result is None else self.result
                    node = result.find(action.query or "")
                    if node is not None:
                        expanded.add(id(node))
                return self.observe(
                    action.window,
                    windows_only=(action.query == "windows"),
                    include_other_windows=(action.scope == "desktop"),
                    full=full,
                    expanded=expanded,
                ).text
            if action.kind == "find":
                if getattr(action, "scope_scroll", False):
                    from somnus.runtime.scrollfind import scroll_find

                    return scroll_find(self, action)
                return self.find(action)
            if action.kind == "inspect":
                return self.inspect(action).text
            if action.kind == "call":
                return self.call(action, confirmed=confirmed).text
            if action.kind == "switch":
                return self.switch_session(action.query or "home", app=action.scope)
            if action.kind == "system":
                return self.system_action(action.query or "")
            if action.kind == "app":
                return self.app_action(action)
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


def result_root(session: "Session") -> SemanticNode:
    assert session.result is not None
    return session.result.root


def _node_caps(result: "CompileResult", ref: str) -> set[str]:
    node = result.find(ref) if result else None
    return node.cap_names() if node else set()


def _find_parent(root: SemanticNode, target: SemanticNode) -> SemanticNode | None:
    """The parent of ``target`` in ``root``'s tree, or None for the root."""
    stack = [root]
    while stack:
        node = stack.pop()
        for child in node.children:
            if child is target:
                return node
            stack.append(child)
    return None


def _ancestor_chain(root: SemanticNode, target: SemanticNode) -> list[SemanticNode]:
    """The ancestors of ``target``, window first, parent last.

    Find reports these so a hit is never an orphan: a BUTTON "确定" is only
    meaningful once the agent knows it sits in WINDOW "系统设置" > TEXT
    "是否删除". The chain runs window first, parent last, and carries refs so
    the next action can scope into an ancestor (FIND "…" in REF) without
    another inspect.
    """
    chain: list[SemanticNode] = []
    parent = _find_parent(root, target)
    while parent is not None:
        chain.append(parent)
        parent = _find_parent(root, parent)
    chain.reverse()
    return chain


def _breadcrumb_entry(node: SemanticNode) -> str:
    """One breadcrumb entry: named nodes carry their type and name; anonymous
    structural wrappers (the GROUP/Pane nesting every Android screen is built
    from) carry only the ref - they exist to be scoped into, not read.

    An interactive ancestor also carries its activation cap: a plain text
    with no claim of its own still answers a tap when a clickable ancestor
    consumes the touch (Android bubbles unhandled touches up to the
    clickable container). The annotation is one-directional: a cap is
    platform-proven, so its presence guarantees the tap lands; its absence
    only means the tree has no evidence - a touch listener sets no
    clickable flag and stays invisible (found live: MIUI folder labels,
    chain shows no cap, tap still opens the folder).
    """
    if node.name:
        base = f"{node.display_type} {node.ref} {_q(node.name)}"
    else:
        base = node.ref
    for cap in ("activate", "tap", "open"):
        if cap in node.cap_names():
            return f"{base} +{cap}"
    return base


def _node_matches(node: SemanticNode, query: str) -> bool:
    """Whether ``query`` names ``node``.

    The searchable text is the node's *name* and *value* plus its **textual**
    attributes (an entity type, a unit). Numeric attributes are deliberately
    excluded from token matching, and matching is on word boundaries rather
    than substrings, because the loose version made containers shadow their own
    rows: a catalog whose parent carries ``entity=item count=240`` matched
    ``FIND "Item 40"`` on the ``40`` inside ``240`` and returned the list
    instead of item 40 (found while building the large-catalog scenario). A
    container is not a match for its children's names - that is what scoping
    (``FIND "..." in l1``) is for.
    """
    if not query:
        return True
    q = query.casefold().strip()
    # Exact substring match on the node's own name/value: the strongest signal,
    # and the one that makes "Big Mac" find "Big Mac Meal".
    haystacks = [node.name or "", str(node.value or "")]
    if any(q in h.casefold() for h in haystacks):
        return True
    # Attribute text (entity type, currency) may also be named, but only when
    # it is a string - a number is an amount, not a name.
    attr_text = [
        str(value)
        for value in node.attrs.values()
        if isinstance(value, str) and value
    ]
    if any(q in value.casefold() for value in attr_text):
        return True
    # Token subset match ("big mac" matches "Big Mac Meal"): every query token
    # must appear as a whole word, so "40" does not match inside "240".
    tokens = [t for t in q.split() if t]
    if not tokens:
        return False
    words = set()
    for source in (*haystacks, *attr_text):
        words.update(_word_tokens(source))
    return all(t in words for t in tokens)


_WORD_RE = re.compile(r"[0-9a-z\u4e00-\u9fff]+")


def _word_tokens(text: str) -> list[str]:
    """Lower-cased word tokens, keeping CJK runs as one token each.

    Splitting only on non-alphanumerics means ``Item 40`` yields ``item`` and
    ``40`` (so a token match cannot land inside ``240``), while ``巨无霸``
    stays one token because CJK has no spaces.
    """
    return _WORD_RE.findall(text.casefold())


def _render_subtree(node: SemanticNode, lines: list[str], opts: RenderOptions, depth: int, indent: int) -> None:
    if depth <= 0:
        if node.children:
            lines.append("  " * indent + f"… +{len(node.children)} more")
        return
    # Same-kind preview (as in the observe projection): a long homogeneous run
    # shows its first children and folds the rest, so a 300-row list costs a
    # handful of lines even at L2.
    children = node.children
    preview_limit = len(children)
    if len(children) > 8 and len({c.role for c in children}) <= 2:
        preview_limit = 3
    for i, child in enumerate(children):
        if i == preview_limit:
            hidden = len(children) - preview_limit
            lines.append("  " * indent + f"… +{hidden} more")
            return
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


def _err(code: str, message: str, *, hint: str = "report-user") -> str:
    """One failure line in the same shape the rest of the runtime emits."""
    return f'ERR {code} hint={hint} msg="{message}"'


def _q(text: str | None) -> str:
    if text is None:
        return '""'
    return '"' + text.replace('"', '\\"') + '"'


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
