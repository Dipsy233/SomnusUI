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
import math
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from somnus.compiler.compile import CompileResult, Compiler
from somnus.compiler.pack_schema import Pack, select_pack
from somnus.config import DEFAULT_ACTIONABLE_TIMEOUT_S
from somnus.errors import ErrorCode, SomnusError, target_not_found
from somnus.ir.action import Action
from somnus.ir.node import RawTree, Relation, SemanticNode, WindowInfo
from somnus.observability.trace import NullTraceSink, TraceSink
from somnus.projection.budget import ObservationBudget, count_tokens
from somnus.projection.engine import ProjectionEngine, display_node
from somnus.projection.rules import apply_projection
from somnus.providers.base import AccessibilityProvider
from somnus.runtime.delta import Delta, diff_trees, index_by_fingerprint
from somnus.runtime.policy import Policy
from somnus.runtime import locator as locators
from somnus.runtime import preflight
from somnus.runtime.registry import SessionRegistry
from somnus.runtime.resolver import Resolver
from somnus.runtime.settle import Settler, signature_of
from somnus.runtime.verifier import Verifier
from somnus.sui.serialize import (
    INDENT,
    RenderOptions,
    escape_text,
    render_document,
    render_node_line,
)
from somnus.sui.parse import parse_action

log = logging.getLogger("somnus.session")

#: Set SOMNUS_TRACE_STATES=1 to trace a call end to end: which route a
#: capability resolved to, what each primitive did, and how the target's states
#: looked before and after the settle.
_TRACE_STATES = os.environ.get("SOMNUS_TRACE_STATES", "") not in ("", "0", "false")

#: Set SOMNUS_PROFILE=1 to collect per-call segment timings in
#: :attr:`Session.timing` (``enum``/``snapshot``/``probe``/``inject``/
#: ``settle``/``compile``, in milliseconds). The runner reads these through
#: ``bench/run.py``; they are off by default so the hot path pays nothing.
_PROFILE = os.environ.get("SOMNUS_PROFILE", "") not in ("", "0", "false")

#: Set SOMNUS_VIEWPORT=0 to turn the viewport capture off (A/B measurement and
#: an escape hatch). On by default: on a window whose list holds hundreds of
#: rows it turns a whole-tree observe into a structural read plus the rows that
#: are actually on screen (measured: 3.07s -> ~0.36s on the store screen).
_VIEWPORT_ENABLED = os.environ.get("SOMNUS_VIEWPORT", "") not in ("0", "false", "no")


@contextmanager
def _timed(sink: dict[str, float] | None, name: str):
    """Accumulate one segment into ``sink``; a no-op when profiling is off."""
    if sink is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        sink[name] = sink.get(name, 0.0) + (time.perf_counter() - started) * 1000


#: The keys ``apply_settings`` accepts, with their canonical names, ranges and
#: type. Validation is all-or-nothing: one bad value means none is applied.
_SETTINGS_SCHEMA: dict[str, tuple] = {
    "settle_quiet_ms": (int, 10, 1000),
    "settle_poll_ms": (int, 1, 100),
    "settle_timeout_s": (float, 0.5, 30.0),
    "post_action_enum": (bool,),
}

#: Primitives that prepare the element for the step after them. They are only
#: preparatory when another step follows: `focus` then `type_text` means "focus
#: it, then type", but a capability whose *last* step is `focus` (or
#: `scroll_into_view`) really is doing that thing. Treating a successful
#: preparation as completion reported success for an action that never ran.
_PREPARATORY_PRIMITIVES = frozenset({"focus", "scroll_into_view"})

#: Primitives that change what is on screen (scroll position or expansion).
#: After one of these the watched set no longer describes the viewport, so the
#: next probe falls back to full reads and the response says to re-observe
#: rather than handing back rows that are no longer where it claims.
_VIEWPORT_MOVING_PRIMITIVES = frozenset(
    {"scroll", "scroll_into_view", "expand", "collapse"}
)

#: Minimum spacing between FIND's waiting re-reads, in milliseconds. A viewport
#: read is its own throttle on a real provider (measured ~50-410ms across the
#: suite's windows), so the floor only exists so a replay provider - whose read
#: is ~0ms - cannot spin a core for the whole timeout.
_FIND_POLL_FLOOR_MS = 20

#: Minimum spacing between the preflight wait's signature probes. Same reason
#: as the find floor: on a real provider the probe itself (~hundreds of
#: milliseconds on a large window) is the throttle, but a replay/fake provider
#: could otherwise spin for the whole (small) bound.
_PREFLIGHT_POLL_FLOOR_MS = 20

#: A wait first polls the viewport capture. When that capture is a partial tree
#: (a popup/pane is not in the viewport's layer) and still fits a full read, the
#: wait may spend one full read before the deadline. These gates keep that from
#: turning into a second, unbounded read strategy:
#:
#: * the capture must be *untruncated* - a long list's full read is the 3-4s
#:   case this deliberately refuses;
#: * the captured tree must be small enough that a full read is plausibly cheap;
#: * a few polls must already have missed (the element had a chance to appear);
#: * the read may start only in the first half of the deadline, and its
#:   conservative cost estimate must fit in the time that is left.
_FIND_ESCALATE_AFTER_POLLS = 3
_FIND_ESCALATE_MAX_NODES = 200
_FIND_ESCALATE_MS_PER_NODE = 3.0
_FIND_ESCALATE_START_FRACTION = 0.40
_FIND_ESCALATE_DEADLINE_FRACTION = 0.50

#: A complete capture (nothing truncated) up to this many nodes is read with
#: one full subtree request instead of the viewport walker. The walker pays a
#: cache round trip per node, so it only earns that back when it can *cut*
#: rows (a truncated collection); an untruncated tree is identical either way.
#: Measured p50 on the acceptance screens: form 30 nodes full 78ms vs viewport
#: 127ms, chat 58 nodes 140 vs 192, menu 18 55 vs 76, tree 16 36 vs 62. The cap
#: only guards a pathologically large untruncated window; both paths grow with
#: the node count and full keeps a single request.
_SMALL_WINDOW_NODES = 500

#: Observation shaping for the same small-window shape. L0's default budget
#: (depth 3, 40 nodes, 300 tokens, auto-collapse at 8 homogeneous children)
#: renders a UWP app as "title bar + two anonymous groups": the calculator's
#: digit pad lives at depth 5 and its ten buttons collapse as one group. A
#: window whose *partial* capture was untruncated and this small gets enough
#: depth/nodes/tokens to show its controls, still bounded so a single
#: observation stays cheap. Larger or truncated windows keep the L0 budget.
_SMALL_OBSERVE_NODES = 120
_SMALL_OBSERVE_MAX_DEPTH = 10
_SMALL_OBSERVE_MAX_NODES = 80
_SMALL_OBSERVE_MAX_TOKENS = 700
_SMALL_OBSERVE_AUTO_COLLAPSE = 1000

