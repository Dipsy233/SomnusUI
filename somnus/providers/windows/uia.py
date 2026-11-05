"""Windows UIAutomation provider.

Design notes:

* All UIA work runs on a dedicated MTA thread. COM interfaces are *never*
  touched from the caller's thread - cross-apartment marshalling of UIA objects
  is both slow and deadlock prone.
* Snapshots use ``IUIAutomationCacheRequest`` and ``BuildUpdatedCache`` so that
  one COM round trip per node is replaced by one per *batch*.
* ``Invoke`` (and any pattern that can block on a modal dialog) is executed on
  the worker thread with a hard timeout; on timeout we return
  ``undetermined=True`` so the runtime settles and re-observes instead of
  hanging.
* ControlView (not RawView) is used, and depth is clamped.

The module imports ``comtypes`` lazily. Importing this module on a non-Windows
host is safe; constructing :class:`UiaProvider` will raise ``RuntimeError``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.base import NullSubscription
from somnus.providers.windows.cache import (
    SIGNATURE_PATTERNS,
    build_cache_request,
    cache_tier_from_env,
)
from somnus.providers.windows.events import EventCounter
from somnus.providers.windows.executor import PATTERN_FOR_PRIMITIVE, run_primitive
from somnus.providers.windows.workspace import Workspace, raise_window

log = logging.getLogger("somnus.uia")

#: Set SOMNUS_TRACE_STATES=1 to log how each node's `enabled` was decided.
#: Used to debug self-drawn (Chromium/Electron) controls whose UIA `IsEnabled`
#: can disagree with what is rendered - e.g. QQ's 发送 button.
_TRACE_STATES = os.environ.get("SOMNUS_TRACE_STATES", "") not in ("", "0", "false")

#: Set SOMNUS_PROFILE=1 to collect per-snapshot timings (``provider.timing``):
#: the root ``BuildUpdatedCache``, the whole node walk and the taskbar tray
#: re-read. That split is the one measurement needed to know whether narrowing
#: the cache request can matter at all.
_PROFILE = os.environ.get("SOMNUS_PROFILE", "") not in ("", "0", "false")

#: Patterns the signature probe asks the cache for (see cache.py). Probing a
#: pattern that was not requested raises per node, so the probe must iterate
#: exactly this set instead of every known pattern id.
_SIG_PATTERN_NAMES = frozenset(SIGNATURE_PATTERNS.values())

# ---------------------------------------------------------------------------
# UIA constants we need (avoids importing the generated comtypes modules at
# import time so this file is importable everywhere).
# ---------------------------------------------------------------------------

UIA_CONTROL_TYPES = {
    50000: "button",
    50001: "calendar",
    50002: "checkbox",
    50003: "combobox",
    50004: "edit",
    50005: "hyperlink",
    50006: "image",
    50007: "listitem",
    50008: "list",
    50009: "menu",
    50010: "menubar",
    50011: "menuitem",
    50012: "progressbar",
    50013: "radiobutton",
    50014: "scrollbar",
    50015: "slider",
    50016: "spinner",
    50017: "statusbar",
    50018: "tab",
    50019: "tabitem",
    50020: "text",
    50021: "toolbar",
    50022: "tooltip",
    50023: "tree",
    50024: "treeitem",
    50025: "custom",
    50026: "group",
    50027: "thumb",
    50028: "datagrid",
    50029: "dataitem",
    50030: "document",
    50031: "splitbutton",
    50032: "window",
    50033: "pane",
    50034: "header",
    50035: "headeritem",
    50036: "table",
    50037: "titlebar",
    50038: "separator",
    50039: "semanticzoom",
    50040: "appbar",
}

#: Element property ids.
_P_AUTOMATION_ID = 30011
_P_CLASS = 30012
_P_NAME = 30005
_P_CONTROL_TYPE = 30003
_P_VALUE_VALUE = 30045
_P_IS_OFFSCREEN = 30022
_P_IS_ENABLED = 30010
_P_IS_FOCUSED = 30008
_P_IS_SELECTED = 30034
_P_TOGGLE_STATE = 30042
_P_EXPAND_COLLAPSE = 30053
_P_IS_READONLY = 30046
_P_DESCRIPTION = 30004
#: The element's own Win32 HWND (0 when it maps none).
_P_NATIVE_WINDOW_HANDLE = 30020

_PATTERN_IDS = {
    "invoke": 10000,
    "selection": 10001,
    "value": 10002,
    "rangevalue": 10003,
    "scroll": 10004,
    "expandcollapse": 10005,
    "griditem": 10007,
    "grid": 10006,
    "scrollitem": 10016,
    "text": 10014,
    "toggle": 10015,
    "selectionitem": 10010,
    "legacyiaccessible": 10018,
    "window": 10017,
    "table": 10012,
    "tableitem": 10011,
}

_TOGGLE_OFF = 0
_TOGGLE_ON = 1
_TOGGLE_INDETERMINATE = 2
_EXPAND_OFF = 0
_EXPAND_ON = 1
_EXPAND_PARTIAL = 2

#: UIA's ComboBox control type. Its current selection is readable through the
#: container's SelectionPattern (see ``_current_selection_label``).
_CT_COMBOBOX = 50003

#: Element property ids needed by the viewport capture.
_P_BOUNDING_RECT = 30001

#: Roles whose ``children`` are rows of data rather than structure: these are
#: captured row-wise (narrow peek, then full read of the visible rows only).
_COLLECTION_ROLES = frozenset({"list", "tree", "table", "datagrid", "grid"})

#: Roles that are a row of a collection. Used to recognise an unlabelled
#: container (a ``pane`` holding 240 list items) by its composition.
_ROW_ROLES = frozenset({"listitem", "treeitem", "dataitem", "row", "griditem"})

#: How deep the structural skeleton is read before a non-collection node is
#: taken as-is. The visible/structural part of a normal window is 2-3 levels.
_VIEWPORT_MAX_DEPTH = 4

#: A first capture whose whole tree is this small is re-walked deeper in the
#: same request. UWP applications wrap every actionable control in 3-4 levels of
#: landing-target/NavView/group containers: the depth-4 skeleton reaches the
#: groups but not their buttons (a calculator at 38 nodes rendered only the
#: title bar). The re-walk is bounded to small *and untruncated* trees, so a
#: long list keeps the cheap viewport walk and never pays a deeper read.
_VIEWPORT_DEEP_NODES = 120
_VIEWPORT_DEEP_MAX_DEPTH = 10
#: A node with at least this many same-typed row children is treated as a
#: collection even when its role does not say so.
_VIEWPORT_MIN_ROWS = 8
#: Cap on how many visible rows are read in full in one capture. A viewport
#: holds far fewer than this; the cap only guards an unusually tall window.
_VIEWPORT_MAX_VISIBLE_ROWS = 64
#: How many rows an explicitly expanded container reads (the visible window
#: plus the rows right after it, so the agent gets a page rather than a fold).
_VIEWPORT_PAGE_ROWS = 30


class UiaProvider:
    """AccessibilityProvider backed by UIAutomationCore via comtypes."""

    name = "uia"

    def __init__(
        self,
        max_depth: int = 20,
        allow_list: list[str] | None = None,
        cache_tier: str | None = None,
        **_: Any,
    ) -> None:
        if not sys.platform.startswith("win"):
            raise RuntimeError("UiaProvider is only available on Windows")
        self.max_depth = max_depth
        self.allow_list = [p.lower() for p in (allow_list or [])]
        #: Which cache-request tier full snapshots use (see cache.py). Default
        #: ``full`` keeps the previous behaviour; the investigation switches it
        #: with ``SOMNUS_CACHE_TIER`` or the ``cache_tier`` argument.
        self.cache_tier = cache_tier or cache_tier_from_env()
        #: Viewport metadata from the most recent ``snapshot_viewport``, keyed
        #: by window id then by the container's native path. The session copies
        #: these onto the compiled node's attributes; ``RawNode`` has no field
        #: for them (and the raw layer should not grow one for provider hints).
        self.viewport_meta: dict[str, dict[tuple[int, ...], dict]] = {}
        #: Whether the last ``snapshot_viewport`` returned the viewport-scoped
        #: tree (True) or fell back to a full read (False). The session uses it
        #: to decide whether the result may stand in for settle's reuse frame.
        self.last_viewport_partial: bool = False
        #: Per-snapshot segment timings, only populated while SOMNUS_PROFILE is
        #: on (see module attribute ``_PROFILE``).
        self.timing: dict[str, float] | None = {} if _PROFILE else None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")
        # Initialise COM in MTA on the worker thread *before* any comtypes call
        # can auto-initialise it in STA.
        self._executor.submit(self._init_com).result(timeout=10)
        self._windows: dict[str, Any] = {}
        #: (pid, title) -> "wN", so window ids persist across enumerations.
        self._window_id_by_key: dict[tuple[int, str], str] = {}
        self._lock = threading.Lock()
        #: runtime_id -> live UIA element, refreshed on every snapshot.
        self._element_cache: dict[tuple[int, ...], Any] = {}
        #: Windows this provider made visible/moved for background operation.
        #: `close()` restores each one, so agent work never leaves the user's
        #: desktop rearranged.
        self._workspace = Workspace()
        #: The window id whose window is currently expected to be in the
        #: foreground, so a run of snapshot/execute calls on one window raises
        #: it once instead of on every call. Cleared whenever another window
        #: (or the user) is found in front of it.
        self._foreground_window_id: str | None = None
        #: Why the last raise failed, or None when it succeeded. A raise that
        #: the Win32 foreground lock refused is not fatal for UIA reads, but it
        #: is the difference between "this window has no controls" and "this
        #: window could not render" - the session turns it into a warning line
        #: instead of letting a covered self-drawn app look empty.
        self.last_foreground_error: str | None = None

    # -- COM lifecycle ------------------------------------------------------

    def _init_com(self) -> None:
        # The worker thread needs COM in MTA.
        #
        # comtypes initialises COM only for the thread that *imports* it first,
        # so a second provider constructed later - which is what switching
        # sessions does - gets a worker thread with no COM at all, and its first
        # call fails with "CoInitialize has not been called". The import is done
        # first (a no-op when cached) so the flag is set, then COM is
        # initialised explicitly on *this* thread.
        import sys as _sys

        # Must be set before the first import anywhere in the process, so an
        # existing STA import is not overridden here - it is only a default.
        if not hasattr(_sys, "coinit_flags"):
            _sys.coinit_flags = 0x0  # COINIT_MULTITHREADED

        import comtypes

        from comtypes import COINIT_MULTITHREADED

        # RPC_E_CHANGED_MODE means COM is already up on this thread in another
        # model, which is usable: the calls below do not require MTA, they
        # require COM to be initialised at all.
        try:
            comtypes.CoInitializeEx(COINIT_MULTITHREADED)
        except OSError as exc:
            if getattr(exc, "winerror", None) != -2147417850:
                raise

        from comtypes.client import GetModule

        # Generate the typelib wrappers for UIAutomationCore.
        self._uia_mod = GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        self._UIA = UIA
        self._auto = comtypes.client.CreateObject(
            UIA.CUIAutomation, interface=UIA.IUIAutomation
        )
        self._auto2 = None
        if hasattr(UIA, "IUIAutomation2"):
            try:
                self._auto2 = self._auto.QueryInterface(UIA.IUIAutomation2)
            except Exception:  # interface unavailable on older Windows
                self._auto2 = None
        self._cache = self._build_cache()
        #: The signature probe's cache: only the fields settle compares.
        self._sig_cache = build_cache_request(self._auto, self._UIA, tier="sig")
        #: One structural level (the element + its direct children). The
        #: viewport capture walks the skeleton this way instead of asking for a
        #: whole subtree: on the 1467-node store window a subtree read is ~2.9s
        #: and a single children level is ~19ms.
        self._children_cache = build_cache_request(self._auto, self._UIA, tier="full")
        #: ``Children`` alone does **not** cache the element's own properties
        #: (UIA scope semantics: it covers the children, not the element), so a
        #: node read from it has no name/type/state. The scope is
        #: ``Element | Children``: this element plus its direct children, and
        #: never the grandchildren - which is exactly the skeleton depth.
        self._children_cache.TreeScope = (
            self._UIA.TreeScope_Element | self._UIA.TreeScope_Children
        )
        #: The narrow row peek: name/type/enabled/rect, no patterns.
        self._peek_children_cache = build_cache_request(self._auto, self._UIA, tier="peek")
        self._peek_children_cache.TreeScope = (
            self._UIA.TreeScope_Element | self._UIA.TreeScope_Children
        )
        #: The element itself, with its full property/pattern set and no
        #: children (used for a collection container, whose own capabilities
        #: must stay complete while its rows are read selectively).
        self._element_cache_request = build_cache_request(self._auto, self._UIA, tier="full")
        self._element_cache_request.TreeScope = self._UIA.TreeScope_Element
        #: Layered probe caches: the signature/state/rect profile, one
        #: structural level, one element, and one subtree (for a visible row,
        #: whose deep states must be compared). Element|Children again, for the
        #: same scope reason as above.
        element_children = self._UIA.TreeScope_Element | self._UIA.TreeScope_Children
        self._probe_children_cache = build_cache_request(self._auto, self._UIA, tier="probe")
        self._probe_children_cache.TreeScope = element_children
        self._probe_element_cache = build_cache_request(self._auto, self._UIA, tier="probe")
        self._probe_element_cache.TreeScope = self._UIA.TreeScope_Element
        self._probe_subtree_cache = build_cache_request(self._auto, self._UIA, tier="probe")
        self._probe_subtree_cache.TreeScope = self._UIA.TreeScope_Subtree
        #: Rectangle-only peek for the probe's visibility scan.
        self._rect_peek_cache = build_cache_request(self._auto, self._UIA, tier="rect")
        self._rect_peek_cache.TreeScope = element_children

    def _build_cache(self):
        """Create a CacheRequest pulling all properties/patterns we consume."""
        return build_cache_request(self._auto, self._UIA, tier=self.cache_tier)

    def close(self) -> None:  # pragma: no cover - platform
        # Put every window the agent touched back the way the user had it
        # (re-minimize what was minimized) before the process goes away.
        if self._workspace is not None:
            self._workspace.release_all()
        self._executor.shutdown(wait=False)

    # -- window discovery ---------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        """Enumerate windows *without* raising any of them.

        Listing is how an agent decides where to go, and it is the one read that
        needs no window raised: every top-level window has a title, a process
        and an activation flag whether or not it is in front. Raising a window
        here would steal the user's focus just to answer "what is on my
        desktop?", so this path deliberately skips the foreground step (see
        :meth:`peek`).
        """
        return self._run(self._list_windows_sync)

    def peek(self, window_id: str) -> WindowInfo | None:
        """One window's metadata, with its window raised first.

        The runtime asks for the target window's title/flags on every
        observation. Looking it up through :meth:`list_windows` would not raise
        the window (see there), so the target would stay wherever it happens to
        be - behind another window, or minimized - and a self-drawing
        application would answer the following snapshot with an empty tree.
        """
        return self._run(lambda: self._peek_sync(window_id))

    def _peek_sync(self, window_id: str) -> WindowInfo | None:
        el = self._windows.get(window_id)
        if el is None:
            # Re-enumerating is what discovers a window the agent has not listed
            # yet (a pack may name one directly).
            next((w for w in self._list_windows_sync() if w.window_id == window_id), None)
            el = self._windows.get(window_id)
            if el is None:
                return None
        # Raise FIRST, then read the metadata. Reading it from the enumeration
        # instead would return None for the one case this exists to fix: a
        # minimized (or otherwise off-screen) window is filtered out of the
        # list, so a lookup through it finds nothing, reports "no such window",
        # and the window is never raised - leaving the following snapshot and
        # every execute against an empty shell.
        self._enter_foreground_sync(window_id, el)
        try:
            pid = int(el.CurrentProcessId)
            return WindowInfo(
                window_id=window_id,
                title=el.CurrentName or "",
                process=_process_name(pid),
                pid=pid,
                active=bool(el.CurrentHasKeyboardFocus),
                hwnd=_top_level_hwnd_of(el),
            )
        except Exception:
            # The window died between the raise and the read.
            return None

    def _list_windows_sync(self) -> list[WindowInfo]:
        """Enumerate top-level application windows, raising none of them.

        Called by :meth:`list_windows` (the peek path, which must not touch
        the foreground) and by the resolution paths below, which raise the
        window they resolved to themselves.

        The desktop is walked with ``ControlViewWalker`` rather than
        ``FindAll``: the walker view is what actually surfaces UWP/WinUI
        windows hosted by ``ApplicationFrameWindow`` (such as Settings), which a
        ControlType filter on the root's children misses. Candidate elements are
        then validated with the same Win32 style rules Windows uses to decide
        which windows belong on the taskbar, which drops the phantom nodes
        (notification icons, service hosts) UIA also exposes at the root.

        Missing UWP windows would make whole applications invisible to the agent
        (L0 always lists windows).
        """
        out: list[WindowInfo] = []
        seen_hwnds: set[int] = set()
        walker = self._auto.ControlViewWalker
        child = walker.GetFirstChildElement(self._auto.GetRootElement())
        while child is not None:
            nxt = None
            try:
                nxt = walker.GetNextSiblingElement(child)
            except Exception:
                pass
            try:
                ct = int(child.CurrentControlType)
                if ct in _TOPLEVEL_CONTROL_TYPES:
                    hwnd = int(child.CurrentNativeWindowHandle or 0)
                    if hwnd:
                        seen_hwnds.add(hwnd)
                    # Panes are kept when they are real visible top-level
                    # windows (taskbar, Electron/UWP shells) or recognised shell
                    # surfaces; Windows must pass the taskbar-visibility test.
                    # Without this, Chromium's offscreen helper windows
                    # ("Input Occlusion Window", "Backstop Window") show up as
                    # if they were applications.
                    if hwnd and (
                        _is_app_window(hwnd)
                        or (ct == _CT_PANE and _is_shell_surface(hwnd))
                    ):
                        self._add_window(child, out, hwnd=hwnd)
            except Exception:
                pass
            child = nxt

        # The walker can stop early on a dead element; Win32 enumeration fills
        # in anything it missed (and is how UWP frames are reached reliably).
        #
        # The same taskbar-visibility rules must hold here as in the walk above.
        # `_enum_top_level_windows` only filters invisible and child windows, so
        # without this check the fallback readmitted exactly the phantoms the
        # walk had just rejected - measured: NVIDIA's "GeForce Overlay" (a
        # WS_EX_NOACTIVATE overlay that gets no taskbar button, so no user can
        # select it) appeared in the window list purely through this path.
        #
        # A splash animation is the other case: Word registers each letter of
        # its startup logo as a separate top-level window ("T", "K", "D", ...
        # fourteen of them), and they stay in the list until something forces a
        # foreground change. They are tool/no-activate windows, so the same rule
        # drops them.
        for hwnd, pid, title in _enum_top_level_windows():
            if hwnd in seen_hwnds:
                continue
            if not _is_app_window(hwnd) and not _is_shell_surface(hwnd):
                continue
            try:
                el = self._auto.ElementFromHandle(hwnd)
            except Exception:
                continue
            self._add_window(el, out, hwnd=hwnd)

        # Mark the foreground window active.
        try:
            fg_pid = self._auto.GetFocusedElement().CurrentProcessId
            for w in out:
                if w.pid == fg_pid:
                    w.active = True
        except Exception:
            pass
        return out

    def _uwp_content_pid(self, frame_el: Any) -> int | None:
        """The real process behind a UWP ``ApplicationFrameWindow``.

        UWP apps run inside ``ApplicationFrameHost.exe``; the tree looks like::

            Window "Settings" pid=<host>   (ApplicationFrameHost.exe)
              Window "Settings" pid=<host>
              Window "Settings" pid=<real> (SystemSettings.exe)  <- different pid

        Walking the *immediate children* and taking the first with a different
        pid yields the real app process. A subtree search for a class name is
        less reliable because UWP content is built lazily.
        """
        try:
            walker = self._auto.ControlViewWalker
            host_pid = int(frame_el.CurrentProcessId)
            child = walker.GetFirstChildElement(frame_el)
            while child is not None:
                try:
                    cpid = int(child.CurrentProcessId)
                    if cpid and cpid != host_pid:
                        return cpid
                except Exception:
                    pass
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    break
        except Exception:
            pass
        return None

    def _add_window(
        self,
        el: Any,
        out: list[WindowInfo],
        *,
        hwnd: int = 0,
    ) -> None:
        """Validate and append one window element."""
        if el.CurrentIsOffscreen:
            return
        name = el.CurrentName or ""
        if not name:
            return
        if _is_shell_chrome(el):
            return
        pid = int(el.CurrentProcessId)
        proc = _process_name(pid)
        # A UWP app is hosted by ApplicationFrameHost.exe; the real application
        # (e.g. SystemSettings.exe) owns the content inside the frame. Report the
        # real process so packs match the app, not the host.
        if proc.lower() == "applicationframehost.exe":
            real_pid = self._uwp_content_pid(el)
            if real_pid:
                pid = real_pid
                proc = _process_name(pid)
        if self.allow_list and proc.lower() not in self.allow_list:
            return
        # The same native window must not be listed twice. Windows with a
        # handle are deduped by the handle (its element can be reached from
        # both the UIA walker and the Win32 enumeration); a window without one
        # falls back to (pid, title), the historical rule.
        #
        # (pid, title) alone is not enough to *identify* a window: two windows
        # of one process legitimately share a title (two Explorer windows on
        # the same folder, two "无标题 - 画图"), and collapsing them here made
        # one of the two unaddressable.
        for existing in out:
            if hwnd and existing.hwnd:
                if existing.hwnd == hwnd:
                    return
                continue
            if existing.pid == pid and existing.title == name and not existing.hwnd:
                return
        # Stable window ids: numbering by list position renumbers every window
        # after one closes, silently retargeting agents that hold old refs
        # (found driving Steam live: w3 flipped between Steam and QQ between
        # enumerations).
        #
        # The id is keyed by hwnd, NOT by (pid, title). A title is neither
        # unique nor stable: two windows of one app can share it (and then a
        # single id would be handed to both), and raising a window or letting
        # the app retitle it must not move its id. The hwnd is the one property
        # of a top-level window that survives enumeration-order changes - which
        # is exactly what raising a window for rendering can cause. A window
        # without a handle falls back to (pid, title), the old behaviour.
        key = (pid, name) if not hwnd else ("hwnd", hwnd)
        wid = self._window_id_by_key.get(key)
        if wid is None:
            used = set(self._window_id_by_key.values())
            n = len(used) + 1
            while f"w{n}" in used:
                n += 1
            wid = f"w{n}"
            self._window_id_by_key[key] = wid
        # Remember the title too, so a window whose handle the provider could
        # not read still resolves on a later enumeration by its (pid, title).
        if hwnd:
            self._window_id_by_key.setdefault((pid, name), wid)
        with self._lock:
            self._windows[wid] = el
        out.append(
            WindowInfo(
                window_id=wid,
                title=name,
                process=proc,
                pid=pid,
                active=bool(el.CurrentHasKeyboardFocus),
                hwnd=hwnd,
            )
        )

    def focus_window(self, window_id: str) -> ExecResult:
        """``CALL wN.focus()``: switch the target and raise its window."""
        el = self._windows.get(window_id)
        if el is None:
            return ExecResult(ok=False, error="TARGET_NOT_FOUND")
        return self._run(lambda: self._enter_foreground_sync(window_id, el))

    # -- snapshot -----------------------------------------------------------

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 20,
    ) -> RawTree:
        return self._run(lambda: self._snapshot_sync(window_id, scope, max_depth))

    def snapshot_signature(self, window_id: str) -> RawTree:
        """A cheap probe: the tree settle compares, built from a narrow cache.

        This is the same walk as :meth:`snapshot` with a cache request limited
        to the signature fields (role/name/value/child-count/states), so the
        settler can tell "nothing changed" without paying for a full snapshot.
        It deliberately does *not* raise the window (the caller already did
        that for the action) and does not populate the element cache.

        The result is only ever fed to ``settle.signature_of``; it is never
        compiled. Returning a ``RawTree`` (rather than a string computed here)
        is what guarantees the probe and the full snapshot are compared by the
        same function - a second signature implementation would drift.
        """
        return self._run(lambda: self._probe_sync(window_id))

    # -- viewport capture ---------------------------------------------------

    def snapshot_viewport(
        self,
        window_id: str,
        max_depth: int = _VIEWPORT_MAX_DEPTH,
        expanded_paths: set[tuple[int, ...]] | None = None,
    ) -> RawTree:
        """A viewport-scoped snapshot for ``observe``: structure + visible rows.

        The structural skeleton is read one level at a time (an
        ``Element | Children`` cache per element). A collection container is
        then *peeked* with a narrow request (name/type/enabled/rect) and only its
        rows whose rectangle intersects the window are read completely -
        properties, patterns, states and children all present. Everything else
        keeps the full read it always had.

        ``expanded_paths`` names containers the caller explicitly asked to open
        (``OBSERVE w1 expand l1``): those read a page of rows (visible ones plus
        the next few) instead of only the visible ones, which is what makes the
        "+more ... expand" marker do something.

        This is deliberately a separate entry point from :meth:`snapshot`:
        call/settle still take a whole-tree snapshot, so refs used by an action
        never depend on what happened to be on screen.

        Metadata for the truncated containers is left in
        :attr:`viewport_meta` (the session copies it onto node attributes; the
        field names are the contract shared with the projection work).
        """
        return self._run(
            lambda: self._snapshot_viewport_sync(window_id, max_depth, expanded_paths or set())
        )

    def _snapshot_viewport_sync(
        self,
        window_id: str,
        max_depth: int,
        expanded_paths: set[tuple[int, ...]],
    ) -> RawTree:
        el, win_info = self._element_for_window(window_id)
        self._enter_foreground_sync(window_id, el)
        self._current_window_id = window_id
        self.viewport_meta = {}
        self.last_viewport_partial = False
        # The window's own properties are not part of a Children-scope cache
        # (UIA caches the children, not the element), so the clipping rectangle
        # is read once, live: a single property read on the window element.
        window_rect = _parse_rect(_getattr(el, "CurrentBoundingRectangle", None))
        started = time.perf_counter() if self.timing is not None else 0.0
        try:
            self._element_cache.clear()
            root = self._viewport_node(el, 0, max_depth, (), window_rect, expanded_paths)
            self.last_viewport_partial = True
            # Small, untruncated window: the depth-4 skeleton is the title bar
            # plus the top of the content (UWP controls live two containers
            # deeper). Re-walk it deeper in the same call so a default observe
            # carries the actionable controls; a long/truncated list never gets
            # here, so its cheap viewport walk is unchanged.
            if self._viewport_should_deepen(root):
                self._element_cache.clear()
                self.viewport_meta = {}
                root = self._viewport_node(
                    el,
                    0,
                    _VIEWPORT_DEEP_MAX_DEPTH,
                    (),
                    window_rect,
                    expanded_paths,
                )
        except Exception as exc:  # noqa: BLE001 - fall back to the full read
            # A provider that cannot answer a shallow cache (or an element that
            # dies mid-capture) must not leave observe with a partial tree: the
            # honest fallback is exactly today's full snapshot.
            log.debug("viewport capture failed (%s); using the full snapshot", exc)
            self._current_window_id = None
            self.viewport_meta = {}
            self.last_viewport_partial = False
            return self._snapshot_sync(window_id, None, self.max_depth)
        finally:
            self._current_window_id = None
            if self.timing is not None:
                _add_timing(self.timing, "viewport_ms", started)
        return RawTree(window=win_info, root=root, raw_revision=0)

    def _viewport_should_deepen(self, root: RawNode) -> bool:
        """Whether the just-taken skeleton is small enough to read deeper.

        A truncated collection means the first walk already cut rows; reading
        deeper would multiply the expensive part (the rows) instead of just
        adding the two or three structural levels a UWP app hides its controls
        under. The node count is the cheap, already-in-hand evidence.
        """
        nodes = 0
        for node in root.walk():
            nodes += 1
            if node.truncated:
                return False
        if nodes > _VIEWPORT_DEEP_NODES:
            return False
        for containers in self.viewport_meta.values():
            for meta in containers.values():
                if meta.get("items_truncated"):
                    return False
        return True

    def _viewport_node(
        self,
        el: Any,
        depth: int,
        max_depth: int,
        path: tuple[int, ...],
        window_rect: tuple[float, float, float, float] | None,
        expanded_paths: set[tuple[int, ...]],
        probe: bool = False,
    ) -> RawNode:
        """One node, its structural children, and row-wise capture for collections.

        ``probe`` swaps the full profiles for the layered probe's (signature
        fields + states + rect): the probe tree is compared, never compiled, so
        it does not need capabilities.
        """
        patterns_only = _SIG_PATTERN_NAMES if probe else None
        if depth >= max_depth:
            # Leaf end of the skeleton: the element's own property set, no
            # children request at all.
            own = el.BuildUpdatedCache(
                self._probe_element_cache if probe else self._element_cache_request
            )
            return self._build_node(
                el, depth, depth, path, cached=own, patterns_only=patterns_only
            )

        cached = el.BuildUpdatedCache(
            self._probe_children_cache if probe else self._children_cache
        )
        stub = self._build_node(
            el, depth, depth + 1, path, cached=cached, patterns_only=patterns_only
        )
        if window_rect is None:
            window_rect = _cached_rect(cached)
        child_stubs = list(stub.children)
        # An unlabelled container holding many row-typed children is a
        # collection too (a `pane` wrapping a data grid, a self-drawn list).
        if stub.role not in _COLLECTION_ROLES and _looks_like_row_container(stub):
            return self._collection_node(
                el,
                depth,
                path,
                window_rect,
                expand=path in expanded_paths,
                probe=probe,
            )
        if not child_stubs:
            return stub
        stub.children = []
        for index, child_stub in enumerate(child_stubs):
            child_el = self._element_cache.get(child_stub.native.runtime_id)
            if child_el is None:
                stub.children.append(child_stub)
                continue
            child_path = path + (index,)
            if child_stub.role in _COLLECTION_ROLES:
                stub.children.append(
                    self._collection_node(
                        child_el,
                        depth + 1,
                        child_path,
                        window_rect,
                        expand=child_path in expanded_paths,
                        probe=probe,
                    )
                )
            else:
                stub.children.append(
                    self._viewport_node(
                        child_el,
                        depth + 1,
                        max_depth,
                        child_path,
                        window_rect,
                        expanded_paths,
                        probe=probe,
                    )
                )
        return stub

    def _collection_node(
        self,
        el: Any,
        depth: int,
        path: tuple[int, ...],
        window_rect: tuple[float, float, float, float] | None,
        *,
        expand: bool,
        probe: bool = False,
    ) -> RawNode:
        """A collection: complete own capabilities, complete only visible rows."""
        patterns_only = _SIG_PATTERN_NAMES if probe else None
        try:
            peeked = el.BuildUpdatedCache(
                self._rect_peek_cache if probe else self._peek_children_cache
            )
            rows = peeked.GetCachedChildren()
            total = _child_count(rows)
        except Exception as exc:  # noqa: BLE001 - fall back to the full read
            log.debug("row peek failed (%s); reading the container in full", exc)
            if probe and _TRACE_STATES:
                log.info("probe coverage lost: row peek failed (%s); full read", exc)
            return self._full_read_node(el, depth, path, probe=probe)
        # Without a window rectangle visibility cannot be judged at all; the
        # honest answer is the full read, not "nothing is visible".
        if window_rect is None:
            if probe and _TRACE_STATES:
                log.info("probe coverage lost: no window rectangle; full read")
            return self._full_read_node(el, depth, path, probe=probe)

        # Visibility is decided for every row (cheap: ~13us each on a cached
        # rect) and only the selected rows are read completely. An expanded
        # container takes the visible window plus the rows after it, up to a
        # page - that is what "+more ... expand" does.
        visibilities: list[bool] = []
        rect_unknown = 0
        for index in range(total):
            try:
                row_el = rows.GetElement(index)
            except Exception:
                visibilities.append(False)
                rect_unknown += 1
                continue
            rect = _cached_rect(row_el)
            if rect is None:
                visibilities.append(False)
                rect_unknown += 1
                continue
            visibilities.append(_rect_visible(rect, window_rect))
        first_visible = next(
            (i for i, visible in enumerate(visibilities) if visible), None
        )
        if expand:
            start = first_visible if first_visible is not None else 0
            end = min(total, start + _VIEWPORT_PAGE_ROWS)
            selected = list(range(start, end))
        else:
            selected = [i for i, visible in enumerate(visibilities) if visible]
            selected = selected[:_VIEWPORT_MAX_VISIBLE_ROWS]
        if total and not selected and rect_unknown >= total:
            # Every rectangle was unreadable: "cannot tell", not "empty".
            if probe and _TRACE_STATES:
                log.info("probe coverage lost: %s row rects unreadable; full read", total)
            return self._full_read_node(el, depth, path, probe=probe)

        visible_rows: list[RawNode] = []
        for index in selected:
            try:
                row_el = rows.GetElement(index)
                sub = row_el.BuildUpdatedCache(
                    self._probe_subtree_cache if probe else self._cache
                )
                visible_rows.append(
                    self._build_node(
                        row_el,
                        depth + 1,
                        self.max_depth,
                        path + (index,),
                        cached=sub,
                        patterns_only=patterns_only,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - one row must not fail the capture
                log.debug("row %s could not be read: %s", index, exc)

        own = el.BuildUpdatedCache(
            self._probe_element_cache if probe else self._element_cache_request
        )
        node = self._build_node(
            el, depth, depth, path, cached=own, patterns_only=patterns_only
        )
        node.children = visible_rows
        truncated = len(visible_rows) < total
        node.truncated = truncated
        node.virtual_count = total if truncated else None
        if not probe:
            self.viewport_meta.setdefault(self._window_of(el), {})[path] = {
                "items_total": total,
                "items_visible": len(visible_rows),
                "items_truncated": truncated,
                "scroll_pct": _scroll_pct(el, self._UIA),
            }
        return node

    def _full_read_node(
        self, el: Any, depth: int, path: tuple[int, ...], *, probe: bool = False
    ) -> RawNode:
        """Today's read for one node: the whole subtree, no viewport decision."""
        cached = el.BuildUpdatedCache(
            self._probe_subtree_cache if probe else self._cache
        )
        return self._build_node(
            el,
            depth,
            self.max_depth,
            path,
            cached=cached,
            patterns_only=_SIG_PATTERN_NAMES if probe else None,
        )

    # -- layered probe ------------------------------------------------------

    def snapshot_probe(self, window_id: str, max_depth: int = _VIEWPORT_MAX_DEPTH) -> RawTree:
        """The layered settle probe: structure + visible rows in the probe profile.

        Same shape as :meth:`snapshot_viewport`, but every read uses the probe
        profile (signature fields, every state source, the rectangle) and rows
        are read with a probe subtree instead of the full property/pattern set.
        The session decides whether the result is trustworthy: it compares only
        the nodes it watched in the last capture, and falls back to a full
        snapshot when any watched node is missing.

        A probe never raises the window: it runs several times per settle and
        stealing focus each round would be both slow and disruptive. A covered
        or minimized self-drawn window still answers with rectangles, and if it
        does not, the missing watched nodes force the full-read fallback.
        """
        return self._run(lambda: self._probe_tree_sync(window_id, max_depth))

    def _probe_tree_sync(self, window_id: str, max_depth: int) -> RawTree:
        el, win_info = self._element_for_window(window_id)
        self._current_window_id = window_id
        window_rect = _parse_rect(_getattr(el, "CurrentBoundingRectangle", None))
        started = time.perf_counter() if self.timing is not None else 0.0
        try:
            self._element_cache.clear()
            root = self._viewport_node(
                el, 0, max_depth, (), window_rect, set(), probe=True
            )
        finally:
            self._current_window_id = None
            if self.timing is not None:
                _add_timing(self.timing, "probe_ms", started)
        return RawTree(window=win_info, root=root, raw_revision=0)

    def _probe_sync(self, window_id: str) -> RawTree:
        el, win_info = self._element_for_window(window_id)
        self._current_window_id = window_id
        started = time.perf_counter() if self.timing is not None else 0.0
        try:
            # No foreground raise: the session peeks (raises) before it settles,
            # and a probe must stay cheap. A covered self-drawn window renders
            # nothing, but then the signature differs from the pre-action tree
            # and the settler falls back to a full snapshot, which raises.
            root = self._build_node(
                el, depth=0, max_depth=self.max_depth, path=(), signature_only=True
            )
        finally:
            self._current_window_id = None
            if self.timing is not None:
                _add_timing(self.timing, "probe_ms", started)
        return RawTree(window=win_info, root=root, raw_revision=0)

    def _element_for_window(self, window_id: str) -> tuple[Any, WindowInfo]:
        """The live element for a window id, plus its metadata.

        Re-enumerating is what discovers a window that has not been listed yet
        (a pack may name one directly). A window that died between enumeration
        and the read raises ``KeyError``, which the session maps to STALE or the
        "target replaced" outcome.
        """
        el = self._windows.get(window_id)
        if el is not None:
            try:
                return el, WindowInfo(
                    window_id=window_id,
                    title=el.CurrentName or "",
                    process=_process_name(el.CurrentProcessId),
                    pid=el.CurrentProcessId,
                    active=bool(el.CurrentHasKeyboardFocus),
                    hwnd=_top_level_hwnd_of(el),
                )
            except Exception:
                # The window died between enumeration and snapshot (agent was
                # too slow, app closed). Re-enumerate: either it is really gone
                # (KeyError -> clean STALE) or a live element replaces it.
                el = None
        for w in self._list_windows_sync():
            if w.window_id == window_id:
                el = self._windows.get(window_id)
                if el is None:
                    raise KeyError(window_id)
                return el, w
        raise KeyError(window_id)

    def popup_menu_snapshot(self, max_depth: int = 8) -> tuple[str, RawTree] | None:
        """Snapshot the context menu currently on screen, if it is a popup window.

        Two entirely different things are called "a context menu":

        * a popup *inside* the application's own window - Chromium draws QQ's
          menu into the page, so it appears as a `menu` node in that window's
          tree and there is nothing to look for here;
        * a separate top-level popup window - the classic Win32 Shell menu, which
          File Explorer opens as a window of class ``#32768``. It is nobody's
          child, so it never shows up in the owning window's tree and the agent
          would see "menu opened" with no menu.

        This finds the second kind. The popup is registered as a window under a
        synthetic id so the normal snapshot path can read it, which keeps the
        menu's rendering identical to any other tree.
        """
        return self._run(lambda: self._popup_menu_snapshot_sync(max_depth))

    def _popup_menu_snapshot_sync(
        self, max_depth: int
    ) -> tuple[str, RawTree] | None:
        UIA = self._UIA
        auto = self._auto
        root = auto.GetRootElement()
        walker = auto.ControlViewWalker
        # Pass 1: the classic Win32 shell menu class. Highest confidence -
        # nothing else creates a #32768 window.
        child = walker.GetFirstChildElement(root)
        while child is not None:
            nxt = None
            try:
                nxt = walker.GetNextSiblingElement(child)
            except Exception:
                pass
            try:
                cls = child.CurrentClassName or ""
                if cls == "#32768":
                    hwnd = int(child.CurrentNativeWindowHandle or 0)
                    if not hwnd:
                        child = nxt
                        continue
                    window_id = f"popup{hwnd}"
                    el = auto.ElementFromHandle(hwnd)
                    self._windows[window_id] = el
                    info = WindowInfo(
                        window_id=window_id,
                        pid=int(el.CurrentProcessId),
                        process=_process_name(int(el.CurrentProcessId)),
                        title=el.CurrentName or "",
                        active=False,
                        hwnd=hwnd,
                    )
                    tree = self._snapshot_sync(window_id, None, max_depth)
                    return window_id, tree
            except Exception:
                pass
            child = nxt
        # Pass 2: shell/XAML flyouts (Windows 11 Win+X menu, tray menus and
        # the tray overflow panel). Enumerating them via the UIA root walker
        # breaks on the overflow island (NULL COM pointer mid-walk), so find
        # candidates by Win32 class name instead - deterministic and safe.
        import ctypes
        import ctypes.wintypes as _wt
        user32 = ctypes.WinDLL("user32")
        candidates: list[int] = []
        _BUF = ctypes.create_unicode_buffer(256)
        _CB = ctypes.WINFUNCTYPE(ctypes.c_bool, _wt.HWND, _wt.LPARAM)

        def _on_window(h: int, _l: int) -> bool:
            user32.GetClassNameW(h, _BUF, 256)
            if _BUF.value in (
                "Xaml_WindowedPopupClass",
                "Windows.UI.Core.CoreWindow",
                "XamlExplorerHostIslandWindow",
                "TopLevelWindowForOverflowXamlIsland",
                "Shell_TrayFlyout",
            ):
                candidates.append(int(h))
            return True

        try:
            user32.EnumWindows(_CB(_on_window), 0)
        except Exception:
            pass
        # Tray overflow icons are NOT captured here at all: they are always
        # interactive, so _attach_tray_overflow folds them into the taskbar
        # tree at snapshot time. The island's bridge content is cached forever
        # (buttons persist after the panel closes), which made this popup hook
        # return stale panel content and shadow real menus (found live: QQ's
        # tray menu answered with the closed overflow panel's buttons).
        _trust = {
            "Xaml_WindowedPopupClass": 0,   # Win+X / shell menus: transient
            "Shell_TrayFlyout": 1,
            "Windows.UI.Core.CoreWindow": 2,
            "XamlExplorerHostIslandWindow": 2,
        }
        def _trust_of(h: int) -> int:
            buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(h, buf, 256)
            return _trust.get(buf.value, 5)
        candidates.sort(key=_trust_of)
        for hwnd in candidates:
            import ctypes as _ct
            buf = _ct.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, buf, 256)
            if buf.value == "TopLevelWindowForOverflowXamlIsland":
                continue
                # XAML islands (tray overflow panel) are UIA-opaque at the top:
                # their walkers and FindAll return nothing. The content lives
                # under a DesktopWindowContentBridge child hwnd, which exposes
                # a normal tree. Resolve the snapshot root there when present.
                root_hwnd = _content_bridge(hwnd)
                window_id = f"popup{hwnd}"
                try:
                    el = auto.ElementFromHandle(root_hwnd or hwnd)
                except Exception:
                    continue
                # Register BEFORE snapshotting: _snapshot_sync looks the
                # element up by window_id and the bridge/island hwnd pair must
                # both resolve (execution later locates elements under the
                # same id).
                self._windows[window_id] = el
                info = WindowInfo(
                    window_id=window_id,
                    pid=int(el.CurrentProcessId),
                    process=_process_name(int(el.CurrentProcessId)),
                    title=el.CurrentName or "",
                    active=False,
                    hwnd=hwnd,
                )
                tree = self._snapshot_sync(window_id, None, max_depth)
                # Accept windows whose tree shows menu-like content. This is
                # what separates an open flyout/overflow panel from the search
                # host, touch keyboard and other permanent CoreWindows. The
                # tray overflow island keeps its hwnd forever but only ever
                # carries buttons when the panel is actually open, so the
                # content check alone is enough - no one-shot bookkeeping.
                roles = {n.role for n in tree.root.walk()}
                interesting = roles & {"menu", "menuitem", "button", "list", "listitem"}
                if not interesting:
                    continue
                # Panels opened by activate (tray overflow) and menus opened by
                # context_menu are both surfaced here; the runtime renders the
                # result under the node the gesture was made on.
                return window_id, tree
        return None

    def _enter_foreground_sync(self, window_id: str, el: Any) -> ExecResult:
        """Raise the window behind ``window_id`` and record whether it landed.

        Called before every snapshot and every primitive (see
        :meth:`snapshot` and :meth:`execute`): the tree a self-drawing
        application publishes - and the input it queues - belong to whatever is
        in the foreground, so the target has to *be* the foreground. The raise
        is skipped while this window is already the one in front, which is the
        common case (a run of snapshot/execute calls on one window, the
        post-action settle, a child session that owns its own desktop).

        ``SetForegroundWindow`` is refused while another process owns the
        foreground (Win32's foreground lock). That is not fatal for semantic
        UIA primitives - they reach a covered window too - so the result is
        advisory and only the synthetic-input routes (typing, paste) turn a
        refusal into an error (see ``executor._bring_to_foreground``). It is
        *not* silently dropped either: :attr:`last_foreground_error` carries
        the code to the caller, which warns that a covered self-drawn window
        may be publishing an empty shell.
        """
        result = self._raise_foreground_sync(window_id, el)
        self.last_foreground_error = (
            None if result.ok else (result.error or "FOREGROUND_DENIED")
        )
        return result

    def _raise_foreground_sync(self, window_id: str, el: Any) -> ExecResult:
        try:
            hwnd = _top_level_hwnd_of(el)
            if not hwnd:
                return ExecResult(ok=True, message="no top-level window handle")
            import ctypes

            user32 = ctypes.windll.user32
            # Skip only when the window is in front *and restored*: a minimized
            # window is still reported as the foreground window (it is the
            # foreground window that gets minimized), so checking the id alone
            # would leave it minimized and skip the restore that the following
            # snapshot depends on.
            if (
                self._foreground_window_id == window_id
                and int(user32.GetForegroundWindow() or 0) == hwnd
                and not user32.IsIconic(hwnd)
            ):
                return ExecResult(ok=True)
            # `acquire` records the user's state once, shows a minimized window
            # and moves it to this desktop; `raise_window` is the actual raise
            # and reports whether the foreground lock let it through.
            self._workspace.acquire(hwnd)
            self._foreground_window_id = window_id
            if not raise_window(hwnd):
                return ExecResult(
                    ok=False,
                    error="FOREGROUND_DENIED",
                    message=(
                        "the target window could not be raised (another "
                        "application holds the foreground); the tree may be "
                        "incomplete and synthetic input would land elsewhere"
                    ),
                )
        except Exception as exc:  # noqa: BLE001 - converted to a code
            return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
        return ExecResult(ok=True)

    def _snapshot_sync(
        self,
        window_id: str,
        scope: NativeRef | None,
        max_depth: int,
    ) -> RawTree:
        UIA = self._UIA
        el, win_info = self._element_for_window(window_id)

        root_el = el
        if scope is not None:
            root_el = self._find_by_native(el, scope) or el
        # Only the foreground window is reliably rendered (a covered or
        # minimized self-drawn window publishes an empty shell), so raise it
        # before reading anything out of it.
        self._enter_foreground_sync(window_id, el)

        # Nodes built from this snapshot must carry the window they belong to,
        # otherwise execute() later locates them under the wrong root (histori-
        # cally everything defaulted to "w1", which made actions on window w3
        # land on window w1's process - a cross-window safety bug).
        self._current_window_id = window_id
        started = time.perf_counter() if self.timing is not None else 0.0
        walk_done = started
        try:
            self._element_cache.clear()
            root = self._build_node(root_el, depth=0, max_depth=max_depth, path=())
            walk_done = time.perf_counter() if self.timing is not None else 0.0
            # Taskbar: surface the tray overflow icons inline. The overflow
            # panel is *always* interactive (ShowContextMenu works on those
            # buttons without opening the panel), so the icons belong in the
            # tree, folded under the chevron button - the user's mental model,
            # not the desktop's window hierarchy.
            self._attach_tray_overflow(root, max_depth)
        finally:
            self._current_window_id = None
            if self.timing is not None:
                _add_timing(self.timing, "walk_ms", started)
                _add_timing(self.timing, "tray_ms", walk_done)
        return RawTree(window=win_info, root=root, raw_revision=0)

    def _build_node(
        self,
        el: Any,
        depth: int,
        max_depth: int,
        path: tuple[int, ...],
        cached: Any = None,
        signature_only: bool = False,
        patterns_only: frozenset[str] | None = None,
    ) -> RawNode:
        """Build one raw node from the cached element.

        ``signature_only`` builds the node settle's signature needs
        (role/name/value/children/states) from the narrow ``_sig_cache``: ids,
        paths, the element cache and the description are skipped because they
        cost a COM round trip each and none of them is compared. The state
        computation itself is shared with the full path - that is what keeps a
        probe from disagreeing with a real snapshot.
        """
        UIA = self._UIA
        if cached is None:
            try:
                started = time.perf_counter() if self.timing is not None else 0.0
                cached = el.BuildUpdatedCache(
                    self._sig_cache if signature_only else self._cache
                )
                if self.timing is not None:
                    _add_timing(self.timing, "build_cache_ms", started)
            except Exception:
                # The element died mid-build (window closing while the agent acts).
                # Re-raise as KeyError so the session maps it to STALE_TARGET
                # instead of leaking COMError (-2147220991) - found live closing
                # Notepad: the save dialog replaced the main window between the
                # action and the settle snapshot.
                raise KeyError("element vanished during snapshot") from None
        role = UIA_CONTROL_TYPES.get(_getattr(cached, "CachedControlType", 0), "custom")
        only = patterns_only
        if only is None and signature_only:
            only = _SIG_PATTERN_NAMES
        patterns = self._available_patterns(cached, UIA, only=only)

        states: set[str] = set()
        # `_getattr` cannot tell "the cached property said enabled" from "the
        # property could not be read at all", so probe for the member first and
        # only then decide. The default stays True (unreadable -> assume usable)
        # because a self-drawn control can legitimately report no IsEnabled and
        # must not be hidden from the agent; the trace records which case it was
        # so a control that is reported enabled=False can be told apart from one
        # that never answered.
        try:
            enabled_raw: Any = cached.CachedIsEnabled
            enabled_read = True
        except Exception:
            enabled_raw = None
            enabled_read = False
        if not (enabled_raw if enabled_read else True):
            states.add("disabled")
            if _TRACE_STATES:
                # Direction 3 probe: the cached value says disabled. Ask the
                # element again, live, to see whether the cache is simply stale
                # (Chromium re-lays out the DOM but the cached IsEnabled keeps
                # the old value) or the control really is disabled right now.
                try:
                    live_enabled: Any = el.CurrentIsEnabled
                except Exception as exc:  # noqa: BLE001 - diagnostic only
                    live_enabled = f"<raised {type(exc).__name__}>"
                try:
                    live_offscreen: Any = el.CurrentIsOffscreen
                except Exception as exc:  # noqa: BLE001 - diagnostic only
                    live_offscreen = f"<raised {type(exc).__name__}>"
                log.info(
                    "STALE-CHECK role=%s name=%r class=%r cached=%r live_enabled=%r "
                    "live_offscreen=%r",
                    role,
                    (cached.CachedName or "")[:40],
                    cached.CachedClassName or "",
                    enabled_raw,
                    live_enabled,
                    live_offscreen,
                )
        if _TRACE_STATES:
            log.info(
                "state role=%s name=%r class=%r enabled_read=%s enabled_raw=%r ->%s",
                role,
                (cached.CachedName or "")[:40],
                cached.CachedClassName or "",
                enabled_read,
                enabled_raw,
                "disabled" if "disabled" in states else "enabled",
            )
        if _getattr(cached, "CachedIsOffscreen", False):
            states.add("offscreen")
        if _getattr(cached, "CachedHasKeyboardFocus", False):
            states.add("focused")
        # IsRequiredForForm is not in the cache request, so this member access
        # raises for every node; a probe must not pay that just to learn "no".
        if not signature_only and _getattr(cached, "CachedIsRequiredForForm", False):
            states.add("required")

        # Selection / toggle / expand states come from patterns, not the flat
        # cached property set, so read them via GetCachedPropertyValue.
        if _cached_bool(cached, _P_IS_SELECTED):
            states.add("selected")
        toggle = _cached_int(cached, _P_TOGGLE_STATE)
        if toggle == _TOGGLE_ON:
            states.add("checked")
        elif toggle == _TOGGLE_OFF:
            states.add("unchecked")
        ec = _cached_int(cached, _P_EXPAND_COLLAPSE)
        if ec == _EXPAND_ON:
            states.add("expanded")
        elif ec == _EXPAND_OFF:
            states.add("collapsed")
        if _cached_bool(cached, _P_IS_READONLY) and ("value" in patterns or role == "input"):
            states.add("readonly")

        value = _cached_str(cached, _P_VALUE_VALUE)
        value = _stringify_value(value)
        if not value and _is_selection_container(role, cached):
            # A provider may implement SelectionPattern on the *container* and
            # report nothing per item (no ``selected`` state on any row). The
            # container's CurrentSelection is then the only place its current
            # value exists, and without it the observation would show the rows
            # but not which one is picked. Done in both the full and the probe
            # path: ``value`` is part of the settle signature, so the two must
            # agree or every probe would read as "changed".
            value = _current_selection_label(el, cached, UIA)

        if signature_only:
            # A probe never compiles or executes: no runtime id (a COM call
            # per node), no ids, no handle.
            native = NativeRef(window_id=self._window_of(el))
        else:
            native = NativeRef(
                window_id=self._window_of(el),
                runtime_id=tuple(el.GetRuntimeId()),
                # Read through the safe accessor: a UIA provider can answer a
                # cached AutomationId/ClassName with E_INVALIDARG instead of an
                # empty string (seen on a live WPF window). Crashing the whole
                # snapshot over an optional identity field is not acceptable -
                # the runtime id above is still a complete handle.
                automation_id=_getattr(cached, "CachedAutomationId", None) or None,
                class_name=_getattr(cached, "CachedClassName", None) or None,
                control_type=role,
                path=path,
                # Read from the *cached* property set, like every other field here.
                # A single element property read would undo the batching; the value
                # is already in the cache because cache.py requests 30020.
                native_window_handle=_cached_int(cached, _P_NATIVE_WINDOW_HANDLE) or 0,
            )
            # Keep the live element so later executions avoid a re-find round trip.
            if native.runtime_id:
                self._element_cache[native.runtime_id] = el

        children: list[RawNode] = []
        truncated = False
        virtual_count = None
        if depth < max_depth:
            # Read the children from the *cache*, not from a fresh FindAll.
            #
            # This is the difference between O(n) and O(n^2). The cache request
            # is TreeScope_Subtree, so one BuildUpdatedCache on the root already
            # materialised every descendant; GetCachedChildren then hands back
            # those cached elements with no COM round trip. A FindAll here would
            # discard that and re-walk the target process once per node, and the
            # per-element BuildUpdatedCache then re-fetched each node's whole
            # subtree. Measured live on the 1467-node scenario suite: 14.4s vs
            # 2.5s, identical tree.
            kids = self._cached_children(cached)
            n = _child_count(kids)
            try:
                for i in range(n):
                    child_el = kids.GetElement(i)
                    children.append(
                        self._build_node(
                            child_el,
                            depth + 1,
                            max_depth,
                            path + (i,),
                            cached=child_el,
                            signature_only=signature_only,
                        )
                    )
                # A virtualising container only realises its visible items, so
                # the child count is a lower bound, not the true total. Mark it
                # unknown rather than reporting a wrong number: -1 renders as
                # `count>=N`.
                if "itemcontainer" in patterns and n > 0:
                    virtual_count = -1
            except Exception as exc:  # pragma: no cover - platform
                log.debug("child enumeration failed: %s", exc)
                truncated = True

        return RawNode(
            native=native,
            role=role,
            name=_getattr(cached, "CachedName", None) or None,
            value=value,
            # CachedDescription is requested by the full tier only; in signature
            # mode reading it raises for every node and no signature uses it.
            description=None if signature_only else getattr(cached, "CachedDescription", None),
            states=frozenset(states),
            patterns=frozenset(patterns),
            children=children,
            truncated=truncated,
            virtual_count=virtual_count,
        )

    def _attach_tray_overflow(self, root: RawNode, max_depth: int) -> None:
        """Fold tray overflow icons under the chevron button (taskbar only).

        The overflow panel's XAML content lives in a bridge child window and
        stays in the bridge tree even when the panel is closed - but the icons
        are *always* operable (ShowContextMenu/Invoke work on them without the
        panel being visible), so they are part of the taskbar's semantic
        surface. Mounting them under the "显示隐藏的图标" chevron makes
        `find`/`call` reach them directly; no open-the-panel step.
        """
        import ctypes
        chevron = next(
            (c for c in root.walk() if c.name and "显示隐藏" in c.name),
            None,
        )
        if chevron is None:
            return
        user32 = ctypes.WinDLL("user32")
        hwnd = user32.FindWindowW("TopLevelWindowForOverflowXamlIsland", None)
        if not hwnd:
            return
        bridge = _content_bridge(hwnd)
        if not bridge:
            return
        try:
            el = self._auto.ElementFromHandle(bridge)
        except Exception:
            return
        try:
            cond = self._auto.CreateTrueCondition()
            found = el.FindAll(self._UIA.TreeScope_Children, cond)
        except Exception:
            return
        for i in range(found.Length):
            child_el = found.GetElement(i)
            try:
                # depth=3 keeps the icon subtree shallow; path (99, idx) never
                # collides with real taskbar paths and stays deterministic.
                node = self._build_node(
                    child_el, depth=3, max_depth=max_depth, path=(99, i)
                )
            except Exception:
                continue
            # Drop non-interactive wrappers: keep buttons (icons) only.
            if node.role == "button" or node.children:
                chevron.children.append(node)

    def _available_patterns(
        self, el: Any, UIA: Any, only: frozenset[str] | None = None
    ) -> set[str]:
        """Patterns present in the cached element.

        ``only`` restricts the probe to the patterns the cache request actually
        asked for: ``GetCachedPattern`` on an unrequested pattern raises, and a
        signature probe doing that 16 times per node would cost more than the
        full snapshot it is meant to replace.
        """
        out: set[str] = set()
        for name, pid in _PATTERN_IDS.items():
            if only is not None and name not in only:
                continue
            try:
                if el.GetCachedPattern(pid):
                    out.add(name)
            except Exception:
                continue
        if only is not None:
            return out
        # Virtualized containers expose ItemContainer / VirtualizedItem.
        for name, attr in (
            ("itemcontainer", "CachedItemContainerPattern"),
            ("virtualizeditem", "CachedVirtualizedItemPattern"),
        ):
            try:
                if getattr(el, attr):
                    out.add(name)
            except Exception:
                continue
        return out

    def _cached_children(self, cached: Any) -> Any:
        """The cached element array for one node's children, or None.

        A leaf answers this with a NULL COM pointer instead of an empty array,
        so None is a normal result meaning "no children" and must not be
        treated as an enumeration failure (it would mark every leaf truncated).
        """
        try:
            kids = cached.GetCachedChildren()
        except Exception:
            return None
        return kids if kids is not None else None

    def _window_of(self, el: Any) -> str:
        return self._current_window_id or "w1"

    # -- execution ----------------------------------------------------------

    def execute(
        self,
        ref: NativeRef,
        primitive: str,
        value: Any = None,
        timeout_s: float = 3.0,
    ) -> ExecResult:
        """Run one primitive against the target window, raised first.

        The foreground is taken here rather than inside each pattern call:
        some patterns (Invoke on a self-drawn control, ShowContextMenu) act on
        whatever is rendered, and a covered window may have rendered nothing.
        The raise is a no-op when the window is already in front.
        """
        future = self._executor.submit(self._execute_sync, ref, primitive, value)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout:
            log.warning("execute %s on %s timed out after %.1fs", primitive, ref, timeout_s)
            return ExecResult(ok=False, error="TIMEOUT", undetermined=True)

    def _execute_sync(self, ref: NativeRef, primitive: str, value: Any) -> ExecResult:
        el = self._locate(ref)
        if el is None:
            return ExecResult(ok=False, error="STALE_TARGET")
        # `focus`/`read` do not need the window in front (a focus step is a
        # preparation for the *element*, and the capability that follows it
        # raises the window itself). Everything else may depend on rendering,
        # so the target window is raised before the primitive runs.
        if primitive not in ("focus", "read"):
            self._enter_foreground_sync(ref.window_id, el)
        if primitive not in PATTERN_FOR_PRIMITIVE and primitive not in (
            "focus",
            "read",
            "type_text",
            "paste_text",
            "scroll_into_view",
            "context_menu",
        ):
            return ExecResult(ok=False, error="ACTION_NOT_SUPPORTED")
        return run_primitive(el, primitive, value, self._UIA)

    def _find_by_native(self, root_el: Any, ref: NativeRef) -> Any | None:
        # Fast path: elements captured by the most recent snapshot, keyed by
        # runtime id. UIA runtime-id conditions are awkward to marshal, so the
        # live element cache is the primary lookup.
        if ref.runtime_id:
            cached = self._element_cache.get(ref.runtime_id)
            if cached is not None:
                return cached
        # Slow path: re-find by AutomationId (stable across repaints). The hit
        # is corroborated before it is trusted: an AutomationId is only unique
        # by convention, and "the first element with that id" can be a
        # different control (see `_ref_matches_element`).
        if ref.automation_id:
            try:
                cond = self._auto.CreatePropertyCondition(
                    self._UIA.UIA_AutomationIdPropertyId, ref.automation_id
                )
                found = root_el.FindFirst(self._UIA.TreeScope_Descendants, cond)
                if found is not None and _ref_matches_element(found, ref):
                    return found
            except Exception:
                pass
        if not ref.runtime_id:
            return None
        try:
            cond = self._auto.CreatePropertyCondition(
                self._UIA.UIA_RuntimeIdPropertyId, list(ref.runtime_id)
            )
            return root_el.FindFirst(self._UIA.TreeScope_Subtree, cond)
        except Exception:
            return None

    def _locate(self, ref: NativeRef) -> Any | None:
        root = self._windows.get(ref.window_id)
        if root is None:
            return None
        return self._find_by_native(root, ref)

    # -- events -------------------------------------------------------------

    def subscribe(
        self, window_id: str, on_event: Callable[[UiEvent], None]
    ) -> NullSubscription:
        """Event subscription.

        Real UIA event handlers require a message pump / separate handler COM
        object; v0.1 keeps polling as the reliable path and exposes this hook so
        a future implementation (``windows/events.py``) can drop in without
        touching the runtime.
        """
        self.event_counter = EventCounter()
        return NullSubscription()

    # -- helpers ------------------------------------------------------------

    _current_window_id: str | None = None
    event_counter: Any = None

    def _run(self, fn: Callable[[], Any]) -> Any:
        return self._executor.submit(fn).result()


