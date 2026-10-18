"""AndroidProvider: the AccessibilityProvider over a Somnus Portal app.

Implements the same contract as the Windows UIA provider (see
:mod:`somnus.providers.base`):

* ``list_windows`` / ``peek``  - window enumeration, never raises;
* ``snapshot``                 - one batched whole-window tree;
* ``execute``                  - one native primitive against one node;
* ``subscribe``                - coarse event counter for the Settler.

Everything Android-specific (IPC, actions, gestures) lives below this
interface; the runtime above cannot tell this provider from the Windows one.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.android.rawtree import portal_json_to_tree, portal_json_to_windows
from somnus.providers.android.transport import PortalError, PortalTransport
from somnus.providers.base import NullSubscription

log = logging.getLogger("somnus.android")

#: Primitive -> Portal ``perform`` action name. The Portal executes the
#: AccessibilityNodeInfo action; nothing here invents coordinates.
ACTION_FOR_PRIMITIVE: dict[str, str] = {
    "activate": "CLICK",
    "long_press": "LONG_CLICK",
    "set_text": "SET_TEXT",
    "toggle": "CLICK",  # a switch's click toggles it; no separate action exists
    "select": "SELECT",
    "expand": "EXPAND",
    "collapse": "COLLAPSE",
    "dismiss": "DISMISS",
    "submit": "IME_ENTER",
    "scroll_into_view": "SHOW_ON_SCREEN",
    "focus": "FOCUS",
    "read": "READ",
    # set_text channel 2 (doc §7.2): the text rides the clipboard (written
    # first via ``clipboard.set``); PASTE itself takes no value argument.
    "paste_text": "PASTE",
}

#: Global actions for the ``system`` tool (doc §6.1).
GLOBAL_ACTIONS = {
    "back": "GLOBAL_ACTION_BACK",
    "home": "GLOBAL_ACTION_HOME",
    "recents": "GLOBAL_ACTION_RECENTS",
    "notifications": "GLOBAL_ACTION_NOTIFICATIONS",
    "quick_settings": "GLOBAL_ACTION_QUICK_SETTINGS",
}

#: Error strings the Portal may return, mapped to the session's error codes.
PORTAL_ERROR_CODES = {
    "NODE_GONE": "STALE_TARGET",
    "TARGET_GONE": "STALE_TARGET",
    "ACTION_FAILED": "NATIVE_EXECUTION_FAILED",
    "SERVICE_DISCONNECTED": "SERVICE_DISCONNECTED",
    "KEYGUARD": "KEYGUARD_LOCKED",
}


class AndroidProvider:
    """Perception + actuation against one Android device via Portal."""

    name = "android"

    #: A live phone redraws, animates and delivers events on its own. The
    #: settler must always run its quiet window.
    is_deterministic = False

    def __init__(
        self,
        *,
        transport: PortalTransport | None = None,
        serial: str | None = None,
        max_depth: int = 20,
        allow_gesture: bool = False,
        **_: Any,
    ) -> None:
        self.max_depth = max_depth
        #: The gesture channel (``dispatchGesture`` at the node's bounds
        #: centre) is off by default: coordinates exist only inside the Portal
        #: and a gesture result is ``OK_UNVERIFIED`` unless the delta proves
        #: it worked (doc §7.1).
        self.allow_gesture = allow_gesture
        self.transport = transport or PortalTransport(serial=serial)
        self._event_count = 0
        self._listeners: list[Callable[[UiEvent], None]] = []
        self._lock = threading.Lock()
        self._snapshot_cache: dict[str, RawTree] = {}
        self._cache_rev: int = -1
        self._apps: list[dict[str, str]] | None = None
        self._last_ok: float = 0.0

    # -- helpers -------------------------------------------------------------

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        try:
            return self.transport.call(method, params)
        except PortalError as err:
            if err.code == "SERVICE_DISCONNECTED":
                # The Portal process died or the service was unbound: the
                # next call reconnects through adb forward (doc §13).
                self.transport.close()
            raise

    def _snapshot_display(self) -> dict[str, Any]:
        result = self._rpc("snapshot", {"max_depth": self.max_depth})
        rev = int((result or {}).get("rev") or 0)
        if rev != self._cache_rev:
            self._snapshot_cache = {
                t.window.window_id: t for t in portal_json_to_tree(result or {})
            }
            self._cache_rev = rev
        return result or {}

    # -- provider API --------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        try:
            snapshot = self._rpc("snapshot", {"max_depth": 0})
        except PortalError as err:
            log.warning("list_windows failed: %s", err)
            return []
        return portal_json_to_windows(snapshot)

    def peek(self, window_id: str) -> WindowInfo | None:
        for win in self.list_windows():
            if win.window_id == window_id:
                return win
        return None

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 20,
    ) -> RawTree:
        self._snapshot_display()
        tree = self._snapshot_cache.get(window_id)
        if tree is None:
            raise KeyError(window_id)
        if scope is None:
            return tree
        node = tree.root.find_native(scope)
        if node is None:
            raise KeyError(scope)
        return RawTree(window=tree.window, root=node, raw_revision=tree.raw_revision)

    def execute(
        self,
        ref: NativeRef,
        primitive: str,
        value: Any = None,
        timeout_s: float = 3.0,
    ) -> ExecResult:
        """Run one primitive against one node.

        Routes (doc §7):

        * ``activate``/``toggle``/... -> ``perform(nodeKey, ACTION)``;
        * ``set_text``                -> ``perform(SET_TEXT, value)`` (Portal
          falls back to clipboard+paste / IME inside the phone);
        * ``scroll``                  -> ``perform(SCROLL_*, value)``;
        * ``gesture``                 -> only when ``allow_gesture`` is on.
        """
        window_part = ref.window_id
        if window_part not in {t for t in self._snapshot_cache}:
            # Refresh so the Portal can match the node by key.
            try:
                self._snapshot_display()
            except PortalError as err:
                return ExecResult(ok=False, error=PORTAL_ERROR_CODES.get(
                    err.code or "", "NATIVE_EXECUTION_FAILED"
                ), message=str(err))

        if primitive == "gesture":
            if not self.allow_gesture:
                return ExecResult(
                    ok=False,
                    error="ACTION_NOT_SUPPORTED",
                    message="gesture channel disabled (--allow-gesture)",
                )
            result = self._gesture(ref, value)
            # A gesture is never verified by the portal: only the delta can
            # prove it worked (doc §7.1).
            return ExecResult(ok=result, undetermined=result)

        action = ACTION_FOR_PRIMITIVE.get(primitive)
        if action is None:
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message=f"no Portal action for primitive {primitive!r}",
                unsupported=True,
            )

        params: dict[str, Any] = {
            # The window id as the Portal expects it (int); the node key in
            # the Portal's slash-separated form. The Portal's findByKey treats
            # the key's FIRST segment as the window id ("6779/0/3"), so the
            # runtime_id path must be prefixed with it (found on device: a
            # bare path "0/0/..." never matched -> NODE_GONE for every node).
            "window": int(window_part.rsplit("/", 1)[1]),
            "node": f"{window_part.rsplit('/', 1)[1]}/"
            + "/".join(str(p) for p in ref.runtime_id),
            "action": action,
        }
        if value is not None and primitive in ("set_text", "paste_text", "type_text"):
            params["value"] = str(value)
        if value is not None and primitive == "scroll":
            direction = value if isinstance(value, str) else None
            mapping = {
                "down": "SCROLL_FORWARD",
                "up": "SCROLL_BACKWARD",
                "left": "SCROLL_LEFT",
                "right": "SCROLL_RIGHT",
                "top": "SCROLL_TO_TOP",
                "bottom": "SCROLL_TO_BOTTOM",
            }
            params["action"] = mapping.get(direction or "", "SCROLL_FORWARD")
        # set_text channel 2: write the text to the clipboard first, then
        # ACTION_PASTE at the node. The paste step carries no value.
        if primitive == "paste_text":
            if not self.set_clipboard(str(value or "")):
                return ExecResult(
                    ok=False,
                    error="NATIVE_EXECUTION_FAILED",
                    message="clipboard write failed; check the clipboard channel",
                )
            params.pop("value", None)

        try:
            result = self._rpc("perform", params)
        except PortalError as err:
            code = PORTAL_ERROR_CODES.get(err.code or "", "NATIVE_EXECUTION_FAILED")
            return ExecResult(ok=False, error=code, message=str(err))
        ok = bool((result or {}).get("ok"))
        if ok:
            self._last_ok = time.monotonic()
            self._bump_events()
            return ExecResult(ok=True)
        err_name = str((result or {}).get("error") or "ACTION_FAILED")
        # A node the Portal can no longer address is a stale ref, not a
        # routing failure: the chain must not try another route.
        unsupported = err_name in ("UNSUPPORTED", "NO_SUCH_ACTION")
        return ExecResult(
            ok=False,
            error=PORTAL_ERROR_CODES.get(err_name, "NATIVE_EXECUTION_FAILED"),
            message=str((result or {}).get("message") or err_name),
            unsupported=unsupported,
        )

    def subscribe(
        self, window_id: str, on_event: Callable[[UiEvent], None]
    ) -> NullSubscription:
        # Events are polled through the snapshot revision (the Portal bumps
        # ``rev`` on any change); a push channel is a later optimisation.
        self._listeners.append(on_event)
        return NullSubscription()

    def close(self) -> None:
        self.transport.close()
        self._snapshot_cache.clear()

    # -- Android-specific surface (Session-level tools call these) -----------

    def global_action(self, name: str) -> ExecResult:
        """BACK / HOME / RECENTS / NOTIFICATIONS (doc §4.2 ``global``)."""
        action = GLOBAL_ACTIONS.get(name)
        if action is None:
            return ExecResult(
                ok=False, error="ACTION_NOT_SUPPORTED", message=f"no global {name!r}"
            )
        try:
            result = self._rpc("global", {"action": action})
        except PortalError as err:
            return ExecResult(
                ok=False,
                error=PORTAL_ERROR_CODES.get(err.code or "", "NATIVE_EXECUTION_FAILED"),
                message=str(err),
            )
        if bool((result or {}).get("ok")):
            self._bump_events()
            return ExecResult(ok=True)
        return ExecResult(ok=False, error="NATIVE_EXECUTION_FAILED")

    def launch_app(self, package: str) -> ExecResult:
        try:
            result = self._rpc("launch", {"package": package})
        except PortalError as err:
            code = "APP_NOT_FOUND" if err.code == "APP_NOT_FOUND" else (
                PORTAL_ERROR_CODES.get(err.code or "", "NATIVE_EXECUTION_FAILED")
            )
            return ExecResult(ok=False, error=code, message=str(err))
        if bool((result or {}).get("ok")):
            self._bump_events()
            return ExecResult(ok=True)
        return ExecResult(
            ok=False,
            error="APP_NOT_FOUND",
            message=str((result or {}).get("message") or package),
        )

    def list_apps(self) -> list[dict[str, str]]:
        """Launchable apps (``OBSERVE apps``)."""
        if self._apps is None:
            try:
                self._apps = list(self._rpc("apps") or ())
            except PortalError:
                self._apps = []
        return self._apps

    def set_clipboard(self, text: str) -> bool:
        """The clipboard write half of the paste route (doc §4.2)."""
        try:
            result = self._rpc("clipboard.set", {"text": text})
        except PortalError:
            return False
        return bool((result or {}).get("ok"))

    # -- internals -----------------------------------------------------------

    def _gesture(self, ref: NativeRef, value: Any) -> bool:
        # The node lookup is only for a bounds sanity check; the Portal
        # resolves the key itself. A stale cache must not veto the gesture:
        # the Portal walks the live tree, so skip the check when the cached
        # snapshot no longer contains the ref (found on device: the search
        # text sat in a snapshot older than the current window id).
        tree = self._snapshot_cache.get(ref.window_id)
        node = tree.root.find_native(ref) if tree else None
        kind = value if isinstance(value, str) else "tap"
        try:
            result = self._rpc(
                "gesture",
                {
                    "window": int(ref.window_id.rsplit("/", 1)[1]),
                    "node": _key_of(ref),
                    "kind": kind,
                },
            )
        except PortalError:
            return False
        return bool((result or {}).get("ok"))

    def _bump_events(self) -> None:
        self._event_count += 1
        for listener in list(self._listeners):
            try:
                listener(UiEvent(window_id="*", kind="structure"))
            except Exception:  # noqa: BLE001 - a listener must not kill the loop
                log.exception("event listener failed")

    @property
    def event_count(self) -> int:
        return self._event_count

    @property
    def last_activity(self) -> float:
        """Monotonic time of the last successful call (Portal liveness)."""
        return getattr(self, "_last_ok", 0.0)


def _key_of(ref: NativeRef) -> str:
    """Portal node key: the window id followed by the runtime_id path.

    The Portal's findByKey treats the key's first segment as the window id,
    so a bare dotted path never matches. Same rule as ``execute``.
    """
    window = ref.window_id.rsplit("/", 1)[1]
    return "/".join([window, *(".".join(str(p) for p in ref.runtime_id)).split(".")])