#: Default wait for a script's ``AWAIT`` when the step gives no ``timeout=``.
#: Menus and dialogs in the suite settle well inside this; the cap keeps a
#: missing element from spending the whole call on one wait.
DEFAULT_AWAIT_TIMEOUT_S = 2.0

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
class _ChainResult:
    """Outcome of walking one resolution's route chain.

    ``written`` is the text a write primitive put into the control (echoed in
    the response); ``undetermined`` means the call may still be in flight - a
    modal dialog opening - and nothing after it can be judged yet.
    """

    exec_result: Any
    route: str = ""
    written: Any = None
    undetermined: bool = False
    #: Routes this chain never reached, in the order it would have walked them.
    #: ``_retry_unconfirmed`` runs exactly these when the settled tree did not
    #: show the declared effect (one retry, never a loop).
    remaining: list = field(default_factory=list)


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
        #: How to rebuild a *local* provider when switching back from the child
        #: session. The provider instance currently in use must be released
        #: during the swap (only one may hold UIA state), so switching away
        #: cannot simply keep this one around - it has to be reconstructible.
        #: Set by the caller that built the session (see somnus/cli.py); when
        #: absent, switching back to "main" is reported as unsupported rather
        #: than done badly.
        self._local_provider_factory = None
        #: Which desktop the current refs name: "main" or "child".
        self._session_label = "main"
        #: The child session this process opened, once it has one. Kept so the
        #: exit hook can clean up what was started here and nothing else.
        self._child_session_id: int | None = None
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
        #: Whether a call re-enumerates windows after its action. On by default:
        #: an action that opens a window (a menu, a modal, a second app window)
        #: must be reportable in the same response. Turning it off saves one
        #: enumeration per call (~85-121ms measured) and hides exactly that -
        #: the new window still shows up on the next ``observe``.
        self.post_action_enum: bool = True
        #: How long one action may wait for a blocked element (a disabled
        #: control) to become actionable. Paid only on the blocked path; the
        #: happy path never probes. 0 disables the wait. A host assigns the
        #: configured value (somnus/config.py).
        self.actionable_timeout_s: float = DEFAULT_ACTIONABLE_TIMEOUT_S
        #: The settings this session started with, so ``reset`` restores them
        #: (file + environment as resolved by the config layer, not the
        #: built-in defaults). Captured lazily on the first ``apply_settings``:
        #: a host builds the session and *then* assigns ``post_action_enum``
        #: from the config file (see ``cli.build_session``), so a snapshot taken
        #: in ``__init__`` would restore the built-in default instead of the
        #: configured value.
        self._startup_settings: dict | None = None
        #: Per-call segment timings (see module attribute ``_PROFILE``). ``None``
        #: unless SOMNUS_PROFILE is on.
        self.timing: dict[str, float] | None = {} if _PROFILE else None
        #: Decision-trace sink (see :mod:`somnus.observability`). Off by default:
        #: every hook checks :attr:`enabled` before doing any work, so a normal
        #: run pays one attribute lookup per hook and nothing else.
        self.trace_sink: TraceSink = NullTraceSink()
        #: Session identity for trace call ids, created on first use.
        self._trace_session_id: str | None = None
        self._trace_call_seq = 0
        #: Call ids of the calls currently executing (a ``run`` script nests its
        #: steps inside the script's own call). Trace events inherit the top id.
        self._trace_call_stack: list[str] = []
        #: call id -> {"before": snapshot ref, "after": snapshot ref}.
        self._trace_snapshots: dict[str, dict[str, str]] = {}
        #: The exact text the last OBSERVE returned, so a call's before-snapshot
        #: is what the model actually saw rather than a re-render.
        self._last_observation_text: str = ""
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
        #: The raw tree the current compilation was built from, and the window
        #: it belongs to. Settle hands it back untouched when a signature probe
        #: says nothing changed, which is what removes the second full
        #: snapshot from a call (see ``Settler.wait``). The window id is kept
        #: beside it: after a switch the tree describes the old desktop and
        #: must not be reused.
        self._last_raw: RawTree | None = None
        self._last_raw_window: str | None = None
        #: The signature of that tree *as the provider returned it*, i.e. before
        #: compilation normalised its nodes in place (``edit`` -> ``input``).
        #: Settle compares the probe against this, not against the tree, or every
        #: probe would look different from the stored frame.
        self._last_raw_sig: str | None = None
        #: As-read shallow signature per native, for the watched-set comparison
        #: (compilation rewrites roles in place, so the tree itself is no longer
        #: in the form a fresh probe returns).
        self._last_raw_map: dict = {}
        #: True when `self.result` came from the viewport capture (structure +
        #: on-screen rows only). `find`/`inspect` take one full read before
        #: giving up, instead of reporting "not found" for a ref that merely
        #: was not captured.
        self._viewport_limited: bool = False
        #: One-line note when a peer fell back to a full snapshot because it has
        #: no viewport capture. Rendered under the observation header once.
        self._viewport_note: str = ""
        #: Identity keys (runtime id, else ids+path) of the nodes the last
        #: viewport capture actually read. Settle compares only these plus the
        #: call's own target and its ancestors; an empty list means "no captured
        #: layers", which disables layered probing (full snapshots instead).
        self._watch_natives: list = []
        #: Size of the last capture and whether it cut anything. Drives the
        #: small-window adaptation: an untruncated capture is read in full next
        #: time, and settle then reads full frames instead of walking a small
        #: tree node by node for a probe.
        self._last_capture_nodes: int | None = None
        self._last_capture_truncated: bool = False
        #: The window whose viewport capture was small and untruncated. Its
        #: observation uses the adaptive budget below, even after a later read
        #: switched to the full snapshot (the flag is about the window's size,
        #: not about which read path produced this frame).
        self._small_observe_window: str | None = None
        #: Which window that capture described. A small window's size says
        #: nothing about another window, so a switch must forget it - otherwise
        #: the first observation of a long-list window would be read in full.
        self._last_capture_window: str | None = None
        #: session ref -> native window_id, and reverse, for CALL w?.focus().
        self._window_refs: dict[str, str] = {}
        self._window_refs_by_id: dict[str, str] = {}
        #: The desktop's cheap Win32 identity at the time of the last
        #: enumeration (``provider.top_level_signature()``). ``None`` means
        #: "unknown" - the provider offers no cheap signature, it failed to
        #: read, or nothing has been enumerated yet - and always means "enumerate".
        self._top_level_sig: object | None = None
        #: Set by a successful desktop switch. Ref names are bare strings, so a
        #: fresh compilation on the new desktop could mint ``q1``/``w3`` again;
        #: until an OBSERVE has printed the new namespace, any ref carried over
        #: from the old desktop must be refused rather than resolve to a new
        #: element with the same name.
        self._refs_need_observe = False

    # -------------------------------------------------------------------- trace

    def _trace_event(self, event: str, **fields: Any) -> None:
        """Emit one trace record; a no-op when tracing is off.

        Every field must be JSON-safe for the JSONL sink; a sink still falls
        back to ``str`` rather than losing the record.
        """
        if not self.trace_sink.enabled:
            return
        call_id = self._trace_call_stack[-1] if self._trace_call_stack else None
        if call_id is not None and "call_id" not in fields:
            fields["call_id"] = call_id
        try:
            self.trace_sink.emit(event, **fields)
        except Exception:  # noqa: BLE001 - evidence must never break a call
            log.debug("trace emit failed", exc_info=True)

    def _trace_new_call_id(self) -> str:
        if self._trace_session_id is None:
            self._trace_session_id = uuid.uuid4().hex[:12]
        self._trace_call_seq += 1
        return f"{self._trace_session_id}:{self._trace_call_seq}"

    def _trace_snapshot(self, text: str | None, call_id: str, slot: str) -> str | None:
        """Store one SUI-Text snapshot and remember its reference for this call."""
        if not text:
            return None
        try:
            ref = self.trace_sink.snapshot(text)
        except Exception:  # noqa: BLE001
            log.debug("trace snapshot failed", exc_info=True)
            return None
        if ref:
            self._trace_snapshots.setdefault(call_id, {})[slot] = ref
        return ref

    def _trace_sui_text(self, result: CompileResult) -> str:
        """Render the current tree as SUI-Text, once, for a trace snapshot."""
        opts = RenderOptions(include_default_caps=False)
        lines = [render_node_line(display_node(result.root), opts)]
        _render_subtree(result.root, lines, opts, 1000, 1)
        return "\n".join([f"@rev {self.rev}", *lines])

    def _trace_observe(self, text: str, tokens: int, *, truncated: bool) -> None:
        self._last_observation_text = text
        self._trace_event(
            "observe",
            rev=self.rev,
            window=self.target_window,
            tokens=tokens,
            truncated=truncated,
        )

    # ------------------------------------------------------------------ windows

    def refresh_windows(self) -> list[WindowInfo]:
        with _timed(self.timing, "enum"):
            listed = self.provider.list_windows()
        self._adopt_windows(listed)
        return self.windows

    def _adopt_windows(self, listed: list[WindowInfo]) -> None:
        """Take one enumeration as the desktop, one ref per native window."""
        trace_before = (
            [(w.window_id, w.title, w.active) for w in self.windows]
            if self.trace_sink.enabled
            else None
        )
        # One native top-level window gets one ref. A UWP host can report the
        # same HWND twice (frame + content); keeping both created two window
        # refs for one element, and only one of them resolved in the tree.
        unique: list[WindowInfo] = []
        seen: dict[int, int] = {}
        for window in listed:
            if not window.hwnd:
                unique.append(window)
                continue
            if window.hwnd in seen:
                index = seen[window.hwnd]
                if window.window_id == self.target_window:
                    unique[index] = window
                continue
            seen[window.hwnd] = len(unique)
            unique.append(window)
        self.windows = unique
        # Record what the desktop looked like *when this list was taken*, so a
        # later post-action comparison can tell "nothing appeared/closed/renamed"
        # without paying for another UIA walk. An unknown signature (no provider
        # support, or a read failure) disables the shortcut, never the refresh.
        self._top_level_sig = self._read_top_level_signature()
        if not self.windows:
            # The previous target may have just been closed (the polite-close
            # path closes every Notepad window). Keeping its id would make the
            # next observation snapshot a window that no longer exists.
            self.target_window = None
        elif self.target_window is None or not any(
            w.window_id == self.target_window for w in self.windows
        ):
            self.target_window = self._default_window()
        if trace_before is not None:
            trace_after = [(w.window_id, w.title, w.active) for w in self.windows]
            if trace_after != trace_before:
                self._trace_event(
                    "window.change", before=trace_before, after=trace_after
                )

    def _read_top_level_signature(self) -> object | None:
        """The provider's cheap desktop signature, or None when unavailable."""
        signature = getattr(self.provider, "top_level_signature", None)
        if signature is None:
            return None
        try:
            # Charged to ``enum`` so a profile run shows the *actual* desktop
            # check: ~0.3ms when the shortcut fires, enumeration + check when
            # it does not.
            with _timed(self.timing, "enum"):
                return signature()
        except Exception:  # noqa: BLE001 - a hint must never break the call
            log.debug("top-level signature unavailable; full enumeration", exc_info=True)
            return None

    def _desktop_changed(self) -> bool | None:
        """Whether the desktop differs from the last enumeration.

        ``True``/``False`` come from the cheap signature; ``None`` means the
        question could not be answered (no support / read failure / no baseline)
        and the caller must enumerate. ``False`` is safe to trust because the
        signature covers every visible titled top-level window: a new window,
        a closed window, or a renamed one changes it.
        """
        current = self._read_top_level_signature()
        if current is None or self._top_level_sig is None:
            return None
        return current != self._top_level_sig

    def _refresh_after_action(self) -> None:
        """Re-enumerate after an action only if the desktop could have changed.

        Called exactly where ``refresh_windows()`` used to be called in the
        action paths. When the signature is unchanged the existing list *is*
        the current desktop (nothing appeared, closed or was renamed), so
        keeping it - and keeping ``_prev_windows`` as the baseline it already
        is - reports the same empty window delta a refresh would have reported,
        for ~1-3ms instead of an 80-120ms UIA walk. Anything unknown or
        different falls back to the full refresh, preserving the old semantics.
        """
        if self._desktop_changed() is not False:
            self.refresh_windows()
        elif _TRACE_STATES:
            log.info("post-action enumeration skipped: desktop signature unchanged")

    # NOTE: called *after* settle in the action paths. A title change can land
    # milliseconds after the action returns; checking before settle would miss
    # it (and merge two later renames into one delta).

    # ----------------------------------------------------------------- settings

    def _current_settings(self) -> dict:
        """The values in force right now, under their canonical keys."""
        return {
            "settle_quiet_ms": int(self.settler.quiet_ms),
            "settle_poll_ms": int(self.settler.poll_interval_ms),
            "settle_timeout_s": float(self.settler.timeout_s),
            "post_action_enum": bool(self.post_action_enum),
        }

    def apply_settings(
        self,
        *,
        quiet_ms: int | None = None,
        poll_ms: int | None = None,
        timeout_s: float | None = None,
        post_action_enum: bool | None = None,
        persist: bool = False,
        reset: bool = False,
    ) -> dict:
        """Change this session's pacing settings, atomically.

        Only the keys passed are changed; ``reset=True`` puts everything back to
        the values this session started with (the config layer's file + env
        result), and explicit keys then override that baseline in the same call.

        Validation is **all or nothing**: one bad value means nothing is applied
        and the error names the key, its expected range and the value received.
        Strings are rejected rather than parsed - ``"40"`` silently widening or
        narrowing the quiet window is exactly the class of surprise this
        prevents.

        The change applies to the *next* ``call``/``observe``/``find``/
        ``call_batch``; ``Settler.wait`` reads the fields when it starts, so an
        in-flight call keeps the values it began with.

        ``persist=True`` writes the effective values through the config layer's
        save entry (``somnus.config.persist_session_settings``); that entry is
        owned by the config work, and until it exists this raises
        ``NotImplementedError`` **without changing anything**.
        """
        errors: list[str] = []
        updates: dict = {}

        provided = {
            "settle_quiet_ms": quiet_ms,
            "settle_poll_ms": poll_ms,
            "settle_timeout_s": timeout_s,
            "post_action_enum": post_action_enum,
        }
        for key, value in provided.items():
            if value is None:
                continue
            kind, *bounds = _SETTINGS_SCHEMA[key]
            if kind is bool:
                if isinstance(value, bool):
                    updates[key] = value
                else:
                    errors.append(f"key={key} expected bool got={value!r}")
                continue
            # ``bool`` is an ``int`` in Python; True must not become quiet_ms=1.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                if kind is int:
                    errors.append(
                        f"key={key} expected int in {bounds[0]}..{bounds[1]} got={value!r}"
                    )
                else:
                    errors.append(
                        f"key={key} expected number in {bounds[0]}..{bounds[1]} got={value!r}"
                    )
                continue
            if kind is int and not isinstance(value, int):
                errors.append(
                    f"key={key} expected int in {bounds[0]}..{bounds[1]} got={value!r}"
                )
                continue
            if not bounds[0] <= float(value) <= bounds[1]:
                label = "int" if kind is int else "number"
                errors.append(
                    f"key={key} expected {label} in {bounds[0]}..{bounds[1]} got={value!r}"
                )
                continue
            updates[key] = int(value) if kind is int else float(value)

        if not isinstance(persist, bool):
            errors.append(f"key=persist expected bool got={persist!r}")
        if not isinstance(reset, bool):
            errors.append(f"key=reset expected bool got={reset!r}")

        if errors:
            raise SomnusError(
                ErrorCode.INVALID_ARGUMENT,
                message="; ".join(errors) + "; nothing changed",
            )

        if self._startup_settings is None:
            # First settings call: this is the baseline ``reset`` returns to.
            self._startup_settings = self._current_settings()
        effective = (
            dict(self._startup_settings)
            if reset
            else self._current_settings()
        )
        effective.update(updates)

        persisted = None
        if persist:
            # Write first: a failed save must leave the session as it was,
            # otherwise the process would run values the file does not contain.
            persisted = self._persist_settings(effective)

        self.settler.quiet_ms = effective["settle_quiet_ms"]
        self.settler.poll_interval_ms = effective["settle_poll_ms"]
        self.settler.timeout_s = effective["settle_timeout_s"]
        self.post_action_enum = effective["post_action_enum"]

        out = dict(effective)
        out["scope"] = "session"
        if persisted is not None:
            out["persisted"] = persisted
        return out

    def _persist_settings(self, values: dict) -> str:
        """Hand the effective values to the config layer's save entry."""
        from somnus import config as config_module

        hook = getattr(config_module, "persist_session_settings", None)
        if hook is None:
            raise NotImplementedError(
                "persist=true needs the config layer's save entry: "
                "somnus.config.persist_session_settings(**values) -> path; "
                "nothing changed"
            )
        return str(hook(**values))

    def _compile_tree(
        self, tree: RawTree, pack: Pack | None, *, window_ref: str | None = None
    ) -> CompileResult:
        if not self.trace_sink.enabled:
            with _timed(self.timing, "compile"):
                return self.compiler.compile(tree, pack=pack, window_ref=window_ref)
        started = time.perf_counter()
        with _timed(self.timing, "compile"):
            result = self.compiler.compile(tree, pack=pack, window_ref=window_ref)
        self._trace_event(
            "compile",
            nodes=sum(1 for _ in result.root.walk()),
            rev=self.rev,
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
            truncated=any(node.truncated for node in tree.walk()),
        )
        return result

    def _remember_raw(self, tree: RawTree) -> None:
        """Store the frame settle may reuse, *before* it is compiled.

        The signature must be taken here: :mod:`somnus.compiler.normalize`
        rewrites raw nodes in place, so after compilation the tree no longer
        answers with the signature a fresh read would produce. When ``tree`` is
        the frame we already hold (settle found nothing changed), the signature
        stays as it was.
        """
        if tree is self._last_raw and self._last_raw_sig is not None:
            self._last_raw_window = self.target_window
            return
        self._last_raw = tree
        self._last_raw_window = self.target_window
        self._last_raw_sig = signature_of(tree)
        #: Per-native shallow signatures of the frame *as read*, captured
        #: before compilation normalises roles in place (``edit`` -> ``input``).
        #: The watched-set comparison needs the as-read form or every probe
        #: would look different from the stored frame.
        self._last_raw_map = {
            _native_key(node.native): _shallow_signature(node) for node in tree.walk()
        }

    def _signature_probe(self) -> Callable[[], RawTree] | None:
        """The cheap probe for settle, when the provider offers one.

        The provider's *layered* probe (structure + visible rows in the signature
        profile) is what makes a big window cheap; the older whole-subtree
        signature read remains for a provider that only implements that. The
        caller decides whether to use it at all: on a window that fits whole,
        the probe costs about a full read (measured: chat sig probe 158ms vs
        full 163ms), and "probe, read, probe" is then three reads where the
        full-read loop takes two - see ``_settle``.
        """
        window = self.target_window
        if window is None:
            return None
        layered = getattr(self.provider, "snapshot_probe", None)
        if layered is not None:

            def probe_layered() -> RawTree:
                with _timed(self.timing, "probe"):
                    return layered(window)  # type: ignore[arg-type]

            return probe_layered
        snapshot_signature = getattr(self.provider, "snapshot_signature", None)
        if snapshot_signature is None:
            return None

        def probe() -> RawTree:
            with _timed(self.timing, "probe"):
                return snapshot_signature(window)  # type: ignore[arg-type]

        return probe

    def _watch_for_call(self, refs: list[str]) -> list:
        """The nodes settle may compare: captured layers + touched refs/ancestors.

        Returns ``[]`` when there is no captured layer (no viewport observe has
        run), which the caller turns into "no probe, full snapshots" - probing
        without a known visible set would be a guess, not evidence.

        The same ``[]`` is returned when a touched ref cannot be placed in the
        set at all (it maps no native element, or is not in the current
        compilation). That is the hard rule: a probe that cannot see the
        element this call acted on is not evidence of "unchanged", so the wait
        must pay for a full snapshot instead.
        """
        if not self._watch_natives or self.result is None:
            return []
        watch = list(self._watch_natives)
        seen = set(watch)
        for ref in refs:
            if not ref:
                continue
            keys = self._ancestor_keys(ref)
            if not keys:
                if _TRACE_STATES:
                    log.info(
                        "settle probe disabled: touched ref %s has no native "
                        "identity; using full snapshots",
                        ref,
                    )
                return []
            for key in keys:
                if key not in seen:
                    seen.add(key)
                    watch.append(key)
        return watch

    def _ancestor_keys(self, ref: str) -> list:
        """The ref's own identity key plus every ancestor's, from the current result."""
        if self.result is None:
            return []
        chain = _ref_path(self.result.root, ref)
        if not chain:
            return []
        out = []
        for node in chain:
            source = self.result.sources.get(node.ref)
            if source is not None:
                out.append(_native_key(source.native))
        return out

    def _previous_watch_signature(self, watch: list) -> str | None:
        """The watched-set signature of the pre-action frame, as it was read."""
        if (
            not watch
            or self._last_raw_window != self.target_window
            or not self._last_raw_map
        ):
            return None
        parts = []
        for native in watch:
            signature = self._last_raw_map.get(native)
            if signature is None:
                return None
            parts.append(signature)
        return "\x1f".join(parts)

    def _watch_signature(self, tree: RawTree, watch: list) -> str | None:
        return _watch_signature(tree, watch)

    def _settle(
        self, *, fetch: Callable[[], RawTree], refs: list[str]
    ) -> tuple[RawTree, Any]:
        """Run settle over the watched set, falling back to full reads.

        The hard rule lives here: if the pre-action frame cannot evaluate the
        watched set (a watched node is absent, or no viewport capture ever
        established the layers), the probe is dropped and the settler uses full
        snapshots. It never treats "not captured" as "did not change".
        """
        has_layered = getattr(self.provider, "snapshot_probe", None) is not None
        if (
            self._capture_prefers_full()
            and has_layered
            and not self._settle_deterministic
        ):
            # The window fits whole and the provider's layered probe walks it
            # node by node, so a probe buys nothing: it costs about the same as
            # the one-request full read, and "probe, snapshot, confirm" is then
            # three reads where the full loop takes two. Correctness is
            # unchanged - a full frame is the strictest evidence there is.
            # Providers whose only probe is the whole-tree signature keep it
            # (that is the documented one-probe/no-snapshot path), as does a
            # deterministic provider, whose probe is just a copy.
            probe = None
            layered = False
            if _TRACE_STATES:
                log.info("settle probe skipped: window fits whole; full reads")
        else:
            probe = self._signature_probe()
            layered = has_layered
            if probe is None and _TRACE_STATES:
                log.info("settle probe disabled: provider offers no probe; full snapshots")
        if not layered:
            # Legacy path: a provider whose probe reads the *whole* tree's
            # signature can be compared directly (no watched-set projection).
            previous = self._settle_previous() if probe else None
            return self.settler.wait(
                fetch,
                event_source=lambda: self._event_count,
                deterministic=self._settle_deterministic,
                previous=previous[0] if previous else None,
                previous_signature=previous[1] if previous else None,
                probe=probe,
            )
        watch = self._watch_for_call(refs) if probe else []
        previous = self._settle_previous() if (probe and watch) else None
        previous_signature = (
            self._previous_watch_signature(watch) if previous else None
        )
        signature = None
        if previous_signature is None:
            if probe is not None and _TRACE_STATES:
                reason = "no captured layer" if not watch else "watched set not covered"
                log.info("settle probe disabled: %s; using full snapshots", reason)
            probe = None
            previous = None
        else:
            signature = lambda tree: self._watch_signature(tree, watch)  # noqa: E731
        return self.settler.wait(
            fetch,
            event_source=lambda: self._event_count,
            deterministic=self._settle_deterministic,
            previous=previous[0] if previous else None,
            previous_signature=previous_signature,
            probe=probe,
            signature=signature,
        )

    def _settle_previous(self) -> tuple[RawTree, str] | None:
        """The pre-action frame and its as-read signature, for the current window."""
        if (
            self._last_raw is None
            or self._last_raw_sig is None
            or self._last_raw_window != self.target_window
        ):
            return None
        return self._last_raw, self._last_raw_sig

    def _fetch_snapshot(self) -> RawTree:
        with _timed(self.timing, "snapshot"):
            return self.provider.snapshot(self.target_window)  # type: ignore[arg-type]

    def _note_capture(self, tree: RawTree, partial: bool) -> None:
        """Remember how big the last capture was, and whether it cut anything.

        Walking the raw tree is O(nodes) in Python - microseconds at these
        sizes - against the 50-100ms a wrong read strategy costs. The provider's viewport metadata
        is consulted too: it records truncation for containers a provider
        reports without setting the raw node's flag, and its key is the window
        the capture actually described.
        """
        nodes = 0
        truncated = False
        for node in tree.walk():
            nodes += 1
            if node.truncated:
                truncated = True
        if partial:
            meta = getattr(self.provider, "viewport_meta", None) or {}
            for container in (meta.get(self.target_window or "") or {}).values():
                if container.get("items_truncated"):
                    truncated = True
        self._last_capture_nodes = nodes
        self._last_capture_truncated = truncated
        self._last_capture_window = self.target_window

    def _capture_prefers_full(self) -> bool:
        """Whether the next read of this window should skip the viewport walker.

        Only a *truncated* capture benefits from the walker (it skipped rows).
        Once we know the window fit whole, the walker's per-node round trips are
        pure overhead (measured: 127ms vs one 78ms subtree request on the form),
        so the next read is done in one request. ``None`` (nothing captured yet,
        e.g. the first observation of a session) keeps the walker: choosing full
        without knowing whether a list is long would cost seconds on a big
        window, while choosing the walker costs only ~50ms on a small one.
        """
        nodes = self._last_capture_nodes
        return (
            nodes is not None
            and self._last_capture_window == self.target_window
            and nodes <= _SMALL_WINDOW_NODES
            and not self._last_capture_truncated
        )

    def _snapshot_viewport(
        self,
        expanded_paths: set[tuple[int, ...]] | None = None,
        *,
        prefer_full: bool = True,
    ) -> tuple[RawTree, bool]:
        """The ``observe`` snapshot, and whether it is viewport-scoped.

        Falls back to the full read for a provider without viewport capture,
        when it is switched off, or when the capture fails - never to an empty
        tree. A fallback tree is complete, so the caller may keep it as settle's
        reuse frame; a viewport-scoped one may not.

        ``expanded_paths`` are native paths (from the previous result) of
        containers the caller asked to open; the provider reads a page for them
        instead of only the on-screen rows.
        """
        if not _VIEWPORT_ENABLED or self.target_window is None:
            return self._snapshot(), False
        # Small-window adaptation: a window whose last capture fit whole is
        # read with one subtree request. The first capture of a session still
        # uses the walker - there is no evidence yet that the tree is small,
        # and guessing "full" on a long list costs seconds instead of ~50ms.
        #
        # ``prefer_full=False`` is the waiting path: it wants viewport polls
        # and at most one deliberate escalation, not a full read on every poll.
        if prefer_full and self._capture_prefers_full():
            return self._snapshot(), False
        capture = getattr(self.provider, "snapshot_viewport", None)
        if capture is None:
            if getattr(self.provider, "name", "") == "peer":
                self._viewport_note = (
                    "peer has no viewport capture; full snapshot budget applies"
                )
            return self._snapshot(), False
        try:
            with _timed(self.timing, "snapshot"):
                try:
                    tree = capture(
                        self.target_window,  # type: ignore[arg-type]
                        expanded_paths=expanded_paths or set(),
                    )
                except TypeError:
                    # A provider that implements the basic form only.
                    tree = capture(self.target_window)  # type: ignore[arg-type]
        except KeyError:
            raise
        except Exception as exc:  # noqa: BLE001 - the full read is the fallback
            log.debug("viewport capture failed (%s); using the full snapshot", exc)
            self._viewport_note = getattr(
                self.provider, "viewport_fallback_note", ""
            ) or self._viewport_note
            return self._snapshot(), False
        # A provider that fell back internally must say so; the default is the
        # honest one for anyone implementing the method: it is viewport-scoped.
        partial = bool(getattr(self.provider, "last_viewport_partial", True))
        self._viewport_note = getattr(self.provider, "viewport_fallback_note", "")
        return tree, partial

    def _expand_paths(self, expanded: set[int] | set[str] | None) -> set[tuple[int, ...]]:
        """Native paths of refs the caller wants expanded, from the *current* result.

        Refs are session handles; the provider speaks native paths. This is the
        only place the two meet, and it runs before the new capture because
        after it the refs may have been re-allocated.
        """
        if not expanded or self.result is None:
            return set()
        previous_window = getattr(self.result, "window_id", None)
        if previous_window and str(previous_window) != self.target_window:
            # The previous result describes another window; its paths would
            # name different elements here.
            return set()
        paths: set[tuple[int, ...]] = set()
        for item in expanded:
            if not isinstance(item, str):
                continue
            source = self.result.sources.get(item)
            if source is not None:
                paths.add(tuple(source.native.path))
        return paths

    def _apply_viewport_meta(self, result: CompileResult) -> None:
        """Copy the provider's viewport metadata onto the compiled containers.

        The field names are the contract shared with the projection work:
        ``items_total`` / ``items_visible`` / ``items_truncated`` / optional
        ``scroll_pct``. They live in ``attrs`` so the renderer and any verifier
        can read them without the raw layer growing provider-only fields.
        """
        provider_meta = getattr(self.provider, "viewport_meta", None)
        if not provider_meta or self.target_window is None:
            return
        info_by_path = provider_meta.get(self.target_window) or {}
        if not info_by_path:
            return
        for node in result.root.walk():
            source = result.sources.get(node.ref)
            if source is None:
                continue
            info = info_by_path.get(tuple(source.native.path))
            if not info:
                continue
            node.attrs["items_total"] = int(info.get("items_total", 0))
            node.attrs["items_visible"] = int(info.get("items_visible", 0))
            node.attrs["items_truncated"] = bool(info.get("items_truncated", False))
            scroll = info.get("scroll_pct")
            if scroll is not None:
                node.attrs["scroll_pct"] = int(scroll)

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
        if self.target_window is None:
            self.refresh_windows()
        if self.target_window is None:
            raise SomnusError(ErrorCode.TARGET_NOT_FOUND, message="no window")
        return self._fetch_snapshot()

    def _recompile(self) -> CompileResult:
        return self._recompile_with(self._snapshot(), remember=True)

    def _recompile_with(self, tree: RawTree, *, remember: bool) -> CompileResult:
        """Compile ``tree`` and adopt it as the session's current result.

        ``remember=False`` is the viewport capture's path: the tree only holds
        the structure plus the rows that are on screen, so it must **not** become
        settle's reuse frame (a probe answers with a whole tree and would
        compare two different universes). The result is still adopted, because
        the refs it printed must resolve; whenever a later tool names a ref that
        is not in it, that tool recompiles in full (see ``find``/``inspect``/
        ``call``).
        """
        if remember:
            # Record the frame *before* compiling: compilation normalises raw
            # nodes in place, and settle compares against the as-read signature.
            self._remember_raw(tree)
        pack = self._current_pack()
        # Pin the window's ref: the session owns the wN namespace (the window
        # list and CALL wN.focus() hand it to the agent), so recompiling the
        # same window must not mint a fresh name for it.
        pinned = self._window_refs_by_id.get(self.target_window or "")
        result = self._compile_tree(tree, pack, window_ref=pinned)
        report = self.registry.reconcile(result)
        self.result = result
        self._viewport_limited = not remember
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
        compiled = self._compile_tree(tree, None)
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
        expanded: set[int] | set[str] | None = None,
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
        its own foreground window (see docs/reference/child-session.md).

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
            self._refs_need_observe = False
            tokens = count_tokens(text)
            self._trace_observe(text, tokens, truncated=False)
            return Observation(text=text, rev=self.rev, tokens=tokens)

        # Raise the target before reading it (and before the header reports its
        # active flag, which is only true after the raise). `_recompile` then
        # snapshots a window that is actually on screen.
        win = self._peek(self.target_window) if self.target_window else None
        if win is not None:
            # Refresh in place: the window list is ordered (refs and window
            # numbering are allocated from it), so the target keeps its slot.
            self.windows = [win if w.window_id == win.window_id else w for w in self.windows]

        # Viewport capture: the structure plus only the rows that are on screen.
        # A partial frame is kept out of settle's reuse slot (a probe answers
        # with a whole tree and would compare two different universes); the full
        # fallback is remembered exactly like before. Either way the compiled
        # result is adopted, so every printed ref resolves.
        expand_paths = self._expand_paths(expanded)
        tree, partial = self._snapshot_viewport(expand_paths)
        self._note_capture(tree, partial)
        # A small, untruncated *viewport* capture is the shape whose controls
        # sit below L0's depth/nodes/token caps (a UWP app's groups at depth 4,
        # their buttons at 5-6). Remember that about the window itself: later
        # full reads of the same window keep the adaptive budget.
        if partial:
            small = (
                (self._last_capture_nodes or 0) <= _SMALL_OBSERVE_NODES
                and not self._last_capture_truncated
            )
            self._small_observe_window = self.target_window if small else None
        elif self._small_observe_window != self.target_window:
            self._small_observe_window = None
        # The tree may not describe the window: a refused raise (or an empty
        # capture) is a fact about the *capture*, not about the application.
        # Both would otherwise read as "this window has no controls".
        warnings = self._observation_warnings(tree)
        result = self._recompile_with(tree, remember=not partial)
        self._apply_viewport_meta(result)
        # The watched set for settle: exactly the nodes this capture read.
        # A full fallback leaves no captured layers, so probing is disabled
        # until a viewport observe establishes them again.
        self._watch_natives = _unique_natives(result) if partial else []
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
        header: list[str] = list(warnings)
        if self._viewport_note:
            header.append(f"~ {self._viewport_note}")
        if include_other_windows:
            header.extend(self._window_header(result))
        if window_delta:
            header.append(window_delta)
        # The header (other windows + deltas) consumes part of the observation
        # budget: reserve tokens for it so the total stays within the level's
        # cap instead of max_tokens + header (found live: Steam L0 came out at
        # 365 tokens against a 300 cap because the 74-token header was extra).
        budget = self.budget.for_level(level)
        small_observe = (
            not full and self._small_observe_window == self.target_window
        )
        if small_observe:
            # Depth is the primary fix (L0 stops at 3; UWP buttons are at 5-6).
            # The token/node floor is what lets those lines survive, and the
            # raised auto-collapse threshold keeps a 10-button pad from folding
            # into one `+10 more` group. All three are capped, not removed.
            budget = ObservationBudget(
                max_nodes=max(budget.max_nodes, _SMALL_OBSERVE_MAX_NODES),
                max_depth=max(budget.max_depth, _SMALL_OBSERVE_MAX_DEPTH),
                max_tokens=max(budget.max_tokens, _SMALL_OBSERVE_MAX_TOKENS),
                hard_tokens=max(budget.hard_tokens, _SMALL_OBSERVE_MAX_TOKENS),
            )
        header_tokens = count_tokens("\n".join(header)) if header else 0
        # +6 reserves room for the ``@rev N`` line the renderer prepends.
        body_budget = ObservationBudget(
            max_nodes=budget.max_nodes,
            max_depth=budget.max_depth,
            max_tokens=max(100, budget.max_tokens - header_tokens - 6),
            hard_tokens=budget.hard_tokens,
        )
        engine = ProjectionEngine(
            body_budget,
            auto_collapse_threshold=(
                _SMALL_OBSERVE_AUTO_COLLAPSE if small_observe else 8
            ),
            l0=(level == 0),
        )
        projection = apply_projection(result.root, self._pack_projection())
        # `expanded` may be node ids (a caller that already holds this result)
        # or refs (the text path, which is the only stable handle before this
        # compilation exists). Refs are resolved *against the new result*: the
        # old id-based lookup always missed, which made `OBSERVE ... expand`
        # silently a no-op even though the marker promises it.
        expanded_ids: set[int] | None = None
        if expanded:
            expanded_ids = set()
            for item in expanded:
                if isinstance(item, int):
                    expanded_ids.add(item)
                    continue
                node = result.find(item)
                if node is not None:
                    expanded_ids.add(id(node))
        # The desktop as data, for the projection's `WINDOWS ...` line. It is
        # skipped when the caller asked for the long-form header
        # (``include_other_windows``): that header already lists the same
        # windows in the same order, and rendering both would say it twice.
        desktop_summary = (
            None if include_other_windows else self._desktop_summary(result)
        )
        plan = engine.plan(
            result.root,
            projection,
            full=full,
            expanded=expanded_ids,
            desktop_summary=desktop_summary,
        )

        text = render_document(plan.lines, rev=self.rev, extra_header=header)
        self._refs_need_observe = False
        tokens = count_tokens(text)
        self._trace_observe(text, tokens, truncated=self._last_capture_truncated)
        return Observation(text=text, rev=self.rev, tokens=tokens)

    def _single_active_window_id(self) -> str | None:
        """The one window that may be printed as `active`.

        The provider's window list is the primary evidence (a foreground-hwnd
        match). A peek/raise refreshes the *target's* entry in place, so for a
        moment the list can hold the old foreground window and the just-raised
        target as both active; rotating the desktop can also leave a stale
        flag. Rendering must still name exactly one: prefer the target (it was
        raised immediately before the render), otherwise the first claimant.
        """
        active = [w.window_id for w in self.windows if w.active]
        if not active:
            return None
        if len(active) == 1:
            return active[0]
        if self.target_window in active:
            return self.target_window
        return active[0]

    def _render_window_list(self) -> str:
        """The desktop as a flat list: every window, its ref and what it is.

        The refs are the same ones ``CALL wN.focus()`` accepts, so an agent can
        list, then switch, then observe - the way a person alt-tabs.
        """
        lines = [f"WINDOWS count={len(self.windows)}"]
        active_id = self._single_active_window_id()
        refs = self._sync_window_refs(self.result) if self.result is not None else {}
        frame_titles = {w.title for w in self.windows if w.frame}
        mixed_titles = {
            title
            for title in frame_titles
            if any(w.title == title and not w.frame for w in self.windows)
        }
        for w in self.windows:
            ref = refs.get(w.window_id) or self._header_window_ref(w.window_id)
            flags = []
            if w.window_id == self.target_window:
                flags.append("obs")
            if w.window_id == active_id:
                flags.append("active")
            if w.title in mixed_titles:
                # Only a listed frame/content pair is labelled: two separate
                # instances of one UWP app are both frames, and calling either
                # "content" would invent a row that does not exist.
                flags.append("frame" if w.frame else "content")
            suffix = (" " + " ".join(flags)) if flags else ""
            lines.append(f"  WINDOW {ref} {_q(w.title)}{suffix}")
        return "\n".join(lines)

    def _post_action_window_delta(self) -> str:
        """Window delta for a call response, only when the list was refreshed.

        With ``post_action_enum`` off the window list is the one from before the
        action. Reporting a delta against it would say "nothing changed" while
        a window may well have opened; worse, :meth:`_window_delta` advances
        ``_prev_windows`` to that stale set, so the *next* observation would no
        longer report the new window either. Skipping here keeps the baseline
        intact: the next real enumeration reports ``+ WINDOW`` as usual.
        """
        if not self.post_action_enum:
            return ""
        return self._window_delta()

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
        Both directions of the mapping are written by
        :meth:`_sync_window_refs`, so this header and the desktop summary can
        never name one window twice.
        """
        refs = self._sync_window_refs(result)
        return [
            f"WINDOW {refs[w.window_id]} {_q(w.title)}"
            for w in self.windows
            if w.window_id != self.target_window
        ]

    def _sync_window_refs(self, result: CompileResult) -> dict[str, str]:
        """The canonical ``window_id -> ref`` map for this compilation.

        The body prints the target under the tree root's ref and every other
        window under a header ref. This is the one place that writes **both**
        maps. Before it, only the forward map was updated for the target, so a
        reverse lookup (the compiler's window pin and the desktop summary)
        answered with a stale ref the body never printed: one window appeared
        as ``w2`` in the ``WINDOWS`` line and ``w1`` in the tree underneath it.
        """
        refs: dict[str, str] = {}
        target_ref = result.root.ref if result.root else None
        if target_ref and self.target_window:
            owner = self._window_refs.get(target_ref)
            if owner is not None and owner != self.target_window:
                # Another window already claimed the tree's ref; rehome it in
                # both directions first - refs must stay unique.
                while True:
                    spare = self.registry.allocator.next("w")
                    if spare not in self._window_refs:
                        break
                self._window_refs_by_id[owner] = spare
                self._window_refs[spare] = owner
            stale = self._window_refs_by_id.get(self.target_window)
            if (
                stale
                and stale != target_ref
                and self._window_refs.get(stale) == self.target_window
            ):
                # Drop the forward alias of the name this window used to go by,
                # so the pair stays one-to-one.
                self._window_refs.pop(stale, None)
            self._window_refs[target_ref] = self.target_window
            self._window_refs_by_id[self.target_window] = target_ref
            refs[self.target_window] = target_ref
        for window in self.windows:
            if window.window_id == self.target_window:
                continue
            refs[window.window_id] = self._header_window_ref(window.window_id)
        return refs

    def _header_window_ref(self, window_id: str) -> str:
        ref = self._window_refs_by_id.get(window_id)
        if ref is None:
            ref = self.registry.allocator.next("w")
            self._window_refs_by_id[window_id] = ref
            self._window_refs[ref] = window_id
        return ref

    def _observation_warnings(self, tree: RawTree) -> list[str]:
        """Warning lines for a capture that may not describe the window.

        Two failures make an application look like it has no controls, and
        neither is the window's content:

        * the raise was refused (``FOREGROUND_DENIED``) - a covered or minimized
          self-drawn window publishes an empty shell;
        * the snapshot is structurally empty while the window has a title -
          the window was read, its content was not rendered.

        The empty-tree test is deliberately the strictest one (the root has
        *no* children). A real small window - a dialog with two buttons - still
        has children, so it is never reported.
        """
        out: list[str] = []
        code = getattr(self.provider, "last_foreground_error", None)
        if code:
            out.append(
                f"~ foreground denied ({code}); tree may be empty for "
                "covered windows"
            )
        title = (tree.window.title or "").strip() if tree.window is not None else ""
        if title and not tree.root.children:
            out.append(
                "~ tree looks empty; window may be covered or app not rendering"
            )
        return out

    def _desktop_summary(self, result: CompileResult) -> list[dict] | None:
        """The desktop as *data* for the projection: one entry per window.

        Fields are ``window_id`` (the ref ``CALL <ref>.focus()`` accepts),
        ``title`` and ``active``. ``None`` when there is nothing to report (no
        windows enumerated), which the projection renders as today's output.

        ``ops`` is deliberately absent. Counting operable elements needs one
        shallow read per window; measured on five live suite windows that is
        ~111ms in total (43ms of it the form window alone), above the ~80ms an
        observation may spend on it. The structure therefore carries only what
        the window list already knows.

        Refs come from :meth:`_sync_window_refs`, the same map the body and the
        long-form header use, so the summary line, the rendered root and ``CALL
        wN.focus()`` / ``OBSERVE wN`` all name one window the same way.
        """
        if not self.windows:
            return None
        refs = self._sync_window_refs(result)
        out: list[dict] = []
        for window in self.windows:
            ref = refs.get(window.window_id)
            if ref is None:  # pragma: no cover - the map covers every window
                ref = self._header_window_ref(window.window_id)
            out.append(
                {
                    "window_id": ref,
                    "title": window.title or "",
                    "active": bool(window.active),
                }
            )
        return out

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

    def launch(
        self,
        command: Any,
        args: Any = None,
        cwd: Any = None,
        window: Any = None,
        wait_s: Any = 3.0,
    ) -> str:
        """Start a program on the current desktop or the child desktop."""
        try:
            if not isinstance(command, str) or not command.strip():
                raise SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    message="command must be a non-empty string",
                )
            command = command.strip()
            if args is None:
                args = []
            if not isinstance(args, list) or any(
                not isinstance(arg, str) for arg in args
            ):
                raise SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    message="args must be a list of strings",
                )
            if cwd is not None:
                if not isinstance(cwd, str) or not os.path.isdir(cwd):
                    raise SomnusError(
                        ErrorCode.INVALID_ARGUMENT,
                        message=f"cwd is not a directory: {cwd!r}",
                    )
            if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
                raise SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    message=f"wait_s expected number >= 0 got={wait_s!r}",
                )
            if not math.isfinite(float(wait_s)) or float(wait_s) < 0:
                raise SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    message=f"wait_s expected number >= 0 got={wait_s!r}",
                )
            wait_s = float(wait_s)

            use_peer = self.session_label == "child"
            if window is not None:
                if not isinstance(window, str) or not window.strip():
                    raise SomnusError(
                        ErrorCode.INVALID_ARGUMENT,
                        message="window must be a ref, 'child' or 'main'",
                    )
                selected = window.strip()
                if selected.lower() == "child":
                    use_peer = True
                elif selected.lower() == "main":
                    if self.session_label == "child":
                        raise SomnusError(
                            ErrorCode.ACTION_NOT_SUPPORTED,
                            message="switch to main before launching there",
                        )
                    use_peer = False
                elif re.fullmatch(r"[a-z]{1,3}[0-9]+", selected):
                    if self._window_by_ref(selected) is None:
                        raise target_not_found(selected)
                else:
                    raise SomnusError(
                        ErrorCode.INVALID_ARGUMENT,
                        message=f"unknown window {selected!r}; use wN or child",
                    )

            # Whether the program lands on the desktop the refs currently name.
            # `window=child` from main starts it on the other desktop: its
            # windows can be counted there, but the refs that come back are not
            # valid here, so the reply says so instead of minting names.
            on_current = not use_peer or self.session_label == "child"

            peer = None
            owns_peer = False
            if use_peer and on_current:
                peer = self.provider
            elif use_peer:
                peer = self._connect_launch_peer()
                owns_peer = True
            if not use_peer:
                self.policy.check_process(os.path.basename(command))

            try:
                baseline_windows = (
                    self.refresh_windows() if on_current else peer.list_windows()
                )
                baseline = {w.window_id for w in baseline_windows}
                sig_before = None if on_current else self._peer_signature(peer)

                if use_peer:
                    pid = self._launch_on_peer_pid(peer, command, args, cwd)
                else:
                    pid = self._launch_local_pid(command, args, cwd)

                if on_current:
                    new = self._wait_for_launch_window(baseline, wait_s)
                else:
                    new = self._wait_for_peer_launch_window(
                        peer, baseline, sig_before, wait_s
                    )
                return self._launch_reply(
                    pid,
                    new,
                    wait_s,
                    refs=on_current and not self._refs_need_observe,
                    desktop=None if on_current else "child",
                    note=(
                        "desktop changed; observe again"
                        if self._refs_need_observe
                        else ""
                    ),
                )
            finally:
                if owns_peer:
                    peer.close()
        except SomnusError as err:
            return err.render()
        except Exception as err:  # noqa: BLE001 - reported, never raised
            return SomnusError(
                ErrorCode.NATIVE_EXECUTION_FAILED, message=str(err)
            ).render()

    @staticmethod
    def _connect_launch_peer():
        from somnus.session.peerprovider import PeerProvider

        provider = PeerProvider()
        if not provider.connect():
            raise SomnusError(
                ErrorCode.ACTION_NOT_SUPPORTED,
                message="no peer is running in the child session",
            )
        return provider

    @staticmethod
    def _peer_signature(peer) -> object | None:
        signature = getattr(peer, "top_level_signature", None)
        if signature is None:
            return None
        try:
            return signature()
        except Exception:  # noqa: BLE001 - a hint must never break the call
            return None

    def _launch_local_pid(self, command: str, args: list[str], cwd: str | None) -> int:
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            [command, *args],
            cwd=cwd or None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
        return proc.pid

    def _launch_reply(
        self,
        pid: int,
        new: list[WindowInfo],
        wait_s: float,
        *,
        refs: bool,
        desktop: str | None = None,
        note: str = "",
    ) -> str:
        lines = [f"OK pid={pid}"]
        if new:
            for window in new[:6]:
                if refs:
                    ref = self._header_window_ref(window.window_id)
                    lines.append(f"+ WINDOW {ref} {_q(window.title)}")
                else:
                    lines.append(f"+ WINDOW {_q(window.title)}")
            if len(new) > 6:
                lines.append(f"+ {len(new) - 6} more")
            if len(new) > 1:
                lines.append(f"~ {len(new)} windows from one launch")
            if desktop is not None:
                lines.append(f"~ on {desktop}; switch {desktop} to address them")
        elif desktop is not None:
            lines.append(f"~ no window in {wait_s:g}s on {desktop}")
        else:
            lines.append(f"~ no window in {wait_s:g}s")
        if note:
            lines.append(f"~ {note}")
        return "\n".join(lines)

    def _wait_for_launch_window(
        self, baseline: set[str], wait_s: float
    ) -> list[WindowInfo]:
        """Poll the cheap desktop signature, then enumerate once when it moves."""
        deadline = time.monotonic() + wait_s
        while True:
            if self._desktop_changed() is not False:
                self.refresh_windows()
                new = self._launch_windows(baseline, self.windows)
                if new:
                    return new
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        self.refresh_windows()
        return self._launch_windows(baseline, self.windows)

    def _wait_for_peer_launch_window(
        self,
        peer,
        baseline: set[str],
        sig_before: object | None,
        wait_s: float,
    ) -> list[WindowInfo]:
        """The same bounded wait against another desktop's provider.

        The signature is the only polling cost; a provider without one is
        enumerated once at the deadline rather than on every tick.
        """
        deadline = time.monotonic() + wait_s
        while True:
            sig = self._peer_signature(peer)
            if sig is not None and sig_before is not None and sig != sig_before:
                windows = peer.list_windows()
                sig_before = sig
                new = self._launch_windows(baseline, windows)
                if new:
                    return new
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        return self._launch_windows(baseline, peer.list_windows())

    @staticmethod
    def _launch_windows(
        baseline: set[str], windows: list[WindowInfo]
    ) -> list[WindowInfo]:
        """Windows that were not on the desktop when the program started.

        This is a baseline diff rather than a pid filter: a launcher such as
        ``calc.exe`` hands the window to another process and exits, so the
        window a launch produced does not always carry the pid it returned.
        """
        return [w for w in windows if w.window_id not in baseline]

    def _launch_on_peer_pid(
        self, provider, command: str, args: list[str], cwd: str | None
    ) -> int:
        from somnus.session.peerprovider import PeerProcessError

        try:
            pid = provider.start_process(
                command,
                args,
                cwd=cwd,
                stdout_path=os.devnull,
                stderr_path=os.devnull,
            )
        except PeerProcessError as err:
            raise SomnusError(err.code, message=err.message) from err
        return pid

    def find(
        self,
        action: "Action | str",
        window: str | None = None,
        timeout_s: float | None = None,
        *,
        limit: int = 50,
    ) -> str:
        """Search the current window (or `window`) for a matching node.

        Two call shapes land here: the text/tool protocol passes an
        ``Action(kind="find", ...)``, and the waiting contract is
        ``find(pattern, window=None, timeout_s=None)``. ``timeout_s`` of
        ``None`` or ``0`` is exactly today's answer - one in-memory search, no
        extra reads. A positive value turns the search into a *wait*: the
        viewport is re-read until the query matches or the deadline passes.

        Waiting re-reads the viewport capture (structure + the rows on screen),
        not the whole tree: measured on the suite's 240-row store screen one
        viewport read is ~413ms against ~3923ms for a full snapshot, and it
        arrives as a compilable result, so a hit is answered immediately rather
        than after a second round-trip. The read is its own throttle; only a
        provider whose read is cheaper than :data:`_FIND_POLL_FLOOR_MS` gets an
        explicit sleep.

        A miss on a viewport-truncated collection is **not** retried with a full
        read while waiting: that read costs seconds and would push the timeout
        far past its deadline. The timeout reply says which rows were not
        searched, so the agent can widen the view with ``OBSERVE full``.

        A partial capture that did **not** cut rows (a popup or pane outside the
        captured layer) can afford one full read: after a few zero-hit viewport
        polls, while still in the first half of the deadline, the wait escalates
        exactly once. A hit from that read is marked ``(full read)`` so the
        caller sees why it arrived after the viewport polls came back empty.
        """
        if not isinstance(action, Action):
            action = Action(kind="find", query=str(action), scope=window)
        elif window:
            action = replace(action, scope=window)
        started = time.monotonic()
        hits = self._find_hits(action)
        if hits:
            return self._render_find_hits(hits, limit)
        waiting = timeout_s is not None and timeout_s > 0
        if self._viewport_limited and not waiting:
            # Today's behaviour: a row may simply not have been captured.
            self._recompile()
            hits = self._find_hits(action)
            if hits:
                return self._render_find_hits(hits, limit)
        if not waiting:
            return self._render_find_miss()
        return self._find_wait(action, float(timeout_s), started, limit)

    def _find_wait(
        self, action: Action, timeout_s: float, started: float, limit: int
    ) -> str:
        """Re-read the viewport until a match appears or ``timeout_s`` passes."""
        hits, polls, escalated = self._find_wait_hits(action, timeout_s, started)
        if hits:
            return self._render_find_hits(hits, limit, full_read=escalated)
        waited = time.monotonic() - started
        message = f"0 matches in {polls} viewport reads"
        if escalated:
            message += " (+1 full read)"
        # Only say "not searched" when the capture actually cut something: a
        # viewport read that kept every row (a small window) searched the whole
        # window, and the note would send the agent on a pointless OBSERVE.
        if any(
            bool(node.attrs.get("items_truncated"))
            for node in (self.result.root.walk() if self.result else ())
        ):
            message += (
                "; rows outside the captured viewport were not searched "
                "(use OBSERVE expand/full for a full read)"
            )
        # ``ref`` is the slot the error line prints right after the code; the
        # contract for a timed-out wait is ``ERR TARGET_NOT_FOUND after=<s>``.
        return SomnusError(
            ErrorCode.TARGET_NOT_FOUND,
            ref=f"after={waited:.1f}s",
            hint="observe",
            message=message,
        ).render()

    def _find_wait_hits(
        self, action: Action, timeout_s: float, started: float
    ) -> tuple[list[SemanticNode], int, bool]:
        """The waiting search: hits, number of reads, and whether a full read ran.

        Shared by ``find(timeout=)`` and a script's ``AWAIT`` so the two wait
        the same way - one implementation, one set of caveats about viewport
        reads. An empty list means the deadline passed without a match.

        One escalation rule, not a strategy: after a few zero-hit viewport
        polls, a **partial but untruncated** capture of a plausibly-small tree
        may spend exactly one full read. Truncated collections never escalate -
        their full read is the multi-second case a short timeout cannot hold -
        and the deadline gate keeps the one read inside the caller's budget.
        """
        deadline = started + timeout_s
        polls = 0
        escalated = False
        while time.monotonic() < deadline:
            poll_started = time.monotonic()
            tree, partial = self._snapshot_viewport(prefer_full=False)
            self._note_capture(tree, partial)
            result = self._recompile_with(tree, remember=not partial)
            self._apply_viewport_meta(result)
            # Same bookkeeping as ``observe``: settle's next probe must compare
            # against what this capture actually read.
            self._watch_natives = _unique_natives(result) if partial else []
            polls += 1
            hits = self._find_hits(action)
            if hits:
                return hits, polls, escalated

            if not escalated and self._find_escalation_ready(
                partial=partial, polls=polls, started=started, timeout_s=timeout_s
            ):
                tree = self._snapshot()
                self._note_capture(tree, False)
                result = self._recompile_with(tree, remember=True)
                self._apply_viewport_meta(result)
                self._watch_natives = []
                polls += 1
                escalated = True
                hits = self._find_hits(action)
                if hits:
                    return hits, polls, True

            spent_ms = (time.monotonic() - poll_started) * 1000
            if spent_ms < _FIND_POLL_FLOOR_MS:
                time.sleep(
                    min(
                        _FIND_POLL_FLOOR_MS / 1000,
                        max(0.0, deadline - time.monotonic()),
                    )
                )
        return [], polls, escalated

    def _find_escalation_ready(
        self, *, partial: bool, polls: int, started: float, timeout_s: float
    ) -> bool:
        """Whether this wait may spend its one full read now.

        The gates are deliberately conservative: a truncated capture (a long
        list) is never eligible, the tree must be small, the element must have
        been given a few polls to show up, the read must start in the first half
        of the deadline, and its conservative cost estimate must fit in the time
        that is left.
        """
        if not partial or self._last_capture_truncated:
            return False
        if self._last_capture_nodes is None:
            return False
        if self._last_capture_nodes > _FIND_ESCALATE_MAX_NODES:
            return False
        if polls < _FIND_ESCALATE_AFTER_POLLS:
            return False
        elapsed = time.monotonic() - started
        if elapsed < timeout_s * _FIND_ESCALATE_START_FRACTION:
            return False
        if elapsed >= timeout_s * _FIND_ESCALATE_DEADLINE_FRACTION:
            return False
        estimate_s = (
            0.05 + _FIND_ESCALATE_MS_PER_NODE * self._last_capture_nodes / 1000.0
        )
        return estimate_s <= timeout_s - elapsed

    def await_element(self, action: Action) -> SemanticNode:
        """The element a script's ``AWAIT`` step names, or a timeout error.

        A bound ``timeout=`` of zero is a single in-memory search; a positive
        one re-reads the viewport exactly as ``find(timeout=)`` does. The
        first hit is the binding: when several match, naming the type or a
        scope is how the caller narrows it, and the same first hit is what
        ``find`` would print first.
        """
        if self.result is None:
            self._recompile()
        timeout_s = (
            DEFAULT_AWAIT_TIMEOUT_S if action.timeout is None else action.timeout
        )
        started = time.monotonic()
        hits = self._find_hits(action)
        if not hits and self._viewport_limited:
            # The element may exist just outside the captured rows; one full
            # read before waiting or refusing, as ``find`` does.
            self._recompile()
            hits = self._find_hits(action)
        if not hits and timeout_s > 0:
            hits, _polls, _escalated = self._find_wait_hits(
                action, timeout_s, started
            )
        waited = time.monotonic() - started
        if hits:
            return hits[0]
        wanted = (
            f"{action.query_type.upper()} {_q(action.query or '')}"
            if action.query_type
            else _q(action.query or "")
        )
        raise SomnusError(
            ErrorCode.TARGET_NOT_FOUND,
            ref=f"after={waited:.1f}s",
            hint="observe",
            message=f"AWAIT {wanted} found nothing; the element did not appear",
        )

    def _find_hits(self, action: Action) -> list[SemanticNode]:
        """The matches for one search pass (no retries, no waiting)."""
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
        # the name, so reporting both is noise, not information. The exception
        # is a child that exposes an action the parent line does not: a combo
        # box and its editable INPUT share the label "文件名:", and hiding the
        # INPUT made FIND answer with a container that cannot take the write.
        by_fp = {n.fingerprint: n for n in nodes}
        parent_fps: set[str] = set()
        generic = {"read", "focus", "context_menu", "find"}
        for node in matches:
            parent = _find_parent(result_root(self), node)
            if parent is not None and parent.fingerprint in seen_fingerprints:
                extra = (set(node.cap_names()) - set(parent.cap_names())) - generic
                if not extra:
                    parent_fps.add(node.fingerprint)
        return [m for m in matches if m.fingerprint not in parent_fps]

    def _render_find_hits(
        self, matches: list[SemanticNode], limit: int, *, full_read: bool = False
    ) -> str:
        out = [f"FOUND {len(matches)}" + (" (full read)" if full_read else "")]
        # Full capability line: with default caps omitted (the grammars' implicit
        # table), a bare `+context_menu`-only line made the agent believe
        # activate was unavailable (found live: the Steam game row showed only
        # context_menu and the agent right-clicked instead of opening).
        opts = RenderOptions(include_default_caps=True, show_relations=False)
        for hit in matches[:limit]:
            out.append(render_node_line(hit, opts))
        if len(matches) > limit:
            out.append(f"… +{len(matches) - limit} more")
        return "\n".join(out)

    def _render_find_miss(self) -> str:
        # A bare `FOUND 0` dead-ends the agent. Name the top-level
        # containers of the current window so the next query can be
        # scoped (e.g. `FIND "..." in l2`) or an observe can re-orient.
        assert self.result is not None
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

    # ------------------------------------------------------------------ inspect

    def inspect_direct(self, ref: str, *, depth: int | None = None) -> str:
        """Convenience wrapper: inspect a ref and return its SUI-Text."""
        return self.inspect(Action(kind="inspect", ref=ref, depth=depth)).text

    def inspect(self, action: Action) -> Observation:
        if self._refs_need_observe:
            raise SomnusError(
                ErrorCode.STALE_TARGET,
                ref=action.ref,
                hint="observe",
                message="desktop changed; observe again",
            )
        if self.result is None:
            self._recompile()
        assert self.result is not None
        node = self.result.find(action.ref or "")
        if node is None and self._window_by_ref(action.ref or "") is not None:
            # A window ref from the list names a top-level window, not necessarily
            # the tree root printed by the last observe. Focus that window and
            # inspect its root, so every listed wN resolves.
            window = self._window_by_ref(action.ref or "")
            self.observe(action.ref or "")
            assert self.result is not None
            if self.target_window != window.window_id:
                # The window vanished between the list and the call; observe
                # fell back to another one, and its root is not this ref.
                raise target_not_found(action.ref or "")
            node = self.result.root
        if node is None and self._viewport_limited:
            # The ref may name a row that is not on screen; the viewport capture
            # never read it. One full read before reporting "no such target".
            self._recompile()
            node = self.result.find(action.ref or "")
        if node is None:
            raise target_not_found(action.ref or "")

        # Long text range read.
        if action.range and node.value is not None:
            start, end = action.range
            chunk = str(node.value)[start:end]
            text = (
                f"{node.display_type} {node.ref} "
                f"{render_node_line(display_node(node), RenderOptions(include_default_caps=True, show_relations=True))}\n"
                f'TEXT {node.ref} range={start}:{end} "{_esc(chunk)}"'
            )
            return Observation(text=text, rev=self.rev, tokens=count_tokens(text))

        opts = RenderOptions(include_default_caps=True, show_relations=True)
        depth = action.depth if action.depth is not None else self.budget.max_depth
        lines = [render_node_line(display_node(node), opts)]
        _render_subtree(node, lines, opts, depth, 1)
        text = f"@rev {self.rev}\n" + "\n".join(lines)
        return Observation(text=text, rev=self.rev, tokens=count_tokens(text))

    # ------------------------------------------------------------------ call

    def call(self, action: Action) -> CallOutcome:
        """Execute one capability call, with the trace envelope around it.

        Tracing is the only reason this is a wrapper: a call's *envelope*
        (start/end, why it ended, and the text the model saw before and after)
        is what a trace reader needs, while ``_call`` owns the behaviour. With
        tracing off this is one flag check and a direct delegation.
        """
        if not self.trace_sink.enabled:
            return self._call(action)
        started = time.perf_counter()
        call_id = self._trace_new_call_id()
        self._trace_call_stack.append(call_id)
        self._trace_snapshots[call_id] = {}
        try:
            before = self._last_observation_text
            if not before and self.result is not None:
                before = self._trace_sui_text(self.result)
            self._trace_event(
                "call.start",
                tool="call",
                ref=action.ref,
                cap=action.capability,
                args=action.args,
                rev=self.rev,
                window=self.target_window,
                target_states=_trace_target_states(self.result, action.ref),
                before=self._trace_snapshot(before, call_id, "before"),
            )
            outcome = self._call(action)
            self._trace_event(
                "call.end",
                status=outcome.status,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                settled=outcome.settled,
                after=self._trace_snapshots.get(call_id, {}).get("after"),
            )
            return outcome
        except SomnusError as err:
            self._trace_event(
                "call.end",
                status="ERR",
                error_code=str(err.code),
                hint=err.hint,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
                after=self._trace_snapshots.get(call_id, {}).get("after"),
            )
            raise
        except Exception as exc:  # noqa: BLE001 - record the envelope, then re-raise
            self._trace_event(
                "call.end",
                status="ERR",
                error_code="INTERNAL",
                message=str(exc),
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            raise
        finally:
            self._trace_call_stack.pop()
            self._trace_snapshots.pop(call_id, None)

    def _call(self, action: Action) -> CallOutcome:
        if self._refs_need_observe:
            raise SomnusError(
                ErrorCode.STALE_TARGET,
                ref=action.ref,
                hint="observe",
                message="desktop changed; observe again",
            )
        if self.result is None:
            self._recompile()
        assert self.result is not None

        # Window focus is a special session-level capability.
        win = self._window_by_ref(action.ref or "")
        if win is not None and action.capability == "focus":
            return self.focus_window(action.ref or "")
        if win is not None and self.result.find(action.ref or "") is None:
            # A listed window ref may differ from the root ref the last observe
            # printed. Focus and compile that window, then act on its root.
            self.observe(action.ref or "")
            assert self.result is not None
            if self.target_window != win.window_id:
                raise target_not_found(action.ref or "")
            if self.result.root.ref != action.ref:
                action = replace(action, ref=self.result.root.ref)

        # Stale check: the ref must still resolve in the current compilation.
        # Retry order is deliberately fixed and each step runs at most once:
        #   1. one recompile (the element may simply be in a newer frame);
        #   2. one structural relocation by the ref's last-seen locator (a
        #      mode switch re-parents a button and changes its fingerprint);
        #   3. the route chain, then one twin retry for "does not answer";
        #   4. one EFFECT_NOT_CONFIRMED route retry after settle.
        # There is no loop: a relocation that still fails reports STALE_TARGET.
        stale_heal_note: str | None = None
        if self.result.find(action.ref or "") is None:
            self._recompile()
            assert self.result is not None
            if self.result.find(action.ref or "") is None:
                # Strategy 1: the fingerprint's last-seen structural evidence
                # (role/name/index/depth/ancestors). Strategy 2: the re-resolvable
                # locator, which can break a structural tie with evidence the
                # caller already has (the capability it is about to run).
                relocated = self.registry.relocate(action.ref or "", self.result)
                if relocated is None:
                    relocated = self._locate_stale(
                        action.ref or "", action.capability
                    )
                if relocated is not None:
                    new_ref, evidence = relocated
                    stale_heal_note = (
                        f"~ healed {action.ref}\u2192{new_ref} (via {evidence})"
                    )
                    action = replace(action, ref=new_ref)
                else:
                    # Tell the agent what the ref *was*, so it can decide whether
                    # the element moved (find again) or is gone (give up). When
                    # the recipe still names several lookalikes, name them: the
                    # agent can pick one instead of guessing from a bare error.
                    record = self.registry.records.get(action.ref or "")
                    if record is not None:
                        candidates = self._locator_candidates(
                            action.ref or "", action.capability
                        )
                        hint = (
                            "; candidates: " + ", ".join(candidates)
                            if candidates
                            else ""
                        )
                        raise SomnusError(
                            ErrorCode.STALE_TARGET,
                            ref=action.ref,
                            message=(
                                f"was {record.role} "
                                + (f"{record.name!r} " if record.name else "")
                                + "; refind by name (FIND) or observe"
                                + hint
                            ),
                        )
                    raise SomnusError(ErrorCode.STALE_TARGET, ref=action.ref)

        # Ambiguity is *deferred*, not fatal: a ref that shares a fingerprint
        # with another node still names exactly one node in this compilation,
        # so it can be acted on first. If the element refuses the interface (or
        # died), `_execute_with_twin` retries its twin once. The set is kept for
        # diagnostics and for the batch's per-step report.

        # State actionability: a disabled control refuses a real action for a
        # real reason. A *briefly* disabled one (a submit button during a
        # validation round-trip) can clear within a frame or two, so this wait
        # is bounded and only paid when the action is already blocked - the
        # happy path reads the compiled states and does nothing else.
        self._preflight(action)

        resolver = Resolver(self.result)
        before_index = index_by_fingerprint(self.result.root)
        #: Window list before the action: a context_menu may open its menu as
        #: a separate top-level window, and comparing the two lists is the only
        #: reliable way to see that (the in-window delta cannot).
        self._windows_before_action = list(self.windows)

        resolution = self._resolve_with_hint(resolver, action)
        self.policy.check_process(self._target_process())

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
        # A twin may be the element the action actually lands on, so its
        # pre-action state is part of the baseline too.
        for candidate in self._twin_candidates(action.ref or ""):
            verifier.capture(candidate)

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

        # Walk the declared routes in order; the first one that works wins.
        # If the element refuses every route (or is gone), the same capability
        # is retried once on a twin ref that shares its fingerprint.
        chain, resolution, healed = self._execute_with_twin(
            action, resolution, resolver
        )
        effective_ref = healed or (action.ref or "")
        written = chain.written
        self._trace_event(
            "route.chain",
            route=chain.route,
            routes=[step.primitive for step in resolution.steps],
            written=written,
            healed=healed,
            remaining=len(chain.remaining),
        )

        # Settle, recompile, compute delta, verify.
        try:
            tree, result, delta, settle_result, popup_text = self._read_after_action(
                action, resolution, verifier, effective_ref, before_index
            )
        except KeyError:
            return self._target_replaced_outcome()

        verify = verifier.verify(resolution.effects, effective_ref, delta)
        self._trace_event(
            "verify",
            attempt="first",
            status=verify.status,
            checked=verify.checked,
            failed=list(verify.failed),
        )
        retried_via: str | None = None
        if verify.status == "EFFECT_NOT_CONFIRMED":
            # The route reported success but the settled tree does not show the
            # declared effect. Before refusing, try the next untried route on
            # the same target - once. It is the same capability through another
            # channel (measured on Chromium editors: the Value setter answers
            # success and writes nothing), not a risk judgement.
            original_error = SomnusError(
                ErrorCode.EFFECT_NOT_CONFIRMED,
                ref=effective_ref,
                message="; ".join(verify.failed),
            )
            retry = self._retry_unconfirmed(action, resolution, chain, effective_ref)
            if retry is None:
                raise original_error
            chain = retry
            written = retry.written
            try:
                tree, result, delta, settle_result, popup_text = self._read_after_action(
                    action, resolution, verifier, effective_ref, before_index
                )
            except KeyError:
                return self._target_replaced_outcome()
            verify = verifier.verify(resolution.effects, effective_ref, delta)
            self._trace_event(
                "verify",
                attempt="retry",
                route=retry.route,
                status=verify.status,
                checked=verify.checked,
                failed=list(verify.failed),
            )
            if verify.status == "EFFECT_NOT_CONFIRMED":
                # The retry did not confirm either: the original failure is the
                # honest answer, and there is no second retry.
                raise original_error
            retried_via = retry.route

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
        body = self._post_action_window_delta()
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
            echo = _write_echo(effective_ref, written)
            body = f"{body}\n{echo}" if body else echo
        # A `read` call is an answer, not a side effect: its payload (the
        # value, its length and whether it was truncated) is carried on the
        # primitive result and surfaced here. Without this line every read
        # collapsed to a bare `OK`, which is the exact complaint this fixes.
        read_payload = ""
        if chain.route == "read" and chain.exec_result is not None:
            read_payload = (getattr(chain.exec_result, "message", "") or "").strip()
        if read_payload:
            note = f"~ {effective_ref} {read_payload}"
            body = f"{body}\n{note}" if body else note
        if stale_heal_note:
            # A stale ref was relocated and the action then really ran: name
            # both refs and the evidence, so the agent's next CALL uses one
            # that resolves in this layout.
            body = f"{body}\n{stale_heal_note}" if body else stale_heal_note
        if healed:
            # The ref the caller named had no usable twin answer; the same
            # element (same fingerprint) did. Say which one acted, so the agent's
            # next CALL uses a ref that works.
            note = f"~ healed {action.ref}→{healed}"
            body = f"{body}\n{note}" if body else note
        if retried_via:
            # The first route reported success but its effect did not land; a
            # later route did. Say which one, or the agent will think the
            # original channel is reliable.
            note = f"~ retried via {retried_via}"
            body = f"{body}\n{note}" if body else note
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

        # P2-4, the `settled` half. The verifier flattens "no declared effects
        # but the tree visibly moved" to OK on the delta alone; this is the only
        # layer that knows whether the settle actually finished. A visible change
        # on a tree that timed out is evidence, not confirmation - the same
        # change may still be mid-flight - so it is reported as OK_UNVERIFIED
        # (with `settled=false`) instead of claiming OK. A *verified* declared
        # effect (checked > 0) is a concrete observation and keeps OK, and a
        # menu that really appeared is structural evidence, not a delta guess.
        if (
            status == "OK"
            and not settle_result.settled
            and verify.checked == 0
            and not (is_context_menu and menu_produced)
        ):
            status = "OK_UNVERIFIED"

        if is_context_menu and not menu_produced:
            # The menu gesture ran but produced nothing anywhere: not in this
            # window's tree, not as a shell popup, not as a new window.
            direct = sorted(
                c
                for c in _node_caps(self.result, effective_ref)
                if c in ("activate", "select", "toggle", "expand", "open")
            )
            hint = (
                f"; {effective_ref} does expose {'/'.join(direct)} - "
                f"try CALL {effective_ref}.{direct[0]}()"
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
            popup_text = self._mount_popup_content(effective_ref)
            if popup_text:
                tree_body = popup_text
                body = f"{body}\n{tree_body}" if body else tree_body
            elif "context_menu" in _node_caps(self.result, effective_ref):
                # activate ran, settled, and changed nothing - on a row that
                # also exposes context_menu. This is the QQ conversation-row
                # shape: the Invoke only highlights and the real command lives
                # in the menu. Say it once, here, so the agent does not
                # conclude the row is inert and give up.
                body = (body + "\n" if body else "") + (
                    f"~ no effect; {effective_ref} exposes context_menu - "
                    f'the command may be in the menu: CALL {effective_ref}.context_menu()'
                )
        # A scroll/expand moved the viewport: the watched set no longer names
        # what is on screen. Drop it (the next probe is a full read) and say so,
        # instead of leaving the agent to act on rows from the old viewport.
        if any(s.primitive in _VIEWPORT_MOVING_PRIMITIVES for s in resolution.steps):
            self._watch_natives = []
            note = "~ viewport changed; OBSERVE again"
            body = f"{body}\n{note}" if body else note
        return CallOutcome(
            status=status,
            rev=self.rev,
            delta=delta,
            text=f"{status} rev={self.rev}"
            + ("" if settle_result.settled else " settled=false")
            + ("\n" + body if body else ""),
            settled=settle_result.settled,
        )

    def _blocked_state(self, action: Action) -> preflight.PreflightResult | None:
        """The blocking preflight result for this action, or None."""
        if self.result is None:
            return None
        node = self.result.find(action.ref or "")
        if node is None:
            return None
        result = preflight.check(node, action.capability)
        return None if result.ok else result

    def _preflight(self, action: Action) -> None:
        """Refuse an action the element's *state* cannot accept, after a bounded wait.

        The check itself is free - it reads the states the last compilation
        already carries. The wait is the only cost, and it is paid exactly on
        the blocked path. Providers with no signature probe (a replay) cannot
        observe a change, so they get the historical immediate refusal.
        """
        blocked = self._blocked_state(action)
        if blocked is None:
            return
        waited_ms = 0
        if blocked.waitable and float(self.actionable_timeout_s) > 0:
            waited_ms = self._wait_for_actionable(action)
            if self._blocked_state(action) is None:
                self._trace_event("preflight", blocked=None, waited_ms=waited_ms)
                return
        self._trace_event(
            "preflight", blocked=blocked.state, waited_ms=waited_ms
        )
        raise preflight.error(blocked, action.ref, waited_ms=waited_ms)

    def _wait_for_actionable(self, action: Action) -> int:
        """Wait (bounded) for a blocked element's state to clear.

        Polls the provider's cheap whole-window signature and pays for a full
        read + compile only when the signature *changed* - the same evidence
        settle uses, so a state flip (disabled -> enabled) is visible and an
        unrelated repaint does not cause a read. Returns the time waited in
        milliseconds (0 when this provider offers no probe).
        """
        window = self.target_window
        probe = getattr(self.provider, "snapshot_signature", None)
        if window is None or probe is None:
            return 0
        poll_s = max(int(self.settler.poll_interval_ms), _PREFLIGHT_POLL_FLOOR_MS) / 1000
        start = time.monotonic()
        deadline = start + float(self.actionable_timeout_s)
        previous = self._settle_previous()
        previous_signature = previous[1] if previous else None
        if previous_signature is None:
            try:
                previous_signature = signature_of(probe(window))
            except KeyError:  # window vanished; the caller reports the state
                return 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(poll_s, remaining))
            try:
                signature = signature_of(probe(window))
            except KeyError:
                break
            if signature == previous_signature:
                continue
            previous_signature = signature
            try:
                self._recompile()
            except KeyError:
                break
            if self._blocked_state(action) is None:
                break
        return int((time.monotonic() - start) * 1000)

    def _read_after_action(
        self,
        action: Action,
        resolution,
        verifier: Verifier,
        effective_ref: str,
        before_index: dict,
    ) -> tuple[RawTree, CompileResult, Delta, Any, str]:
        """Settle, recompile, and diff the tree after an action attempt.

        Shared by the first attempt and the ``EFFECT_NOT_CONFIRMED`` retry: both
        need the same evidence ("did the tree move"), and the verifier's
        baseline stays the one captured before the action, so a change made by
        either attempt counts.
        """
        # Re-raise before the settle snapshot: the action (or the user) may have
        # moved the foreground away, and the settled tree is only complete for a
        # window that is rendered.
        if self.target_window:
            self._peek(self.target_window)
        # Layered probe: compare only the watched set (captured layers + the
        # touched ref and its ancestors). No captured layer, or a previous frame
        # whose watched set is not fully evaluable -> no probe at all (full reads).
        with _timed(self.timing, "settle"):
            tree, settle_result = self._settle(
                fetch=self._fetch_snapshot, refs=[effective_ref]
            )
        self._trace_event(
            "settle",
            settled=settle_result.settled,
            timed_out=settle_result.timed_out,
            snapshots=settle_result.snapshots,
            probes=settle_result.probes,
            waited_ms=settle_result.waited_ms,
        )
        # The window list is refreshed *after* settle, not before it: an
        # application may finish its title/metadata update a few milliseconds
        # after the action returns (measured on Notepad: the title gained its
        # "*" marker ~8ms after the write). A signature taken before settle
        # still describes the old desktop and would skip the enumeration, so
        # the rename was reported one CALL late (and two renames could merge).
        if self.post_action_enum:
            self._refresh_after_action()
        # Signature before compile (normalisation rewrites the raw nodes).
        self._remember_raw(tree)
        result = self._compile_tree(tree, self._current_pack())
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
                effective_ref,
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
                popup_text = self._mount_popup_content(effective_ref)

        # The after-snapshot is rendered once, and only when tracing: it is the
        # settled tree the model is about to be shown, including a mounted menu.
        if self.trace_sink.enabled and self._trace_call_stack:
            self._trace_snapshot(
                self._trace_sui_text(result), self._trace_call_stack[-1], "after"
            )

        after_index = index_by_fingerprint(result.root)
        delta = diff_trees(before_index, after_index)
        self._trace_event(
            "delta",
            changed=len(delta.changed),
            added=len(delta.added),
            removed=len(delta.removed),
            folded=delta.removed_folded,
        )
        return tree, result, delta, settle_result, popup_text

    def _target_replaced_outcome(self) -> CallOutcome:
        """The response for a target window that vanished mid-call.

        The action itself succeeded; report unverified with a window delta
        instead of failing the whole call. This path re-enumerates
        unconditionally: the window is gone, so the list is the only thing that
        can say what replaced it.
        """
        self.refresh_windows()
        text = f"OK_UNVERIFIED rev={self.rev}\n{self._window_delta()}".strip()
        text += (
            f"\n~ target replaced (window {self.target_window} closed)"
            if self._window_by_ref(self.target_window) is None
            else ""
        )
        return CallOutcome(
            status="OK_UNVERIFIED",
            rev=self.rev,
            delta=Delta(),
            text=text,
            settled=False,
        )

    def _retry_unconfirmed(
        self, action: Action, resolution, chain: _ChainResult, ref: str
    ) -> _ChainResult | None:
        """Run the next untried route after ``EFFECT_NOT_CONFIRMED`` (once).

        The honest next step is the chain's *next* alternative on the same
        target: a capability's routes are alternatives, and one of them may
        reach the control through a channel the first one only pretended to
        use. This is not a risk decision and not a confirmation gate - it is
        one more attempt, and only one. ``None`` means there is nothing left to
        try (or the next route is unusable), so the caller re-raises the
        original effect error.
        """
        if chain.undetermined or not chain.remaining:
            return None
        retry_action = replace(action, ref=ref)
        try:
            retried = self._execute_chain(
                retry_action, resolution, steps=chain.remaining
            )
        except SomnusError as err:
            if _TRACE_STATES:
                log.info("effect retry unusable (%s); keeping the original error", err.code)
            return None
        if _TRACE_STATES:
            log.info(
                "effect not confirmed: retried %s via %s", ref, retried.route
            )
        return retried

    def _execute_step(self, step) -> Any:
        with _timed(self.timing, "inject"):
            return self.provider.execute(step.native, step.primitive, step.value)

    # -------------------------------------------------------------- twin retry

    def _fingerprint_of_ref(self, ref: str) -> str | None:
        if self.result is not None:
            fingerprint = self.result.fingerprints.get(ref)
            if fingerprint:
                return fingerprint
        return self.registry.fingerprint_of(ref)

    def _locate_stale(
        self, ref: str, capability: str | None
    ) -> tuple[str, str] | None:
        """Second relocation strategy: the re-resolvable locator.

        Only called after the registry's structural relocation declined (no
        record, a tie, or no unique winner). The recipe is built from what the
        record already carries - role, name, entity - plus the capability the
        caller is about to run, which is the evidence available here and not to
        the registry. A tie that survives that filter is still a tie: the
        caller keeps STALE_TARGET rather than acting on a lookalike.
        """
        record = self.registry.records.get(ref)
        if record is None or self.result is None:
            return None
        if record.window_id and record.window_id != self.target_window:
            return None
        if not record.name and not record.entity:
            return None
        if capability:
            capped = locators.match(
                self.result.root,
                locators.from_record(record, capability=capability),
            )
            if capped.unique and capped.ref != ref:
                return capped.ref, capped.locator.describe()
        plain = locators.match(
            self.result.root, locators.from_record(record)
        )
        if plain.unique and plain.ref != ref:
            return plain.ref, plain.locator.describe()
        return None

    def _locator_candidates(
        self, ref: str, capability: str | None, *, limit: int = 3
    ) -> list[str]:
        """Lookalikes the locator still finds for a ref that did not resolve."""
        record = self.registry.records.get(ref)
        if record is None or self.result is None:
            return []
        if record.window_id and record.window_id != self.target_window:
            return []
        result = locators.match(
            self.result.root,
            locators.from_record(record, capability=capability),
        )
        if result.found <= 1:
            # Also offer the broader recipe when the capability filter emptied
            # it: "these are the same-named elements that exist" is still
            # useful evidence.
            result = locators.match(
                self.result.root, locators.from_record(record)
            )
        if result.found <= 1:
            return []
        return result.candidate_lines(limit)

    def _twin_candidates(self, ref: str) -> list[str]:
        """Live refs of *this* window that share ``ref``'s fingerprint.

        A fingerprint identifies one element shape; two nodes may carry it (a
        WPF combo exposing each row twice, a tray flyout holding a hidden
        copy), and the runtime used to refuse both refs outright. The ref the
        agent holds still names exactly one node, so it is usable; the twin is
        the fallback for when that node turns out not to answer.

        Scoped to the current window on purpose: the same fingerprint in another
        window names a different native element. Stale refs (not resolvable in
        the current compilation) are dropped - rebinding an action onto a
        ref that no longer names anything would be a silent retarget.
        """
        if not ref or self.result is None:
            return []
        fingerprint = self._fingerprint_of_ref(ref)
        if not fingerprint:
            return []
        out: list[str] = []
        for candidate in self.registry.candidates_for(
            fingerprint, window_id=self.target_window
        ):
            if candidate == ref or candidate in out:
                continue
            if self.result.find(candidate) is None:
                continue
            out.append(candidate)
        return out

    def _retryable_twin(self, action: Action, err: SomnusError) -> str | None:
        """The twin to retry after ``err``, or None when the error is not that kind.

        Only "this element does not answer" earns a retry: a missing interface
        (``ACTION_NOT_SUPPORTED``) or a vanished element (``STALE_TARGET``). A
        disabled control, a UIPI denial or a failed effect is a real answer about
        the element and must not be routed around onto a sibling.
        """
        if err.code not in (ErrorCode.ACTION_NOT_SUPPORTED, ErrorCode.STALE_TARGET):
            return None
        candidates = self._twin_candidates(action.ref or "")
        return candidates[0] if candidates else None

    def _execute_with_twin(
        self,
        action: Action,
        resolution,
        resolver: "Resolver",
    ) -> tuple[_ChainResult, Any, str | None]:
        """Execute the action; on "does not answer", retry the same capability once.

        Returns ``(chain, resolution_actually_used, healed_ref_or_None)``. The
        twin carries the same fingerprint, so its capability set and risk are
        the same; it is re-resolved only because its native reference differs.
        """
        try:
            return self._execute_chain(action, resolution), resolution, None
        except SomnusError as err:
            route_failed = err.code in (
                ErrorCode.ACTION_NOT_SUPPORTED,
                ErrorCode.NATIVE_EXECUTION_FAILED,
            )
            if action.capability == "set_text" and route_failed:
                nested = self._nested_text_ref(action.ref or "")
                if nested is not None:
                    nested_action = replace(action, ref=nested)
                    try:
                        nested_resolution = self._resolve_with_hint(
                            resolver, nested_action
                        )
                        chain = self._execute_chain(nested_action, nested_resolution)
                        return chain, nested_resolution, nested
                    except SomnusError:
                        err.message = (
                            f"{err.message}; try {nested} (INPUT inside COMBOBOX)"
                            if err.message
                            else f"try {nested} (INPUT inside COMBOBOX)"
                        )
            twin = self._retryable_twin(action, err)
            if twin is None:
                hint = self._failure_hint(action, err)
                if hint:
                    err.message = f"{err.message}; {hint}" if err.message else hint
                raise
            twin_action = replace(action, ref=twin)
            twin_resolution = self._resolve_with_hint(resolver, twin_action)
            try:
                chain = self._execute_chain(twin_action, twin_resolution)
            except SomnusError as twin_err:
                hint = self._failure_hint(action, twin_err)
                if hint:
                    twin_err.message = (
                        f"{twin_err.message}; {hint}"
                        if twin_err.message
                        else hint
                    )
                raise
            return chain, twin_resolution, twin

    def _nested_text_ref(self, ref: str) -> str | None:
        """A writable INPUT inside a container such as a save-dialog COMBOBOX."""
        if self.result is None:
            return None
        node = self.result.find(ref)
        if node is None:
            return None
        for child in node.walk():
            if child.ref == ref or child.role != "input":
                continue
            if "set_text" in child.cap_names():
                return child.ref
        return None

    def _parent_of_ref(self, ref: str):
        if self.result is None:
            return None
        chain = _ref_path(self.result.root, ref)
        return chain[-2] if len(chain) > 1 else None

    def _combo_text_holders(self, ref: str, *, limit: int = 2) -> list[str]:
        """Real text fields: INPUTs nested inside a SELECT (a combo box).

        A save dialog lists its folder contents as rows whose cells are also
        named ``名称`` and also advertise ``set_text``. Those are table cells,
        not fields: the filename is the INPUT inside the ``文件名`` combo.
        When a write is aimed at the wrong one, naming the combo's field is
        the only candidate that is evidence rather than a guess.
        """
        if self.result is None:
            return []
        out: list[str] = []
        for node in self.result.root.walk():
            if node.ref == ref or node.role != "input":
                continue
            parent = self._parent_of_ref(node.ref)
            if parent is None or parent.role != "select":
                continue
            if "set_text" not in node.cap_names():
                continue
            label = f'{node.ref} (INPUT inside COMBOBOX {parent.name!r})'
            out.append(label)
            if len(out) >= limit:
                break
        return out

    def _text_failure_hint(self, ref: str) -> str:
        """A truthful candidate for a failed text write on a non-field node."""
        node = self.result.find(ref) if self.result is not None else None
        if node is None:
            return ""
        parent = self._parent_of_ref(ref)
        if node.role == "input" and parent is not None and parent.role == "select":
            # The target already is the combo's own field; retrying the nested
            # input is the right move, and naming another ref would be a guess.
            return ""
        holders = self._combo_text_holders(ref)
        return f"try {holders[0]}" if holders else ""

    def _failure_hint(self, action: Action, err: SomnusError) -> str:
        """A one-line candidate for the capability that just refused."""
        if action.capability == "set_text" and err.code in (
            ErrorCode.ACTION_NOT_SUPPORTED,
            ErrorCode.NATIVE_EXECUTION_FAILED,
        ):
            return self._text_failure_hint(action.ref or "")
        if (
            action.capability == "activate"
            and err.code == ErrorCode.ACTION_NOT_SUPPORTED
        ):
            return self._activate_failure_hint(action.ref or "")
        return ""

    def _activate_failure_hint(self, ref: str) -> str:
        """Name the selection gesture when an Invoke exists but answers NULL.

        Calculator panel items publish Invoke and SelectionItem; their Invoke
        returns "NULL COM pointer access", while SelectionItem.Select is the
        gesture the app actually implements. The capability was real (the
        platform advertised it), so the failure is reported, not hidden - with
        the sibling capability the element does answer.
        """
        node = self.result.find(ref) if self.result is not None else None
        if node is not None and "select" in node.cap_names():
            return "try select"
        return ""

    def _execute_chain(
        self, action: Action, resolution, *, steps: list | None = None
    ) -> _ChainResult:
        """Walk a resolution's route chain at execution time.

        ``steps`` is supplied only by the ``EFFECT_NOT_CONFIRMED`` retry: it is
        the tail of the chain the first attempt never reached, already in the
        order that attempt would have walked. Re-deriving the order here would
        be wrong - the successful route has just been remembered and would sort
        back to the front, replaying it instead of advancing.

        A capability may carry several `via` steps (write through the Value
        pattern, *else* focus and type) and the resolver keeps the ones the
        node's patterns allow. Which one actually works is a fact about the
        live element that no amount of static inspection can settle - UIA
        reports patterns the element then refuses (measured: a Chromium
        editable advertises `value` and its setter is a no-op shell) - so the
        chain is walked here, at execution time, and the first route that
        *works* wins.

        Only a missing interface advances the chain. A pattern that exists
        and fails (disabled, UIPI, a raised COM error) is a real answer about
        this element and is reported instead: silently trying something else
        would be the runtime overruling what it was told to do.

        Raises :class:`SomnusError` for a real failure - the same codes
        ``call`` has always raised; a batch step catches it and stops there.
        """
        exec_result = None
        route = ""
        written: Any = None
        ordered_steps = (
            list(steps)
            if steps is not None
            else self._order_steps(action.ref or "", resolution.steps)
        )
        denied: list[tuple[str, SomnusError]] = []
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
            exec_result = self._execute_step(step)
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
                return _ChainResult(
                    exec_result,
                    route=route,
                    undetermined=True,
                    remaining=ordered_steps[index + 1 :],
                )
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
                return _ChainResult(
                    exec_result,
                    route=route,
                    written=written,
                    remaining=ordered_steps[index + 1 :],
                )
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
                # Carry the last route's own answer, not just "unsupported": the
                # select read-back ("Select returned but the element did not
                # become selected") is the one detail that tells a caller the
                # gesture ran and was ignored, rather than being unavailable.
                # It is only surfaced here, on total failure - on a chain that
                # escalates and succeeds it would describe an attempt that no
                # longer matters.
                detail = (
                    f"; {exec_result.message}" if exec_result.message else ""
                )
                raise SomnusError(
                    ErrorCode.ACTION_NOT_SUPPORTED,
                    ref=action.ref,
                    message=f"no usable interface on this element (tried {tried}){detail}",
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
        # Unreachable: the loop returns or raises on every path.
        return _ChainResult(exec_result, route=route, written=written)

    def call_batch(self, actions: list[Action]) -> CallOutcome:
        """Execute several CALL statements in one round trip.

        The batch is the unit, not the statement. Every step is resolved and
        authorised against the compilation as it stands *before* the first one
        runs - a step that could never work must be refused while refusing it
        costs nothing, not after the steps before it have moved the desktop.
        The steps then run in order without settling between them, and the
        whole batch ends with one settle, one delta and one rev: the response
        reports what all of the steps did together.

        Consequences of that shape, which the response states rather than
        hides:

        * steps may only name elements that exist when the batch starts. A
          flow whose later steps act on something an earlier step *creates*
          (open a menu, then invoke its items) is two calls, because the menu
          items only come into being at the settle between them;
        * a step that fails stops the batch there. The steps before it really
          ran and their delta is reported; the steps after it are ``not-run``;
        * a step whose outcome is undetermined (a modal dialog may be opening)
          stops the batch the same way - no later step can be judged while the
          call may still be in flight.
        """
        if self.result is None:
            self._recompile()
        assert self.result is not None
        if not actions:
            raise SomnusError(ErrorCode.PARSE_ERROR, message="empty batch")

        # ---------------------------------------------------------- dry run
        # Resolve and authorise every step against the current compilation.
        # Nothing has executed yet, so a refusal here is free: the desktop is
        # exactly as it was, and the caller can fix its arguments and resend.
        def _first_missing() -> int | None:
            return next(
                (
                    i
                    for i, a in enumerate(actions)
                    if self.result.find(a.ref or "") is None
                ),
                None,
            )

        missing_index = _first_missing()
        if missing_index is not None:
            # Stale refs get the one recompile `call` grants them; after that
            # a ref that still resolves nowhere is reported as what it is.
            self._recompile()
            missing_index = _first_missing()
            if missing_index is not None:
                ref = actions[missing_index].ref or ""
                record = self.registry.records.get(ref)
                was = (
                    f" was {record.role} "
                    + (f"{record.name!r} " if record.name else "")
                    if record is not None
                    else ""
                )
                raise SomnusError(
                    ErrorCode.STALE_TARGET,
                    ref=ref,
                    message=(
                        f"step {missing_index + 1}{was}; "
                        "refind by name (FIND) or observe"
                    ),
                )

        self.policy.check_process(self._target_process())
        resolver = Resolver(self.result)
        resolutions = []
        for index, action in enumerate(actions, start=1):
            win = self._window_by_ref(action.ref or "")
            if win is not None and action.capability == "focus":
                raise SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    ref=action.ref,
                    message=(
                        f"step {index}: CALL wN.focus() cannot be a batch step - "
                        "switching windows re-allocates refs; call it separately"
                    ),
                )
            if action.capability == "context_menu":
                raise SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    ref=action.ref,
                    message=(
                        f"step {index}: context_menu cannot be a batch step - the "
                        "menu only exists after a settle, so later steps could not "
                        "name it; make it a separate CALL"
                    ),
                )
            # Ambiguity is deferred for batch steps too: each ref names one node
            # here, and a step that finds no usable interface retries its twin
            # in the execute loop below.
            resolution = self._resolve_with_hint(resolver, action)
            # State is checked here, without a wait: nothing in this batch has
            # run yet, so a disabled step cannot clear before its turn - and a
            # wait per step would delay a refusal that is still free.
            blocked = preflight.check(resolution.node, action.capability)
            if not blocked.ok:
                raise preflight.error(blocked, action.ref)
            # A refused *route* is skipped at execution, as in a single call;
            # a step whose every route is refused can never run, which the
            # dry run refuses here while refusal is still free.
            authorized = False
            for step in resolution.steps:
                try:
                    self.policy.check_step(step.primitive)
                    authorized = True
                except SomnusError:
                    continue
            if resolution.steps and not authorized:
                raise SomnusError(
                    ErrorCode.ACTION_NOT_SUPPORTED,
                    ref=action.ref,
                    message=f"step {index}: every declared route is refused by policy",
                )
            resolutions.append(resolution)

        # ------------------------------------------------------- baseline
        before_index = index_by_fingerprint(self.result.root)
        #: Window list before the batch: a window that appears mid-batch is
        #: reported through the window delta at the end, as a single call does.
        self._windows_before_action = list(self.windows)
        verifier = Verifier(
            by_ref={n.ref: n for n in self.result.root.walk()},
            by_entity={
                n.entity: n for n in self.result.root.walk() if n.entity
            },
        )
        for resolution in resolutions:
            for name in (resolution.ref, *(e.target for e in resolution.effects)):
                if not name or name.startswith("$"):
                    continue
                verifier.capture(name)
                entity_node = verifier.by_entity.get(name)
                if entity_node is not None:
                    verifier.capture(entity_node.ref)
            # A twin that a step may fall back to needs its pre-action state too.
            for candidate in self._twin_candidates(resolution.ref):
                verifier.capture(candidate)

        # -------------------------------------------------------- execute
        #: Per-step state, in order: ``ok``, ``dispatched`` (sent, outcome
        #: being settled), ``ERR <code>``, or ``not-run`` (the batch stopped
        #: before reaching it).
        step_states: list[str] = []
        writes: list[tuple[str, Any]] = []
        #: ``a→b`` per healed step, reported as a note line under the batch.
        healed_notes: list[str] = []
        error_line: str | None = None
        stopped_at = 0
        undetermined_at = 0
        for index, (action, resolution) in enumerate(
            zip(actions, resolutions), start=1
        ):
            try:
                chain, used_resolution, healed = self._execute_with_twin(
                    action, resolution, resolver
                )
            except SomnusError as err:
                step_states.append(f"ERR {err.code}")
                writes.append(("", None))
                error_line = err.render()
                stopped_at = index
                break
            if healed:
                # The verification baseline and `$self` effects must name the
                # element that actually acted.
                resolutions[index - 1] = used_resolution
                healed_notes.append(f"step {index} healed {action.ref}→{healed}")
            if chain.undetermined:
                step_states.append("dispatched")
                writes.append(("", None))
                undetermined_at = index
                break
            step_states.append("ok")
            writes.append(
                (used_resolution.ref or "", chain.written)
                if chain.written is not None
                else ("", None)
            )
        ran = len(step_states)

        # ---------------------------------------------------- settle once
        if self.target_window:
            self._peek(self.target_window)
        try:
            with _timed(self.timing, "settle"):
                tree, settle_result = self._settle(
                    fetch=self._fetch_snapshot,
                    refs=[resolution.ref for resolution in resolutions],
                )
        except KeyError:
            # The target window vanished mid-batch. Whatever ran really ran;
            # report unverified with a window delta instead of failing all of it.
            # Unconditional re-enumeration: the target is gone, and only the
            # window list can say what replaced it.
            self.refresh_windows()
            text = (
                f"BATCH OK_UNVERIFIED steps={ran}/{len(actions)} rev={self.rev}\n"
                + self._step_lines(actions, step_states)
                + "\n~ target replaced (window closed)"
            )
            return CallOutcome(
                status="OK_UNVERIFIED",
                rev=self.rev,
                delta=Delta(),
                text=text,
                settled=False,
            )
        # Refresh after settle for the same reason as a single call: the
        # desktop's titles are stable by the time the settle finished.
        if self.post_action_enum:
            self._refresh_after_action()
        # Signature before compile (normalisation rewrites the raw nodes).
        self._remember_raw(tree)
        result = self._compile_tree(tree, self._current_pack())
        self.registry.reconcile(result)
        self.result = result
        self._bump_rev()

        after_index = index_by_fingerprint(result.root)
        delta = diff_trees(before_index, after_index)

        # ------------------------------------------------------- verify
        # Only the steps that ran have their effects verified: a not-run step's
        # effects did not happen and must not be checked against the final
        # state. A dispatched (undetermined) step's effects may still land -
        # the settle above is what they wait for.
        verifiable = ran if not error_line else ran - 1
        grouped = [
            (index, resolutions[index - 1].ref, resolutions[index - 1].effects)
            for index in range(1, verifiable + 1)
            if resolutions[index - 1].effects
        ]
        verify = verifier.verify_grouped(grouped, delta)

        # -------------------------------------------------------- render
        #: ``steps=`` counts what was *performed*: an ok step ran, a
        #: dispatched step was sent (its outcome is being settled), a failed
        #: step did not complete and is named by failed-at/stopped-at.
        performed = sum(1 for s in step_states if s in ("ok", "dispatched"))
        body = self._post_action_window_delta()
        tree_body = self.delta_text(delta)
        if tree_body:
            body = f"{body}\n{tree_body}" if body else tree_body
        for ref, value in writes:
            if not ref:
                continue
            echo = _write_echo(ref, value)
            body = f"{body}\n{echo}" if body else echo

        settled_tag = "" if settle_result.settled else " settled=false"
        if any(
            step.primitive in _VIEWPORT_MOVING_PRIMITIVES
            for resolution in resolutions
            for step in resolution.steps
        ):
            self._watch_natives = []
            note = "~ viewport changed; OBSERVE again"
            body = f"{body}\n{note}" if body else note
        if error_line:
            head = (
                f"BATCH PARTIAL steps={performed}/{len(actions)} "
                f"failed-at={stopped_at} rev={self.rev}{settled_tag}"
            )
            status = "PARTIAL"
        elif undetermined_at:
            head = (
                f"BATCH PARTIAL steps={performed}/{len(actions)} "
                f"stopped-at={undetermined_at} rev={self.rev}{settled_tag}"
            )
            status = "PARTIAL"
        else:
            head = f"BATCH {verify.status} steps={performed}/{len(actions)} rev={self.rev}{settled_tag}"
            status = verify.status
            if status == "OK_UNVERIFIED" and settle_result.settled and delta.empty:
                # A settled no-op batch is a plain OK, exactly as a single
                # call is: agents retry UNVERIFIED work that already succeeded.
                status = "OK"
                head = f"BATCH OK steps={performed}/{len(actions)} rev={self.rev}{settled_tag}"
            # P2-4's settled half, same as a single call: a visible change on a
            # tree that never finished settling is evidence, not confirmation.
            if status == "OK" and not settle_result.settled and verify.checked == 0:
                status = "OK_UNVERIFIED"
                head = f"BATCH OK_UNVERIFIED steps={performed}/{len(actions)} rev={self.rev}{settled_tag}"

        text = head + "\n" + self._step_lines(actions, step_states)
        for note in healed_notes:
            text += f"\n~ {note}"
        if error_line:
            text += f"\n{error_line}"
        elif undetermined_at:
            text += (
                f"\n~ step {undetermined_at} may still be in flight (a modal "
                "dialog opening); the state above is post-settle - judge from "
                "it before continuing"
            )
        if verify.status == "EFFECT_NOT_CONFIRMED":
            for line in verify.failed:
                text += f"\n~ not confirmed: {line}"
        if error_line:
            text += (
                f"\n~ steps 1-{ran - 1 if ran > 1 else ran} really ran; fix the "
                "failing step and continue with fresh CALL statements"
                if ran > 1
                else "\n~ the failing step ran first; nothing else was sent"
            )
        elif undetermined_at and ran < len(actions):
            text += (
                f"\n~ {len(actions) - ran} step(s) not run; continue with fresh "
                "CALL statements once the outcome is clear"
            )
        if body:
            text += f"\n{body}"
        return CallOutcome(
            status=status, rev=self.rev, delta=delta, text=text,
            settled=settle_result.settled,
        )

    @staticmethod
    def _step_lines(actions: list[Action], step_states: list[str]) -> str:
        """One line per statement: the statement as sent, then its state."""
        lines = []
        for index, (action, state) in enumerate(zip(actions, step_states), start=1):
            raw = action.raw if len(action.raw) <= 60 else action.raw[:57] + "..."
            lines.append(f"  {index}. {raw} -> {state}")
        for index in range(len(step_states) + 1, len(actions) + 1):
            raw = actions[index - 1].raw
            raw = raw if len(raw) <= 60 else raw[:57] + "..."
            lines.append(f"  {index}. {raw} -> not-run")
        return "\n".join(lines)

    # ------------------------------------------------------------------- run

    def run_script(
        self,
        steps: list[Action],
        *,
        window: str | None = None,
    ) -> str:
        """Execute a parsed ``run`` script and return SUI-Text.

        There are two executors, chosen by the script itself, because the two
        kinds of script can make different promises:

        * **No AWAIT** - nothing in the script depends on an earlier step's
          effect, so the batch path is exactly right: every step is resolved
          and authorised before the first one runs, then one settle, one delta
          and one rev. That guarantee is kept, not weakened;
        * **With AWAIT** - a later step names an element that only exists after
          an earlier step settled (a menu item, a dialog button). The script is
          therefore resolved *step by step, at execution time*, each step
          settling before the next; the first failure stops it and reports the
          steps that ran plus their net delta, exactly like ``BATCH PARTIAL``.

        ``window`` focuses that window before the first step, so the whole flow
        is addressable without a separate ``CALL wN.focus()`` round trip.

        Deliberately **no confirmation gate** - the same rule every other CALL
        follows in this build: a critical (``!``) capability executes directly,
        because stopping a one-call flow to ask for consent would put a second
        model turn back into exactly the flow ``run`` exists to collapse.
        """
        if not steps:
            raise SomnusError(ErrorCode.PARSE_ERROR, message="empty script")
        if window:
            self.focus_window(window)
        self._check_it_bindings(steps)
        if not any(step.kind == "await" for step in steps):
            outcome = self.call_batch(steps)
            # Same body, same step lines, same delta; only the word changes so
            # the reply names the tool the caller actually used.
            return (
                "RUN " + outcome.text[6:]
                if outcome.text.startswith("BATCH ")
                else outcome.text
            )
        return self._run_awaiting(steps)

    @staticmethod
    def _check_it_bindings(steps: list[Action]) -> None:
        """Refuse ``$it`` before the ``AWAIT`` that binds it.

        Checked for the whole script up front: the misordering is a property of
        the text, so it costs nothing to refuse before any step moves the UI.
        """
        bound = False
        for index, step in enumerate(steps, start=1):
            if step.kind == "await":
                bound = True
                continue
            if step.ref == "$it" and not bound:
                raise SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    ref="$it",
                    message=(
                        f"step {index}: $it is not bound - an AWAIT must run "
                        "before a step can name it"
                    ),
                )

    def _run_awaiting(self, steps: list[Action]) -> str:
        """The step-by-step executor for a script that contains an AWAIT.

        Every step goes through the session's ``call`` path, so it keeps every
        other authorisation - the process allow-list, the route policy, the
        capability resolution - and only the confirmation gate is (deliberately)
        absent, as it is for every CALL in this build.
        """
        if self.result is None:
            self._recompile()
        assert self.result is not None
        #: The script's whole delta is one tree comparison at the end, the same
        #: way a batch computes it - the steps in between are not observations.
        before_index = index_by_fingerprint(self.result.root)
        bound: str | None = None
        step_states: list[str] = []
        error_line: str | None = None
        failed_at = 0
        settled = True
        notes: list[str] = []
        for index, step in enumerate(steps, start=1):
            if step.kind == "await":
                try:
                    hit = self.await_element(step)
                except KeyError:
                    # The window vanished mid-wait (a modal may have replaced
                    # it); a wait is a read, so report it rather than crash.
                    step_states.append(f"ERR {ErrorCode.TARGET_NOT_FOUND}")
                    error_line = SomnusError(
                        ErrorCode.TARGET_NOT_FOUND,
                        ref="window",
                        hint="observe",
                        message="the target window closed while waiting",
                    ).render()
                    failed_at = index
                    break
                except SomnusError as err:
                    step_states.append(f"ERR {err.code}")
                    error_line = err.render()
                    failed_at = index
                    break
                bound = hit.ref
                step_states.append(f"ok ($it={bound})")
                continue
            if step.capability == "focus" and self._window_by_ref(step.ref or ""):
                # Focusing mid-script re-allocates refs and discards `$it`;
                # the tool's `window` argument is the supported way.
                step_states.append(f"ERR {ErrorCode.INVALID_ARGUMENT}")
                error_line = SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    ref=step.ref,
                    message=(
                        f"step {index}: CALL wN.focus() cannot be a script step - "
                        "switching windows re-allocates refs; pass window= instead"
                    ),
                ).render()
                failed_at = index
                break
            action = replace(step, ref=bound) if step.ref == "$it" else step
            try:
                outcome = self.call(action)
            except SomnusError as err:
                step_states.append(f"ERR {err.code}")
                error_line = err.render()
                failed_at = index
                break
            settled = outcome.settled
            step_states.append("ok")
            # A write echo and a window appearing are not part of the tree delta
            # (and each step's own reply has already advanced the window
            # baseline), so they are carried over here rather than lost.
            for line in outcome.text.splitlines():
                if line.startswith(("+ WINDOW", "- WINDOW", "~ WINDOW")):
                    if line not in notes:
                        notes.append(line)
                elif line.startswith("~ ") and " text=" in line and line not in notes:
                    notes.append(line)
        after_index = index_by_fingerprint(self.result.root)
        delta = diff_trees(before_index, after_index)

        performed = sum(1 for state in step_states if state.startswith("ok"))
        settled_tag = "" if settled else " settled=false"
        if error_line:
            head = (
                f"RUN PARTIAL steps={performed}/{len(steps)} "
                f"failed-at={failed_at} rev={self.rev}{settled_tag}"
            )
        else:
            head = (
                f"RUN OK steps={performed}/{len(steps)} rev={self.rev}{settled_tag}"
            )
        text = head + "\n" + self._step_lines(steps, step_states)
        if error_line:
            text += f"\n{error_line}"
            text += (
                f"\n~ steps 1-{performed} really ran; fix the failing step and "
                "continue with a fresh RUN"
                if performed
                else "\n~ the failing step ran first; nothing else was sent"
            )
        if notes:
            text += "\n" + "\n".join(notes)
        body = self.delta_text(delta)
        if body:
            text += f"\n{body}"
        return text

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
                if action.capability == "set_text":
                    combo = self._combo_text_holders(action.ref or "")
                    if combo:
                        err.message = (
                            f"{action.ref} has no set_text (not a text field); "
                            f"try {combo[0]}"
                        )
                        raise
                holders = self._capability_holders(action.capability)
                if holders:
                    err.message = (
                        f"{action.ref} has no {action.capability}; "
                        f"try one of: {', '.join(holders)}"
                    )
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

    # ------------------------------------------------------------------ sessions

    def open_child_session(
        self,
        *,
        visible: bool = False,
        auto_logon: bool = True,
        start_peer: bool = True,
        timeout: float = 120.0,
    ) -> str:
        """Open the child session and move this agent into it.

        One call does the whole sequence a person would otherwise perform by
        hand, which is what lets the model do it on request:

        1. start the RDP loopback (with the stored credential when
           ``auto_logon``, so no one has to type at the window);
        2. wait for Windows to create the session;
        3. start the peer inside it and wait for the pipe;
        4. switch this session over to it.

        Failure at any step is reported as it is, with the step named, rather
        than as one opaque "could not open a session".
        """
        from somnus.session import childsession

        if childsession.inside_child_session():
            return self._err(
                "PERMISSION_DENIED",
                "this agent is already running inside the child session",
                hint="report-user",
            )

        # A session is "already open" only when Windows really has one with a
        # desktop. The raw id is a hint that survives a refused logon and a
        # reset, so trusting it made this report "exists but has no peer" for a
        # session that was not there at all.
        existing = childsession.live_child_session()
        if existing is not None:
            # Already open: attach, which is the common case when the window was
            # kept from last time.
            from somnus.session.peerprovider import make_peer_provider

            provider = make_peer_provider(session_id=existing.session_id)
            if provider.connect():
                self._swap_provider(provider)
                self._session_label = "child"
                windows = self.refresh_windows()
                return (
                    f"OK session=child id={existing.session_id} "
                    f"windows={len(windows)} (attached to the session already open)\n"
                    + self._render_window_list()
                )
            # The session is real but its peer is not up yet. That is normal for
            # a second or two after logon (the Run key starts it), so wait
            # rather than declaring failure.
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline and not provider.connect():
                time.sleep(0.5)
            if provider.connected:
                self._swap_provider(provider)
                self._session_label = "child"
                windows = self.refresh_windows()
                return (
                    f"OK session=child id={existing.session_id} "
                    f"windows={len(windows)} (peer came up)\n"
                    + self._render_window_list()
                )
            return self._err(
                "PERMISSION_DENIED",
                f"child session {existing.session_id} is open and has a signed-in "
                "user, but no peer is answering on it; start one inside that "
                "session with `somnus child-session peer`, or register it with "
                "`somnus child-session peer-setup`",
                hint="report-user",
            )

        # Nothing open yet: create it.
        from somnus.session.launch import ChildSessionRun, HostMissing

        run = ChildSessionRun(
            visible=visible,
            auto_logon=auto_logon,
            connect_timeout=timeout,
        )
        try:
            run.start()
        except HostMissing as err:
            return self._err("NATIVE_EXECUTION_FAILED", str(err), hint="report-user")
        except RuntimeError as err:
            detail = "; ".join(run.output()[-3:]) or str(err)
            return self._err(
                "PERMISSION_DENIED",
                f"could not open the child session: {err} ({detail})",
                hint="report-user",
            )

        session_id = run.child_session_id
        if session_id is None:
            # No auto-logon: the window is waiting for a person. Leaving the
            # helper running is right - the model can try again once they have.
            run.detach()
            return self._err(
                "PERMISSION_DENIED",
                "the child session is waiting for a sign-in at the RDP window; "
                "store the password with `somnus child-session credential "
                "--save` to make this automatic",
                hint="ask-user",
            )

        # Peer: run it in the child session. It is started through the Run key
        # when that is registered; otherwise the caller must start it there.
        from somnus.session import peerstart

        if start_peer and peerstart.status() is None:
            run.detach()
            return self._err(
                "PERMISSION_DENIED",
                f"child session {session_id} is open, but no peer is registered "
                "to start with it; run `somnus child-session peer-setup` once "
                "(or start `somnus child-session peer` inside that session)",
                hint="report-user",
            )

        from somnus.session.peerprovider import make_peer_provider

        provider = make_peer_provider(session_id=session_id)
        # The peer starts with the session's own logon, so it can lag the
        # session by a moment; waiting here is what makes the switch reliable.
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline and not provider.connect():
            time.sleep(0.5)
        if not provider.connected:
            run.detach()
            return self._err(
                "PERMISSION_DENIED",
                f"child session {session_id} is open but its peer did not "
                "answer within 60s",
                hint="report-user",
            )

        from somnus.session import lifecycle

        run.detach()  # release the local helper handle; the agent now owns it
        # The agent's session keeps the responsibility, not the short-lived run
        # object: this process may make many tool calls against the session, and
        # it must be cleaned up when the process ends - unless the caller
        # explicitly leaves it running (see detach_child_session).
        lifecycle.claim(session_id)
        self._child_session_id = session_id
        self._swap_provider(provider)
        self._session_label = "child"
        windows = self.refresh_windows()
        return (
            f"OK session=child id={session_id} windows={len(windows)}\n"
            + self._render_window_list()
        )

    def detach_child_session(self) -> str:
        """Leave the child session running when this process exits.

        The default is the opposite - the session is cleaned up at exit, because
        an agent that is gone has no one to drive it. This makes the other
        choice explicit, so a session can be parked and picked up by a later
        run.
        """
        from somnus.session import lifecycle

        session_id = getattr(self, "_child_session_id", None)
        if session_id is None:
            live = childsession.live_child_session()
            session_id = live.session_id if live else None
        lifecycle.disown(session_id)
        if self.session_label == "child":
            self.switch_session("main")
        return f"OK child session {session_id} will stay running after exit"

    def close_child_session(self) -> str:
        """End the child session and return this agent to the main desktop.

        Closing the RDP connection is what ends the session - the session exists
        only as long as its connection does - so the helper process is stopped,
        not just disconnected.
        """
        from somnus.session import childsession, lifecycle

        if self.session_label == "child":
            self.switch_session("main")

        before = childsession.live_child_session()
        report = lifecycle.cleanup()

        # Confirm with the same check the rest of the code trusts, rather than
        # with the raw id (which keeps naming a session after it is gone).
        after = childsession.live_child_session()
        if after is not None:
            return self._err(
                "NATIVE_EXECUTION_FAILED",
                f"child session {after.session_id} is still present after "
                f"cleanup ({report})",
                hint="report-user",
            )
        if before is None:
            return f"OK no child session was open ({report})"
        return f"OK closed child session {before.session_id} ({report})"

    @staticmethod
    def _err(code: str, message: str, *, hint: str = "report-user") -> str:
        """One failure line in the same shape the rest of the runtime emits."""
        return f'ERR {code} hint={hint} msg="{message}"'

    def switch_session(self, target: str, *, peer_factory=None) -> str:
        """Retarget this session at another Windows session.

        The agent has one *current* session and one set of refs; switching
        means pointing both at the other desktop, never driving two at once.
        Two sessions at once would make every bare ref ambiguous, and the ref
        namespace is what the whole tool surface is built on, so the model
        cannot be asked to disambiguate on every call.

        What has to be reset, and why:

        * the provider - that is where the windows live;
        * the registry and the window-ref maps - a ``w2`` that named a window
          on the old desktop must not silently resolve to a different window on
          the new one;
        * ``target_window`` and the compiled result - they describe the old
          desktop.

        The compiler, projection, resolver, policy and budget are untouched:
        they are the agent's *behaviour*, not its location, and the whole point
        of this design is that behaviour does not depend on which session is in
        front of it.

        ``target`` is ``"main"``, ``"child"``, or ``"other"`` (an alias for
        ``child`` that reads better when the caller does not know which
        session is which).
        """
        from somnus.session import childsession

        target = (target or "").strip().lower()
        want_child = target in ("child", "other", "remote", "sub")
        if not want_child and target not in ("main", "local", "host", "primary"):
            return (
                f'ERR INVALID_ARGUMENT hint=inspect msg="unknown session '
                f'{target!r}; use \'main\' or \'child\'; still on '
                f'{self.session_label}"'
            )

        if childsession.inside_child_session():
            # This process already runs inside the child session. There is no
            # second desktop to reach, so asking for child is a no-op and
            # asking for main is an honest refusal. Either way the refs it
            # already holds keep naming the same desktop.
            if want_child:
                self._session_label = "child"
                return "OK session=child (already here)"
            return (
                "ERR PERMISSION_DENIED hint=report-user msg=\"this agent is "
                "running inside the child session, where the main desktop is "
                "not reachable; still on child\""
            )

        if want_child:
            if self.session_label == "child":
                # Already driving the child desktop: connecting a second peer
                # would fail for a reason that is not about the desktop. Do not
                # rebuild anything - the refs in hand stay valid.
                return "OK session=child (already here)"
            if peer_factory is None:
                from somnus.session.peerprovider import make_peer_provider

                peer_factory = make_peer_provider
            provider = peer_factory()
            if not provider.connect():
                try:
                    provider.close()
                except Exception:  # noqa: BLE001 - teardown
                    pass
                child_id = childsession.child_session_id()
                detail = (
                    f"child session {child_id}"
                    if child_id is not None
                    else "no child session is open"
                )
                return (
                    f"ERR PERMISSION_DENIED hint=report-user msg=\"cannot reach "
                    f"{detail}: start it with `somnus child-session start`, and "
                    f"run the peer inside it with `somnus child-session peer` "
                    f"(or register it with `somnus child-session peer-setup`); "
                    f"still on {self.session_label}\""
                )
            try:
                listed = provider.list_windows()
            except Exception as err:  # noqa: BLE001 - keep the old desktop intact
                provider.close()
                return SomnusError(
                    ErrorCode.NATIVE_EXECUTION_FAILED,
                    message=(
                        f"could not read the child desktop ({err}); still on "
                        f"{self.session_label}"
                    ),
                ).render()
            self._swap_provider(provider)
            self._session_label = "child"
            self._adopt_windows(listed)
            self._refs_need_observe = True
            return (
                f"OK session=child windows={len(self.windows)}\n"
                "desktop changed; observe again"
            )

        # Back to this desktop: rebuild the local provider rather than keeping
        # a second one alive, so there is only ever one provider holding UIA
        # state.
        if self.session_label == "main":
            return "OK session=main (already here)"
        if getattr(self, "_local_provider_factory", None) is None:
            return (
                "ERR ACTION_NOT_SUPPORTED hint=report-user msg=\"this session "
                "has no local desktop to return to; still on child\""
            )
        provider = self._local_provider_factory()
        try:
            listed = provider.list_windows()
        except Exception as err:  # noqa: BLE001 - keep the child desktop intact
            provider.close()
            return SomnusError(
                ErrorCode.NATIVE_EXECUTION_FAILED,
                message=(
                    f"could not read the local desktop ({err}); still on "
                    f"{self.session_label}"
                ),
            ).render()
        self._swap_provider(provider)
        self._session_label = "main"
        self._adopt_windows(listed)
        self._refs_need_observe = True
        return (
            f"OK session=main windows={len(self.windows)}\n"
            "desktop changed; observe again"
        )

    def _swap_provider(self, provider) -> None:
        """Replace the provider and drop every ref that pointed at the old one."""
        old = getattr(self, "provider", None)
        if old is not None and old is not provider:
            try:
                old.close()
            except Exception:  # noqa: BLE001 - the swap must not fail on teardown
                pass

        self.provider = provider
        # The new provider may not have the same settle guarantees as the old
        # one (e.g. switching from a replay to the child session's peer), so the
        # flag is derived from the provider that is actually in use.
        self._settle_deterministic = bool(
            getattr(provider, "is_deterministic", False)
        )
        self.registry = SessionRegistry()
        self.compiler = Compiler(allocator=self.registry.allocator)
        self.windows = []
        self.target_window = None
        self.result = None
        self._prev_index = {}
        self._prev_windows = {}
        self._window_refs = {}
        self._window_refs_by_id = {}
        self._menu_owner = {}
        self._route_memory = {}
        self._ambiguous_refs = set()
        self._last_signature = None
        # The raw tree describes the desktop we just left; a probe against it
        # on the new desktop would compare two unrelated windows.
        self._last_raw = None
        self._last_raw_window = None
        self._last_raw_sig = None
        self._last_raw_map = {}
        self._viewport_limited = False
        self._viewport_note = ""
        self._watch_natives = []
        self._last_capture_nodes = None
        self._last_capture_truncated = False
        self._small_observe_window = None
        self._last_capture_window = None
        # The cheap desktop signature described the old desktop; it must not
        # answer "unchanged" for the new one.
        self._top_level_sig = None
        self._prev_structured = None
        self._refs_need_observe = False
        self.rev = 0

    @property
    def session_label(self) -> str:
        """Which desktop the refs currently name: ``main`` or ``child``."""
        return getattr(self, "_session_label", "main")

    @property
    def desktop(self) -> str:
        """The desktop the current refs name (alias of ``session_label``)."""
        return self.session_label

    def close(self) -> None:
        """Release the provider.

        Deliberately does *not* clean up the child session. A CLI command builds
        a session, does one thing and closes it, so cleaning up here would end a
        running child session on every unrelated command (``somnus windows``,
        ``somnus observe``) that merely happened to build a session object.

        The lifetime rule is the one :mod:`somnus.session.lifecycle` states:
        cleanup happens at *process exit* for sessions the process started, or
        explicitly through ``switch(close)``.
        """
        try:
            self.trace_sink.close()
        finally:
            self.provider.close()

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, text: str) -> str:
        """Parse and execute a single SUI-Text command, returning SUI-Text."""
        try:
            action = parse_action(text)
        except SomnusError as err:
            return err.render()
        return self.run(action)

    def dispatch_lines(self, block: str) -> str:
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
            out.append(self.dispatch(line))
        return "\n".join(out)

    def run(self, action: Action) -> str:
        try:
            if action.kind == "observe":
                # ``OBSERVE w5 full`` - the window's whole tree, no folding.
                # ``OBSERVE w5 expand g3`` - unfold one collapsed node this
                # round, everything else stays folded (tokens stay bounded
                # while still revealing exactly what the agent asked for).
                #
                # The ref is handed over *unresolved*. Observe recompiles into
                # brand-new SemanticNode objects, so an id() resolved against
                # the old result can never match the new tree - the expansion
                # silently did nothing (a stated promise in the projection's
                # "+more ... OBSERVE w1 expand l1" marker).
                expanded: set[int] | set[str] | None = None
                full = action.scope == "full"
                if action.depth is not None and action.depth and action.query:
                    expanded = {action.query}
                return self.observe(
                    action.window,
                    windows_only=(action.query == "windows"),
                    include_other_windows=(action.scope == "desktop"),
                    full=full,
                    expanded=expanded,
                ).text
            if action.kind == "find":
                return self.find(action)
            if action.kind == "inspect":
                return self.inspect(action).text
            if action.kind == "call":
                return self.call(action).text
            if action.kind == "switch":
                return self.switch_session(action.query or "main")
            if action.kind == "session":
                if action.query == "close":
                    return self.close_child_session()
                if action.query == "detach":
                    return self.detach_child_session()
                return self.open_child_session()
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


def _trace_target_states(result: "CompileResult | None", ref: str | None) -> list[str]:
    """The target's states as the trace records them (empty when unresolvable)."""
    node = result.find(ref) if result is not None and ref else None
    return sorted(node.states) if node is not None else []


