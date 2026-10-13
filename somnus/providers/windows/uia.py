"""Windows UIAutomation provider.

Design notes (dev doc section 13):

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

log = logging.getLogger("somnus.uia")

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

    def __init__(self, max_depth: int = 12, allow_list: list[str] | None = None, **_: Any) -> None:
        if not sys.platform.startswith("win"):
            raise RuntimeError("UiaProvider is only available on Windows")
        self.max_depth = max_depth
        self.allow_list = [p.lower() for p in (allow_list or [])]
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="uia")
        # Initialise COM in MTA on the worker thread *before* any comtypes call
        # can auto-initialise it in STA (dev doc section 13).
        self._executor.submit(self._init_com).result(timeout=10)
        self._windows: dict[str, Any] = {}
        #: (pid, title) -> "wN", so window ids persist across enumerations.
        self._window_id_by_key: dict[tuple[int, str], str] = {}
        self._lock = threading.Lock()
        #: runtime_id -> live UIA element, refreshed on every snapshot.
        self._element_cache: dict[tuple[int, ...], Any] = {}

    # -- COM lifecycle ------------------------------------------------------

    def _init_com(self) -> None:
        # comtypes initialises COM for the importing thread using
        # ``sys.coinit_flags`` (default: STA). We need this worker thread in MTA
        # (dev doc section 13), so set the flag *before* importing comtypes.
        import sys as _sys

        if not hasattr(_sys, "coinit_flags"):
            _sys.coinit_flags = 0x0  # COINIT_MULTITHREADED

        import comtypes
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
        self._executor.shutdown(wait=False)

    # -- window discovery ---------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        return self._run(self._list_windows_sync)

    def _list_windows_sync(self) -> list[WindowInfo]:
        """Enumerate top-level application windows.

        The desktop is walked with ``ControlViewWalker`` rather than
        ``FindAll``: the walker view is what actually surfaces UWP/WinUI
        windows hosted by ``ApplicationFrameWindow`` (such as Settings), which a
        ControlType filter on the root's children misses. Candidate elements are
        then validated with the same Win32 style rules Windows uses to decide
        which windows belong on the taskbar, which drops the phantom nodes
        (notification icons, service hosts) UIA also exposes at the root.

        Missing UWP windows would make whole applications invisible to the agent
        (dev doc section 5: L0 always lists windows).
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
        for hwnd, pid, title in _enum_top_level_windows():
            if hwnd in seen_hwnds:
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
        # The same native window must not be listed twice.
        for existing in out:
            if existing.pid == pid and existing.title == name:
                return
        # Stable window ids: numbering by list position renumbers every window
        # after one closes, silently retargeting agents that hold old refs
        # (found driving Steam live: w3 flipped between Steam and QQ between
        # enumerations). Key ids by (pid, title) instead; new windows get the
        # next free number.
        key = (pid, name)
        wid = self._window_id_by_key.get(key)
        if wid is None:
            used = set(self._window_id_by_key.values())
            n = len(used) + 1
            while f"w{n}" in used:
                n += 1
            wid = f"w{n}"
            self._window_id_by_key[key] = wid
        with self._lock:
            self._windows[wid] = el
        out.append(
            WindowInfo(
                window_id=wid,
                title=name,
                process=proc,
                pid=pid,
                active=bool(el.CurrentHasKeyboardFocus),
            )
        )

    def focus_window(self, window_id: str) -> ExecResult:
        el = self._windows.get(window_id)
        if el is None:
            return ExecResult(ok=False, error="TARGET_NOT_FOUND")
        return self._run(lambda: _set_foreground(el))

    # -- snapshot -----------------------------------------------------------

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 12,
    ) -> RawTree:
        return self._run(lambda: self._snapshot_sync(window_id, scope, max_depth))

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

        # Nodes built from this snapshot must carry the window they belong to,
        # otherwise execute() later locates them under the wrong root (histori-
        # cally everything defaulted to "w1", which made actions on window w3
        # land on window w1's process - a cross-window safety bug).
        self._current_window_id = window_id
        try:
            self._element_cache.clear()
            root = self._build_node(root_el, depth=0, max_depth=max_depth, path=())
        finally:
            self._current_window_id = None
        return RawTree(window=win_info, root=root, raw_revision=0)

    def _build_node(
        self,
        el: Any,
        depth: int,
        max_depth: int,
        path: tuple[int, ...],
    ) -> RawNode:
        UIA = self._UIA
        cached = el.BuildUpdatedCache(self._cache)
        role = UIA_CONTROL_TYPES.get(cached.CachedControlType, "custom")
        patterns = self._available_patterns(cached, UIA)

        states: set[str] = set()
        if not _getattr(cached, "CachedIsEnabled", True):
            states.add("disabled")
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
        )
        # Keep the live element so later executions avoid a re-find round trip.
        if native.runtime_id:
            self._element_cache[native.runtime_id] = el

        children: list[RawNode] = []
        truncated = False
        virtual_count = None
        if depth < max_depth:
            try:
                found = cached.FindAll(UIA.TreeScope_Children, self._auto.CreateTrueCondition())
                n = found.Length
                for i in range(n):
                    child = found.GetElement(i)
                    children.append(
                        self._build_node(
                            child, depth + 1, max_depth, path + (i,)
                        )
                    )
                # A virtualising container only realises its visible items, so
                # the child count is a lower bound, not the true total (dev doc
                # section 13). Mark it unknown rather than reporting a wrong
                # number: -1 renders as `count>=N`.
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
        if primitive not in PATTERN_FOR_PRIMITIVE and primitive not in (
            "focus",
            "read",
            "type_text",
            "scroll_into_view",
            "double_click",
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
    """True for UWP *content* windows, which duplicate the frame window.

    A UWP app appears twice: an ``ApplicationFrameWindow`` (the window the user
    sees) and its ``Windows.UI.Core.CoreWindow`` child. Listing both would show
    the same app twice, so the frame is kept and the content window dropped.
    """
    try:
        return (el.CurrentClassName or "") == "Windows.UI.Core.CoreWindow"
    except Exception:
        return False


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
    """Safe attribute access for comtypes interfaces with optional members."""
    try:
        return getattr(obj, name)
    except Exception:
        return default


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
    try:
        el.SetFocus()
    except Exception:
        pass
    try:
        import ctypes

        hwnd = el.CurrentNativeWindowHandle
        if hwnd:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception as exc:
        return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
    return ExecResult(ok=True)