# ---------------------------------------------------------------------------
# Win32 helpers
# ---------------------------------------------------------------------------

#: UIA control types that can be a top-level application window.
_CT_WINDOW = 50032
_CT_PANE = 50033
_TOPLEVEL_CONTROL_TYPES = (_CT_WINDOW, _CT_PANE)


#: Shell surfaces that are Pane-typed but genuinely user-facing.
_SHELL_CLASSES = frozenset(
    {"shell_traywnd", "shell_secondarytraywnd", "progman", "workerw"}
)


def _is_shell_surface(hwnd: int) -> bool:
    """True for the taskbar / desktop surfaces.

    These are not "app windows" by the taskbar test (no WS_EX_APPWINDOW, and
    owned by the shell) but they are still things an agent can act on, so a
    Pane-typed element is kept when it is one of them.
    """
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.IsWindowVisible(hwnd):
        return False
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value.lower() in _SHELL_CLASSES


def _is_app_window(hwnd: int) -> bool:
    """True when *hwnd* looks like a real application window.

    Mirrors the rules Windows uses to decide which windows get a taskbar
    button: visible, and either WS_EX_APPWINDOW or unowned and not a
    tool/no-activate window. This filters the phantom UIA root nodes
    (notification icons, service hosts, hidden message windows).
    """
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not user32.IsWindowVisible(hwnd):
        return False
    GWL_EXSTYLE = -20
    GW_OWNER = 4
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_NOACTIVATE = 0x08000000
    ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if ex_style & WS_EX_APPWINDOW:
        return True
    if ex_style & (WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE):
        return False
    # Owned windows are child dialogs, not top-level apps.
    if user32.GetWindow(hwnd, GW_OWNER):
        return False
    return True


