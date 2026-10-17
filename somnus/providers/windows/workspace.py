"""Foreground workspace: the target window is raised before it is used.

Three facts drive the design (all verified live):

* Only the *foreground* window is reliably rendered. A visible window that is
  covered by another one exposes a full tree for ordinary Win32/WPF/UWP apps,
  but self-drawn surfaces (Chromium, Electron, Qt Quick) can render nothing -
  or answer UIA with a bare shell - while they are behind another window. So
  the runtime raises the target window instead of working invisibly.
* A *minimized* window lays nothing out at all: Chromium-based apps (Steam,
  QQ, Chrome) collapse to a bare shell. It is restored and activated for the
  same reason.
* ``IVirtualDesktopManager`` can move a top-level window to the *current*
  desktop (``MoveWindowToDesktop``). A window on another desktop is not
  rendered either, so it is moved first.

The session keeps one workspace per window it has touched: the window's
state when first acquired (minimized? which desktop?) is recorded, and
``release`` puts everything back. A window the user never interacted with is
never touched at all.

Raising a window takes the user's focus. That is the deliberate trade: a
complete tree beats an invisible agent. ``peek`` is the exception - the window
list does not need any window raised, so listing windows never steals focus.
Using the *child session* is what restores non-interference (see
docs/child-session.md).
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


def raise_window(hwnd: int) -> bool:
    """Put ``hwnd`` in the foreground; True when it is there afterwards.

    ``SetForegroundWindow`` alone does nothing for a process that does not own
    the foreground: Windows' foreground lock answers with 0 and leaves the
    current window in front (verified live: a background tool asking for a
    Notepad window gets ``GetForegroundWindow()`` unchanged). Three escapes are
    tried, cheapest first:

    1. plain ``SetForegroundWindow`` - works when the lock is not held (the
       process was just started by the user, or already owned the foreground);
    2. attaching to the foreground thread's input queue with
       ``AttachThreadInput``, which makes this thread count as the foreground
       owner for the duration of the call;
    3. an inert ``Alt`` tap followed by ``SetForegroundWindow``. The lock is
       released by *any* input event and an Alt tap is the least intrusive one
       there is - no character is typed, no button is pressed, and the menu bar
       of the foreground window is not entered because the key down and up are
       delivered back to back. This is the only escape that worked in the live
       test, so it is the one that makes the guarantee real.

    Returns False when the window still is not in front (a hung or elevated
    window), which callers treat as advisory for UIA primitives and as fatal
    for synthetic input.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        # 2. Attach to the foreground thread so this thread may set it.
        fg = int(user32.GetForegroundWindow() or 0)
        cur = kernel32.GetCurrentThreadId()
        fg_thread = 0
        attached = False
        if fg:
            pid = ctypes.wintypes.DWORD()
            fg_thread = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
            if fg_thread and fg_thread != cur:
                attached = bool(user32.AttachThreadInput(cur, fg_thread, True))
        try:
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur, fg_thread, False)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            return True
        # 3. Release the foreground lock with an inert Alt tap, then retry.
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
        return int(user32.GetForegroundWindow() or 0) == hwnd
    except Exception:  # noqa: BLE001 - optional capability, never fatal
        log.debug("raise_window(%s) failed", hwnd, exc_info=True)
        return False


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
        """Show the window without activating it.

        Kept as the *restore* half of :meth:`show_activate` and as an explicit
        escape hatch. It is no longer how the runtime operates: a no-activate
        show lays out a plain Win32 window but does not make a self-drawn one
        render reliably (its tree stays a bare shell while it is behind another
        window), which is why `acquire` activates instead.
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
    def show_activate(hwnd: int) -> None:
        """Restore *and* activate the window: the opposite of the above.

        Some applications only lay out (or only answer UIA at all for) the
        window the user can see and interact with - self-drawn surfaces in
        particular render nothing for a window that is behind another one, so
        their accessibility tree comes back as a bare shell. The runtime
        therefore raises the target window before observing or acting on it
        (see ``UiaProvider._enter_foreground_sync``). Losing the layout of a
        hidden window is the cost being paid here in exchange for a complete
        tree.

        ``SetForegroundWindow`` is not enough on its own: Windows refuses it
        while another process owns the foreground (the foreground lock), which
        is the normal case for a tool that was not just clicked. The documented
        escapes are tried in order; see :func:`raise_window`.
        """
        user32 = ctypes.windll.user32
        # Un-minimize *before* asking for the foreground, and keep asking while
        # the window still reports iconic. A single SW_RESTORE can be a no-op:
        # the window is already the foreground one when it is minimized (it is
        # the foreground window that gets minimized), so the raise below would
        # report "already in front" and return without ever restoring it -
        # measured live on Paint, whose frame stays iconic while its tree is
        # rendered into a child window.
        for _ in range(8):
            if not user32.IsIconic(hwnd):
                break
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
        raise_window(hwnd)

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
        """Record how the user had the window and bring it to the foreground.

        ``minimized`` windows are restored *and activated*: a background window
        is not reliably rendered (self-drawn applications lay out nothing for
        it), so the provider takes the foreground rather than trying to work
        invisibly - see :meth:`show_activate`. Windows on another virtual
        desktop are moved to the agent's desktop. The prior state is remembered
        exactly once per window (a second acquire while the agent is already
        working must not overwrite it).
        """
        if hwnd in self.states:
            self.show_activate(hwnd)
            return
        self.states[hwnd] = WindowState(
            minimized=self.is_minimized(hwnd),
            desktop_id=self.desktop_of(hwnd),
        )
        self.move_to_current_desktop(hwnd)
        self.show_activate(hwnd)

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