def _native_key(native) -> tuple:
    """A stable identity key for a native element, independent of profile.

    ``NativeRef`` equality includes fields a narrower cache profile does not
    request (the element handle), so comparing full refs would make every probe
    node look different from the snapshot node it is the same element as.
    Runtime id is the element's own identity; the id triple is the fallback for
    providers/trees without one.
    """
    if native.runtime_id:
        return ("rid", tuple(native.runtime_id))
    return (
        "ids",
        native.automation_id,
        native.class_name,
        native.control_type,
        tuple(native.path),
    )


def _watch_signature(tree: RawTree, watch: list) -> str | None:
    """A signature over ``watch`` only; None when any watched node is absent.

    Per-node *shallow* signatures (role/name/value/child-count/states) are enough
    because every node inside a visible row is itself watched: a deep
    disabled->enabled flip is an entry in this list, not a descendant that could
    be overlooked. ``None`` is the hard boundary - "cannot see it" is never
    reported as "unchanged".
    """
    if not watch:
        return None
    index = {_native_key(node.native): node for node in tree.walk()}
    parts = []
    for key in watch:
        node = index.get(key)
        if node is None:
            return None
        parts.append(_shallow_signature(node))
    return "\x1f".join(parts)


def _unique_natives(result: CompileResult) -> list:
    """Identity keys of every node a compilation covers, in a stable order."""
    out = []
    seen = set()
    for source in result.sources.values():
        key = _native_key(source.native)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _shallow_signature(node) -> str:
    """One node's own signature fields (no recursion into children).

    The child count is the node's *total* when it reports one: a collection
    whose children are deliberately truncated (12 visible of 240) must compare
    equal to the same container in a full tree. A row's own child list is
    complete in both frames, so real child changes still show up.
    """
    total = node.virtual_count if (node.virtual_count or 0) > 0 else len(node.children)
    return "|".join(
        (
            node.role,
            node.name or "",
            node.value or "",
            str(total),
            ",".join(sorted(node.states)),
        )
    )


def _ref_path(root: SemanticNode, ref: str) -> list[SemanticNode]:
    """The node named ``ref`` plus its ancestors, root first; [] when absent."""
    if root is None:
        return []

    def walk(node: SemanticNode, trail: list[SemanticNode]) -> list[SemanticNode] | None:
        trail = trail + [node]
        if node.ref == ref:
            return trail
        for child in node.children:
            found = walk(child, trail)
            if found is not None:
                return found
        return None

    return walk(root, []) or []


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
        lines.append("  " * indent + render_node_line(display_node(child), opts))
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
    return f'"{escape_text(text)}"'


def _write_echo(ref: str, value: Any) -> str:
    """The `~ ref text=...` line, with the node-string limit applied.

    A write echo confirms what landed. It uses the same 80-character limit as
    a node line, so a 100 KB write cannot double the reply by carrying both the
    delta value and the echo in full; `len=` makes the truncation explicit.
    """
    text = str(value)
    if len(text) <= 80:
        return f"~ {ref} text={_q(text)}"
    return f"~ {ref} text={_q(text[:79] + '…')} len={len(text)}"


def _esc(text: str) -> str:
    return escape_text(text)