def _enum_top_level_windows() -> list[tuple[int, int, str]]:
    """Visible, titled, top-level Win32 windows as ``(hwnd, pid, title)``.

    Used to reach windows the UIA walker omits or that appear after it stops:
    UWP/WinUI apps hosted by ``ApplicationFrameWindow``, and windows that are
    still being created while the walk runs.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    out: list[tuple[int, int, str]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            GWL_STYLE = -16
            WS_CHILD = 0x40000000
            if user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CHILD:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append((int(hwnd), int(pid.value), buf.value))
        except Exception:
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return out


def _is_shell_chrome(el: Any) -> bool:
    """True for UWP *content* windows, which duplicate the frame window.

    A UWP app appears twice: an ``ApplicationFrameWindow`` (the window the user
    sees) and its ``Windows.UI.Core.CoreWindow`` child. Listing both would show
    the same app twice, so the frame is kept and the content window dropped.
    """
    try:
        return (el.CurrentClassName or "") == "Windows.UI.Core.CoreWindow"
    except Exception:
        return False


def _content_bridge(hwnd: int) -> int | None:
    """Find the DesktopWindowContentBridge child hwnd of a XAML island.

    The tray overflow panel (TopLevelWindowForOverflowXamlIsland) is UIA-opaque
    at the top level, but its content is hosted in a child bridge window that
    exposes a normal accessibility tree. Returns None when there is no bridge
    (ordinary popups), in which case the island hwnd itself is used.
    """
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.WinDLL("user32")
    found: list[int] = []
    CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    buf = ctypes.create_unicode_buffer(256)

    def on_child(h: int, _l: int) -> bool:
        user32.GetClassNameW(h, buf, 256)
        if buf.value == "Windows.UI.Composition.DesktopWindowContentBridge":
            found.append(int(h))
            return False
        return True

    try:
        user32.EnumChildWindows(hwnd, CB(on_child), 0)
    except Exception:
        return None
    return found[0] if found else None


def _add_timing(sink: dict[str, float], name: str, started: float) -> None:
    """Accumulate a segment duration into ``sink`` (SOMNUS_PROFILE only)."""
    sink[name] = sink.get(name, 0.0) + (time.perf_counter() - started) * 1000


def _process_name(pid: int) -> str:
    """The image name for a pid, read live.

    Deliberately *not* cached. Windows reuses pid numbers, and a cache keyed by
    pid outlives the process it described: in one full E2E run a freshly
    launched app inherited a pid that had belonged to another app, so every
    enumeration saw the old image name and the allow-list filtered the real
    window out - the fixture's wait then reported "window did not appear" while
    the process was plainly running. The read itself is ~8us, so caching bought
    almost nothing and cost correctness.
    """
    try:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(len(buf))
            kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            return buf.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return ""


def _stringify_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _getattr(obj: Any, name: str, default: Any = None) -> Any:
    """Safe attribute access for comtypes interfaces with optional members.

    Note the trap this helper hides: a missing member and a member that raises
    are both reported as ``default``. A caller that passes ``True`` therefore
    cannot tell "the property says enabled" from "the property could not be
    read at all". Prefer passing ``None`` and comparing explicitly.
    """
    try:
        return getattr(obj, name)
    except Exception:
        return default


def _child_count(kids: Any) -> int:
    """Number of children in a cached element array; 0 for a NULL array.

    Accessing ``.Length`` on the NULL pointer a leaf returns raises "NULL COM
    pointer access", so the count has to be probed rather than assumed.
    """
    if kids is None:
        return 0
    try:
        return int(kids.Length)
    except Exception:
        return 0


def _cached_prop(cached: Any, prop_id: int) -> Any:
    try:
        return cached.GetCachedPropertyValue(prop_id)
    except Exception:
        return None


def _cached_bool(cached: Any, prop_id: int) -> bool:
    return _cached_prop(cached, prop_id) is True


def _cached_int(cached: Any, prop_id: int) -> int | None:
    value = _cached_prop(cached, prop_id)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _looks_like_row_container(node: RawNode) -> bool:
    """Whether a node's children look like data rows rather than structure."""
    children = node.children
    if len(children) < _VIEWPORT_MIN_ROWS:
        return False
    rows = sum(1 for child in children if child.role in _ROW_ROLES)
    return rows >= len(children) * 0.7


