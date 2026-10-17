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
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.base import NullSubscription
from somnus.providers.windows.cache import build_cache_request
from somnus.providers.windows.events import EventCounter
from somnus.providers.windows.executor import PATTERN_FOR_PRIMITIVE, run_primitive
from somnus.providers.windows.workspace import Workspace, raise_window

log = logging.getLogger("somnus.uia")

#: Set SOMNUS_TRACE_STATES=1 to log how each node's `enabled` was decided.
#: Used to debug self-drawn (Chromium/Electron) controls whose UIA `IsEnabled`
#: can disagree with what is rendered - e.g. QQ's 发送 button.
_TRACE_STATES = os.environ.get("SOMNUS_TRACE_STATES", "") not in ("", "0", "false")

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


class UiaProvider:
    """AccessibilityProvider backed by UIAutomationCore via comtypes."""

    name = "uia"

    def __init__(self, max_depth: int = 20, allow_list: list[str] | None = None, **_: Any) -> None:
        if not sys.platform.startswith("win"):
            raise RuntimeError("UiaProvider is only available on Windows")
        self.max_depth = max_depth
        self.allow_list = [p.lower() for p in (allow_list or [])]
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

    def _build_cache(self):
        """Create a CacheRequest pulling all properties/patterns we consume."""
        return build_cache_request(self._auto, self._UIA)

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
        """Raise the window behind ``window_id`` and confirm it is in front.

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
        refusal into an error (see ``executor._bring_to_foreground``).
        """
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
        el = self._windows.get(window_id)
        win_info = None
        if el is None:
            for w in self._list_windows_sync():
                if w.window_id == window_id:
                    win_info = w
                    break
            el = self._windows.get(window_id)
            if el is None:
                raise KeyError(window_id)
        else:
            try:
                win_info = WindowInfo(
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
                        win_info = w
                        break
                el = self._windows.get(window_id)
                if el is None:
                    raise KeyError(window_id)

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
        try:
            self._element_cache.clear()
            root = self._build_node(root_el, depth=0, max_depth=max_depth, path=())
            # Taskbar: surface the tray overflow icons inline. The overflow
            # panel is *always* interactive (ShowContextMenu works on those
            # buttons without opening the panel), so the icons belong in the
            # tree, folded under the chevron button - the user's mental model,
            # not the desktop's window hierarchy.
            self._attach_tray_overflow(root, max_depth)
        finally:
            self._current_window_id = None
        return RawTree(window=win_info, root=root, raw_revision=0)

    def _build_node(
        self,
        el: Any,
        depth: int,
        max_depth: int,
        path: tuple[int, ...],
        cached: Any = None,
    ) -> RawNode:
        UIA = self._UIA
        if cached is None:
            try:
                cached = el.BuildUpdatedCache(self._cache)
            except Exception:
                # The element died mid-build (window closing while the agent acts).
                # Re-raise as KeyError so the session maps it to STALE_TARGET
                # instead of leaking COMError (-2147220991) - found live closing
                # Notepad: the save dialog replaced the main window between the
                # action and the settle snapshot.
                raise KeyError("element vanished during snapshot") from None
        role = UIA_CONTROL_TYPES.get(cached.CachedControlType, "custom")
        patterns = self._available_patterns(cached, UIA)

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
        if _getattr(cached, "CachedIsRequiredForForm", False):
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

        native = NativeRef(
            window_id=self._window_of(el),
            runtime_id=tuple(el.GetRuntimeId()),
            automation_id=cached.CachedAutomationId or None,
            class_name=cached.CachedClassName or None,
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
            name=cached.CachedName or None,
            value=value,
            description=getattr(cached, "CachedDescription", None),
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

    def _available_patterns(self, el: Any, UIA: Any) -> set[str]:
        out: set[str] = set()
        for name, pid in _PATTERN_IDS.items():
            try:
                if el.GetCachedPattern(pid):
                    out.add(name)
            except Exception:
                continue
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
        # Slow path: re-find by AutomationId (stable across repaints).
        if ref.automation_id:
            try:
                cond = self._auto.CreatePropertyCondition(
                    self._UIA.UIA_AutomationIdPropertyId, ref.automation_id
                )
                found = root_el.FindFirst(self._UIA.TreeScope_Descendants, cond)
                if found is not None:
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


def _process_name(pid: int) -> str:
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
