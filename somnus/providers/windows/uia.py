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
    "text": 10014,
    "toggle": 10015,
    "selectionitem": 10010,
    "legacyiaccessible": 10018,
    "window": 10017,
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
        UIA = self._UIA
        cond = self._auto.CreatePropertyCondition(
            UIA.UIA_ControlTypePropertyId, UIA.UIA_WindowControlTypeId
        )
        found = self._auto.GetRootElement().FindAll(UIA.TreeScope_Children, cond)
        out: list[WindowInfo] = []
        root = self._auto.GetRootElement()
        for i in range(found.Length):
            el = found.GetElement(i)
            try:
                if el.CurrentIsOffscreen:
                    continue
                name = el.CurrentName or ""
                if not name:
                    continue
                pid = el.CurrentProcessId
                proc = _process_name(pid)
                if self.allow_list and proc.lower() not in self.allow_list:
                    continue
                wid = f"w{len(out) + 1}"
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
            except Exception:  # element vanished mid-enumeration
                continue
        # Mark the foreground window active.
        try:
            fg = self._auto.GetFocusedElement()
            fg_pid = fg.CurrentProcessId
            for w in out:
                if w.pid == fg_pid:
                    w.active = True
        except Exception:
            pass
        return out

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
            win_info = WindowInfo(
                window_id=window_id,
                title=el.CurrentName or "",
                process=_process_name(el.CurrentProcessId),
                pid=el.CurrentProcessId,
                active=bool(el.CurrentHasKeyboardFocus),
            )

        root_el = el
        if scope is not None:
            root_el = self._find_by_native(el, scope) or el

        self._element_cache.clear()
        root = self._build_node(root_el, depth=0, max_depth=max_depth, path=())
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