def _parse_rect(value: Any) -> tuple[float, float, float, float] | None:
    """A UIA BoundingRectangle (left, top, width, height), or None."""
    if value is None:
        return None
    try:
        values = [float(part) for part in value]
    except Exception:
        return None
    return tuple(values[:4]) if len(values) >= 4 else None


def _cached_rect(element: Any) -> tuple[float, float, float, float] | None:
    """The cached BoundingRectangle of an element, or None."""
    try:
        value = element.GetCachedPropertyValue(_P_BOUNDING_RECT)
    except Exception:
        return None
    return _parse_rect(value)


def _rect_visible(
    rect: tuple[float, float, float, float] | None,
    window_rect: tuple[float, float, float, float] | None,
) -> bool:
    """Whether ``rect`` intersects the window rectangle with a real area.

    ``IsOffscreen`` is deliberately not consulted: measured on this build's WPF
    store list, every one of the 240 rows - including the 228 scrolled out of
    view - reported ``IsOffscreen = false``. A zero/negative rectangle is not
    visible (collapsed or never laid out).
    """
    if rect is None or window_rect is None:
        return False
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return False
    win_left, win_top, win_width, win_height = window_rect
    if win_width <= 0 or win_height <= 0:
        return False
    return not (
        left + width <= win_left
        or win_left + win_width <= left
        or top + height <= win_top
        or win_top + win_height <= top
    )


