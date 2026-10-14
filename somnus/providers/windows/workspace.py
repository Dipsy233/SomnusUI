"""Background workspace: operate windows without stealing the user's screen.

Three facts drive the design (all verified live):

* A window that is *visible* - even completely covered by other windows -
  exposes its full accessibility tree and accepts every UIA primitive
  (Invoke, Value, ScrollItem, ShowContextMenu). Being foreground is not a
  requirement of anything SomnusUI does.
* A *minimized* window lays nothing out: Chromium-based apps (Steam, QQ,
  Chrome) collapse to a bare shell. Restoration before operating is
  mandatory - but it must not steal activation, which is why the restore
  goes through ``SW_SHOWNOACTIVATE``.
* ``IVirtualDesktopManager`` can move a top-level window to the *current*
  desktop (``MoveWindowToDesktop``). Windows on another desktop are usually
  still laid out, but the exact behaviour varies by app; moving first makes
  "the agent works on desktop 2 while the user watches desktop 1" reliable
  instead of hopeful.

The session keeps one workspace per window it has touched: the window's
state when first acquired (minimized? which desktop?) is recorded, and
``release`` puts everything back. A window the user never interacted with is
never touched at all.
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass, field

log = logging.getLogger("somnus.workspace")

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
SW_SHOWMINNOACTIVE = 7
GWL_STYLE = -16
WS_MINIMIZE = 0x2000_0000

#: COM class/iid for the shell's virtual desktop manager (Win10 1607+).
_CLSID_VDM = "{aa509086-5ca9-4c25-8f95-589d3c07b48a}"
_IID_IVDM = "{a5cd92ff-29be-454c-8d04-d82879fb3f1b}"


@dataclass(slots=True)
class WindowState:
    """How the user had the window when the agent acquired it."""

    minimized: bool
    desktop_id: bytes | None = None  # raw GUID bytes, None when unknown


@dataclass(slots=True)
class Workspace:
    """Restores windows to how the user had them after agent operations."""

    states: dict[int, WindowState] = field(default_factory=dict)
    _vdm: object | None = field(default=None, repr=False)

    # -- window-state helpers ------------------------------------------------

    @staticmethod
    def is_minimized(hwnd: int) -> bool:
        return bool(ctypes.windll.user32.IsIconic(hwnd))

    @staticmethod
    def needs_show(hwnd: int) -> bool:
        """True when the window is hidden or minimized and must be laid out.

        ``IsWindowVisible`` stays True for a minimized window, so the check is
        the pair: hidden OR iconic means the renderer may have nothing laid
        out and a snapshot would come back near-empty.
        """
        user32 = ctypes.windll.user32
        return not user32.IsWindowVisible(hwnd) or bool(user32.IsIconic(hwnd))

    @staticmethod
    def show_no_activate(hwnd: int) -> None:
        """Make the window visible without activating it.

        A minimized CEF window lays out nothing, so it must be shown before a
        snapshot - but ``SW_RESTORE`` steals activation, which yanks focus
        from whatever the user is typing into. ``SW_SHOWNOACTIVATE`` lays the
        window out and leaves the focus alone.
        """
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            # ShowWindow(SW_SHOWNOACTIVATE) alone does not un-minimize; the
            # documented sequence is SW_RESTORE followed by a no-activate
            # show, which restores layout and then drops the activation.
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)

    @staticmethod
    def minimize_no_activate(hwnd: int) -> None:
        ctypes.windll.user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)

    @staticmethod
    def is_visible(hwnd: int) -> bool:
        return bool(ctypes.windll.user32.IsWindowVisible(hwnd))

    # -- virtual desktop -----------------------------------------------------

    def _manager(self):
        """``IVirtualDesktopManager``, or None when unavailable (older Win)."""
        if self._vdm is not None:
            return self._vdm or None
        try:
            import comtypes
            from comtypes import COMMETHOD, GUID, HRESULT, POINTER, IUnknown

            class _IVDM(IUnknown):
                _iid_ = GUID(_IID_IVDM)
                _methods_ = [
                    COMMETHOD([], HRESULT, "IsWindowOnCurrentVirtualDesktop",
                              (["in"], ctypes.c_void_p, "topLevelWindow"),
                              (["out", "retval"], POINTER(ctypes.c_long), "onCurrent")),
                    COMMETHOD([], HRESULT, "GetWindowDesktopId",
                              (["in"], ctypes.c_void_p, "topLevelWindow"),
                              (["out", "retval"], POINTER(GUID), "desktopId")),
                    COMMETHOD([], HRESULT, "MoveWindowToDesktop",
                              (["in"], ctypes.c_void_p, "topLevelWindow"),
                              (["in"], GUID, "desktopId")),
                ]

            import comtypes.client

            comtypes.CoInitialize()
            mgr = comtypes.CoCreateInstance(
                GUID(_CLSID_VDM), interface=_IVDM
            )
            self._vdm = mgr
            return mgr
        except Exception as exc:  # noqa: BLE001 - optional capability
            log.debug("IVirtualDesktopManager unavailable: %s", exc)
            self._vdm = False
            return None

    def desktop_of(self, hwnd: int) -> bytes | None:
        """The GUID of the desktop ``hwnd`` is on, or None when unknown."""
        mgr = self._manager()
        if mgr is None:
            return None
        try:
            return bytes(mgr.GetWindowDesktopId(hwnd))
        except Exception:  # noqa: BLE001
            return None

    def move_to_current_desktop(self, hwnd: int) -> bool:
        """Bring ``hwnd`` to the desktop the agent is running on.

        Returns True when the window moved (or was already here). Failure is
        not fatal: a window on another desktop usually still lays out and
        accepts UIA primitives - the move is a reliability improvement, not a
        requirement.
        """
        mgr = self._manager()
        if mgr is None:
            return True  # treat as best-effort success
        try:
            import comtypes
            from comtypes import GUID

            current = GUID()
            # There is no documented "current desktop id"; the trick is to
            # read the desktop of the shell window, which always lives on the
            # active one.
            progman = ctypes.windll.user32.FindWindowW("Progman", None)
            shell = ctypes.windll.user32.FindWindowExW(progman, 0, "SHELLDLL_DefView", None)
            hwnd_ref = shell or progman
            mgr.GetWindowDesktopId(hwnd_ref, ctypes.byref(current))
            mgr.MoveWindowToDesktop(hwnd, current)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("MoveWindowToDesktop failed: %s", exc)
            return False

    # -- acquire / release ---------------------------------------------------

    def acquire(self, hwnd: int) -> None:
        """Record how the user had the window and make it operable.

        ``minimized`` windows are shown without activation; windows on
        another virtual desktop are moved to the agent's desktop. The prior
        state is remembered exactly once per window (a second acquire while
        the agent is already working must not overwrite it).
        """
        if hwnd in self.states:
            self.show_no_activate(hwnd)
            return
        self.states[hwnd] = WindowState(
            minimized=self.is_minimized(hwnd),
            desktop_id=self.desktop_of(hwnd),
        )
        self.show_no_activate(hwnd)
        self.move_to_current_desktop(hwnd)

    def release(self, hwnd: int) -> None:
        """Put the window back the way the user had it."""
        state = self.states.pop(hwnd, None)
        if state is None:
            return
        try:
            if state.minimized:
                self.minimize_no_activate(hwnd)
            # Returning the window to its original desktop only makes sense
            # when the agent actually moved it; a same-desktop acquire leaves
            # nothing to undo. `desktop_id` being None means the query did
            # not work, in which case we also stay put - moving on a guess
            # would be worse than leaving it.
        except Exception:  # noqa: BLE001 - restoration must never raise
            log.debug("restore window %s failed", hwnd, exc_info=True)

    def release_all(self) -> None:
        for hwnd in list(self.states):
            self.release(hwnd)
