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

#: ``SystemParametersInfo`` selectors for the foreground lock.
#:
#: ``SetForegroundWindow`` is refused while another process owns the
#: foreground, and Microsoft's documented condition for it to work reads "the
#: foreground lock time-out has expired (see SPI_GETFOREGROUNDLOCKTIMEOUT)".
#: Measured on this machine the timeout is **200000 ms** - 200 seconds - so that
#: condition effectively never becomes true for a background process, which is
#: exactly the observed behaviour (a tool asking for a Notepad window gets
#: ``GetForegroundWindow()`` unchanged).
#:
#: Setting the timeout to 0 makes the condition hold unconditionally, so a
#: plain ``SetForegroundWindow`` lands with **no synthetic input at all** - no
#: Alt tap, nothing that can reach the foreground application.
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001
#: ``SPIF_UPDATEINIFILE`` (0x01) writes the change to the registry and
#: ``SPIF_SENDCHANGE`` (0x02) broadcasts it. Neither is ever passed: the unlock
#: is for one call only, and a tool must not permanently change how the user's
#: session arbitrates focus. Verified - with ``fWinIni=0``,
#: ``HKCU\Control Panel\Desktop\ForegroundLockTimeout`` stays at 200000.

#: Which rung of :func:`raise_window` actually landed. Kept so the cost of the
#: raise chain is measurable instead of guessed: the whole point of the SPI
#: escape is to make the Alt tap unreachable, and this is how that claim gets
#: checked on a real machine rather than argued.
_RAISE_STATS: dict[str, int] = {
    "already": 0,
    "set": 0,
    "lock": 0,
    "attach": 0,
    "alt": 0,
    "failed": 0,
}


def raise_stats() -> dict[str, int]:
    """How each rung of :func:`raise_window` has fared in this process."""
    return dict(_RAISE_STATS)


def reset_raise_stats() -> None:
    """Zero the counters (tests, and a caller starting a measured run)."""
    for key in _RAISE_STATS:
        _RAISE_STATS[key] = 0


def _lock_timeout() -> int | None:
    """The session's foreground lock timeout in ms, or None if unreadable."""
    import ctypes

    user32 = ctypes.windll.user32
    try:
        value = ctypes.c_ulong(0)
        ok = user32.SystemParametersInfoW(
            SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(value), 0
        )
        return int(value.value) if ok else None
    except Exception:  # noqa: BLE001 - optional capability, never fatal
        return None


def _set_lock_timeout(value: int) -> bool:
    """Set the session's foreground lock timeout. Never writes the registry.

    ``SystemParametersInfoW`` takes the new value through its ``PVOID pvParam``
    slot - as an integer, not a pointer to one. It is therefore passed as a
    plain address. ``ctypes.c_void_p(0)`` would be wrong here: its ``.value`` is
    ``None`` (a null pointer), while the call expects the number 0.
    """
    import ctypes

    user32 = ctypes.windll.user32
    try:
        return bool(
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(int(value)), 0
            )
        )
    except Exception:  # noqa: BLE001 - optional capability, never fatal
        return False


def raise_window(hwnd: int) -> bool:
    """Put ``hwnd`` in the foreground; True when it is there afterwards.

    ``SetForegroundWindow`` alone does nothing for a process that does not own
    the foreground: Windows' foreground lock answers with 0 and leaves the
    current window in front (verified live: a background tool asking for a
    Notepad window gets ``GetForegroundWindow()`` unchanged). The escapes are
    tried cheapest first - and "cheapest" is measured in what reaches the user:

    1. plain ``SetForegroundWindow`` - works when the lock is not held (the
       process was just started by the user, or already owned the foreground);
    2. the same call with the **foreground lock timeout temporarily set to 0**
       (``SPI_SETFOREGROUNDLOCKTIMEOUT``). Microsoft's documented condition for
       ``SetForegroundWindow`` to succeed is "the foreground lock time-out has
       expired"; the session's value is 200000 ms, so it never expires and the
       call is refused forever. Zeroing it makes the condition hold, and the
       call then lands with **no input event of any kind**. The original value
       is restored in a ``finally`` and the registry is never touched
       (``fWinIni=0``), so the user's session is left exactly as it was;
    3. attaching to the foreground thread's input queue with
       ``AttachThreadInput``, which makes this thread count as the foreground
       owner for the duration of the call;
    4. an inert ``Alt`` tap followed by ``SetForegroundWindow``. The lock is
       released by *any* input event, and an Alt tap is the least intrusive one
       there is: no character is typed and no button is pressed.

       It is **not** free, and the cost is visible to the user. An earlier
       version of this docstring claimed the menu bar was not entered because
       the key down and key up are delivered back to back. That was wrong - it
       was inferred, never measured. Measured afterwards on Windows 11: a
       single Alt tap on Notepad makes the window enter its key-tip mode and
       pop one tooltip window (class ``Xaml_WindowedPopupClass``) per toolbar
       button - four of them in that test. They stay on screen until the
       foreground changes, and they appear inside the window's accessibility
       tree, so an agent observing afterwards sees ``主机弹出窗口`` /
       ``弹出窗口`` nodes that were not there before.

       What this costs is the foreground window's *own mode*: an application
       that consumes Alt itself - a menu accelerator, a key-tip overlay, a
       XAML tooltip host - will react, and the reaction outlives the raise.
       That is why this rung exists only as a last resort, and why
       :func:`raise_stats` counts it: a run where ``alt`` is non-zero is a run
       that disturbed the user.

    Rungs 2 and 4 defeat the same lock, and 2 does it without touching the
    user's input at all, so 4 exists only as a last resort - which
    :func:`raise_stats` is there to confirm rather than assume.

    Returns False when the window still is not in front (a hung or elevated
    window), which callers treat as advisory for UIA primitives and as fatal
    for synthetic input.
    """
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    try:
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            _RAISE_STATS["already"] += 1
            return True
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if int(user32.GetForegroundWindow() or 0) == hwnd:
            _RAISE_STATS["set"] += 1
            return True

        # 2. Re-open the foreground lock for the duration of one call. The
        #    timeout is what `SetForegroundWindow` actually checks, so zeroing
        #    it is the documented way to let the call through, and unlike an
        #    input event it reaches nothing the user can see.
        original = _lock_timeout()
        if original is not None and original != 0:
            unlocked = _set_lock_timeout(0)
            try:
                if unlocked:
                    user32.SetForegroundWindow(hwnd)
            finally:
                # Restore even when the raise failed: leaving the session with
                # a 0 ms lock would change how the user's own windows behave.
                _set_lock_timeout(original)
            if int(user32.GetForegroundWindow() or 0) == hwnd:
                _RAISE_STATS["lock"] += 1
                return True

        # 3. Attach to the foreground thread so this thread may set it.
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
            _RAISE_STATS["attach"] += 1
            return True

        # 4. Last resort: release the lock with an inert Alt tap. An input
        #    event reaching the foreground application is a real cost, so it
        #    must stay the path that almost never runs.
        VK_MENU = 0x12
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.SetForegroundWindow(hwnd)
        landed = int(user32.GetForegroundWindow() or 0) == hwnd
        _RAISE_STATS["alt" if landed else "failed"] += 1
        return landed
    except Exception:  # noqa: BLE001 - optional capability, never fatal
        log.debug("raise_window(%s) failed", hwnd, exc_info=True)
        _RAISE_STATS["failed"] += 1
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