def _scroll_pct(element: Any, UIA: Any) -> int | None:
    """The vertical scroll position of a container, 0-100, when readable."""
    try:
        pattern = element.GetCachedPattern(UIA.UIA_ScrollPatternId)
    except Exception:
        return None
    if pattern is None:
        return None
    interface = getattr(UIA, "IUIAutomationScrollPattern", None)
    if interface is not None:
        try:
            pattern = pattern.QueryInterface(interface)
        except Exception:
            return None
    try:
        value = float(pattern.CurrentVerticalScrollPercent)
    except Exception:
        return None
    if value < 0:  # UIA's "not scrollable"
        return None
    return int(round(value))


def _is_selection_container(role: str, cached: Any) -> bool:
    """Whether this node is a control whose "value" is its current selection."""
    if role in ("select", "combobox"):
        return True
    # UIA's own type id is checked as well: a provider may report the type
    # without a role the portable mapping recognises.
    return _getattr(cached, "CachedControlType", 0) == _CT_COMBOBOX


def _current_selection_label(element: Any, cached: Any, UIA: Any) -> str | None:
    """The label of the item a selection container currently has selected.

    UIA reports a selection per *item* (``IsSelected``), and some providers
    implement the container's SelectionPattern only - the rows then carry no
    ``selected`` state at all, so the compiler has nothing to lift onto the
    container. Reading ``CurrentSelection`` here gives the raw tree a
    provider-authoritative ``value``, which the compiler already knows to
    prefer (see ``compile._annotate_selection_values``).

    The cached pattern is tried first (it is part of the full cache request and
    costs no round trip); the live pattern is the fallback, which is what the
    signature probe uses (its narrow cache does not request the pattern). Both
    paths must return the same label, or a probe would disagree with the tree.
    """
    pattern = None
    if cached is not None:
        try:
            pattern = cached.GetCachedPattern(UIA.UIA_SelectionPatternId)
        except Exception:
            pattern = None
    if pattern is None:
        try:
            pattern = element.GetCurrentPattern(UIA.UIA_SelectionPatternId)
        except Exception:
            return None
    if pattern is None:
        return None
    # Both accessors hand back a bare IUnknown pointer; without the cast none
    # of the pattern's members are visible (same reason as executor._pattern).
    interface = getattr(UIA, "IUIAutomationSelectionPattern", None)
    if interface is not None:
        try:
            pattern = pattern.QueryInterface(interface)
        except Exception:
            return None
    selection = _selection_array(pattern)
    if selection is None:
        return None
    try:
        count = int(selection.Length)
    except Exception:
        return None
    for index in range(count):
        try:
            item = selection.GetElement(index)
        except Exception:
            continue
        label = _getattr(item, "CurrentName", None) or _getattr(item, "CachedName", None)
        if label:
            return str(label)
    return None


def _selection_array(pattern: Any) -> Any | None:
    """The item array from a SelectionPattern, cached or live.

    comtypes exposes the getter in whichever shape the typelib declares, and a
    pattern obtained from a cached element answers through its ``Cached...``
    member. Both shapes are tried, cached first (no round trip), and any shape
    that raises is simply skipped - this must never break a snapshot.
    """
    for name in ("GetCachedSelection", "GetCurrentSelection"):
        getter = getattr(pattern, name, None)
        if getter is not None:
            try:
                return getter()
            except Exception:
                continue
    for name in ("CachedSelection", "CurrentSelection"):
        try:
            return getattr(pattern, name)
        except Exception:
            continue
    return None


def _ref_matches_element(element: Any, ref: NativeRef) -> bool:
    """Whether a found element plausibly *is* the ref, for the AutomationId fallback.

    An AutomationId is not unique across a tree: list rows, template-generated
    controls and provider bugs all produce repeats. ``FindFirst`` then returns
    "the first element with that id", which can be a different control - acting
    on it is a silent retarget. The check asks for *one* corroborating identity
    field: control type (mapped through the same table the snapshot uses) or
    class name. Fields the ref does not carry are not judged. If nothing can be
    compared the answer is True (no evidence against).
    """
    comparable = False
    matched = False
    if ref.control_type:
        control_type = _getattr(element, "CurrentControlType", None)
        mapped = UIA_CONTROL_TYPES.get(control_type)
        if mapped is not None:
            comparable = True
            matched = matched or mapped == ref.control_type
    if ref.class_name:
        class_name = _getattr(element, "CurrentClassName", None)
        if class_name is not None:
            comparable = True
            matched = matched or class_name == ref.class_name
    return matched if comparable else True


def _cached_str(cached: Any, prop_id: int) -> str | None:
    value = _cached_prop(cached, prop_id)
    return value if isinstance(value, str) else None


def _set_foreground(el: Any) -> ExecResult:
    """Bring ``el``'s window to the foreground (see ``executor._bring_to_foreground``).

    ``focus_window`` and ``CALL wN.focus()`` use this to switch the session's
    target; it is the same primitive the synthetic-input routes need, so it lives
    in one place.
    """
    from somnus.providers.windows.executor import _bring_to_foreground

    return _bring_to_foreground(el)


def _top_level_hwnd_of(el: Any) -> int:
    """The top-level HWND a window/provider element belongs to, or 0.

    A window element normally maps its own handle; when it does not (a UWP
    frame reports the content window), the Win32 walk from the element's
    parent process handle finds it. 0 means "nothing to raise".
    """
    import ctypes

    try:
        hwnd = int(el.CurrentNativeWindowHandle or 0)
    except Exception:
        hwnd = 0
    if not hwnd:
        return 0
    user32 = ctypes.windll.user32
    return int(user32.GetAncestor(hwnd, 2) or hwnd)  # 2 = GA_ROOT
