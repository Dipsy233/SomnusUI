"""Child sessions: a second Windows login session on the same account.

Why this exists
---------------

A semantic agent that drives an application through synthetic input
(``SendInput``, ``Ctrl+V``) needs the target window to be *foreground*, which
means it competes with the person at the keyboard for the one foreground the
desktop has. There is no Win32 trick that resolves this: input goes to the
foreground window, and there is exactly one per input desktop.

Windows does sell a way out: a **child session**. It is a second interactive
login session for the *same user account* - same files, same registry, same
programs - but with its **own input desktop, its own foreground window and its
own input queue**. An agent that runs there can raise windows, type and paste as
much as it likes, and the person in the first session is unaffected, because the
two sessions do not share the thing they used to fight over.

The cost is stated up front rather than discovered later:

* It needs Windows Pro / Enterprise / Education. Home lacks the feature and
  cannot be made to have it.
* Enabling it writes system settings and needs administrator rights once; the
  terminal-services service is restarted and a reboot is sometimes required.
* Windows allows **one** child session at a time, system-wide.
* It provides **no security isolation**: the two sessions are the same account,
  so the agent can read and write every file the user can.

This module is the thin ``wtsapi32`` layer: enable, query, and answer "am I
inside one?". Establishing a session (the RDP loopback) lives in
:mod:`somnus.session.rdp`.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from typing import NamedTuple

log = logging.getLogger("somnus.session.child")

#: ``WTSGetChildSessionId`` returns this when no child session exists.
NO_CHILD_SESSION = 0xFFFFFFFF

#: ``GetLastError`` when the child-session transport is not configured.
ERROR_NOT_FOUND = 1168


class ChildSessionUnsupported(RuntimeError):
    """The platform cannot do child sessions at all (not Windows, or Home)."""


def _wtsapi():
    if not sys.platform.startswith("win"):  # pragma: no cover - platform
        raise ChildSessionUnsupported("child sessions are a Windows feature")
    dll = ctypes.WinDLL("wtsapi32", use_last_error=True)
    dll.WTSEnableChildSessions.argtypes = [wintypes.BOOL]
    dll.WTSEnableChildSessions.restype = wintypes.BOOL
    dll.WTSIsChildSessionsEnabled.argtypes = [ctypes.POINTER(wintypes.BOOL)]
    dll.WTSIsChildSessionsEnabled.restype = wintypes.BOOL
    dll.WTSGetChildSessionId.argtypes = [ctypes.POINTER(wintypes.ULONG)]
    dll.WTSGetChildSessionId.restype = wintypes.BOOL
    return dll


def is_enabled() -> bool:
    """Whether the child-session feature is turned on for this machine.

    A machine where it has never been enabled answers False; so does one where
    the call itself is unavailable. The two are not distinguished here - the
    caller that needs to *change* the state uses :func:`enable` and reads its
    error.
    """
    try:
        dll = _wtsapi()
    except ChildSessionUnsupported:
        return False
    enabled = wintypes.BOOL()
    if not dll.WTSIsChildSessionsEnabled(ctypes.byref(enabled)):
        log.debug("WTSIsChildSessionsEnabled failed: %s", ctypes.get_last_error())
        return False
    return bool(enabled.value)


def enable(enable_it: bool = True) -> None:
    """Turn the feature on (or off).

    Requires administrator rights. Raising :class:`PermissionError` for a
    refusal is deliberate: this changes the machine, and the caller must be able
    to tell "you are not allowed" from "it did not work".
    """
    dll = _wtsapi()
    if not dll.WTSEnableChildSessions(bool(enable_it)):
        err = ctypes.get_last_error()
        if err in (5,):  # ERROR_ACCESS_DENIED
            raise PermissionError(
                "enabling child sessions needs administrator rights "
                f"(Win32 error {err})"
            )
        raise OSError(err, f"WTSEnableChildSessions({enable_it}) failed")


def child_session_id() -> int | None:
    """The existing child session's id, or None when there is none."""
    try:
        dll = _wtsapi()
    except ChildSessionUnsupported:
        return None
    sid = wintypes.ULONG()
    if not dll.WTSGetChildSessionId(ctypes.byref(sid)):
        # ERROR_NOT_FOUND (1168) is the ordinary "there is none yet" answer.
        err = ctypes.get_last_error()
        if err not in (0, ERROR_NOT_FOUND):
            log.debug("WTSGetChildSessionId failed: %s", err)
        return None
    if sid.value == NO_CHILD_SESSION:
        return None
    return int(sid.value)


#: WTS_CONNECTSTATE_CLASS values that mean "this session can host a desktop".
#: 0 = WTSActive (a user is signed in), 1 = WTSConnected (connected, at the
#: logon screen). WTSDisconnected (4) and WTSListen (6) are not usable.
_WTS_ACTIVE = 0
_WTS_CONNECTED = 1

#: WTS_INFO_CLASS.WTSUserName
_WTS_USERNAME = 5


class SessionEntry(NamedTuple):
    """One row of ``WTSEnumerateSessions``, with the user where there is one."""

    session_id: int
    state: int
    name: str
    user: str

    @property
    def usable(self) -> bool:
        """Whether a desktop exists here that an agent could drive."""
        return self.state in (_WTS_ACTIVE, _WTS_CONNECTED) and bool(self.user)


def enumerate_sessions() -> list[SessionEntry]:
    """Every WTS session, with the signed-in user resolved.

    The user name is fetched per session because the state alone is not enough:
    a session can be Active with no user in the brief window while it is being
    torn down, and a disconnected session keeps its user even though its desktop
    is gone.
    """
    try:
        dll = _wtsapi()
    except ChildSessionUnsupported:
        return []

    dll.WTSEnumerateSessionsW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    dll.WTSEnumerateSessionsW.restype = wintypes.BOOL
    dll.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    dll.WTSQuerySessionInformationW.restype = wintypes.BOOL
    dll.WTSFreeMemory.argtypes = [ctypes.c_void_p]

    class _WTS_SESSION_INFO(ctypes.Structure):
        _fields_ = [
            ("SessionId", wintypes.DWORD),
            ("pWinStationName", wintypes.LPWSTR),
            ("State", ctypes.c_int),
        ]

    buffer = ctypes.c_void_p()
    count = wintypes.DWORD()
    if not dll.WTSEnumerateSessionsW(
        None, 0, 1, ctypes.byref(buffer), ctypes.byref(count)
    ):
        log.debug("WTSEnumerateSessionsW failed: %s", ctypes.get_last_error())
        return []

    entries: list[SessionEntry] = []
    try:
        array = ctypes.cast(buffer, ctypes.POINTER(_WTS_SESSION_INFO))
        for index in range(count.value):
            info = array[index]
            entries.append(
                SessionEntry(
                    session_id=int(info.SessionId),
                    state=int(info.State),
                    name=info.pWinStationName or "",
                    user=_session_user(dll, int(info.SessionId)),
                )
            )
    finally:
        dll.WTSFreeMemory(buffer)
    return entries


def _session_user(dll, session_id: int) -> str:
    buffer = ctypes.c_void_p()
    returned = wintypes.DWORD()
    try:
        if not dll.WTSQuerySessionInformationW(
            None, session_id, _WTS_USERNAME, ctypes.byref(buffer), ctypes.byref(returned)
        ):
            return ""
        return ctypes.wstring_at(buffer) or ""
    except Exception:  # noqa: BLE001 - a missing name is not an error here
        return ""
    finally:
        if buffer:
            dll.WTSFreeMemory(buffer)


def live_child_session() -> SessionEntry | None:
    """The child session **if it really exists with a desktop**, else None.

    ``WTSGetChildSessionId`` alone is not enough to answer this. It keeps
    returning the id of the last session that was *attempted*, so after a refused
    logon - or after a session is reset - it reports an id for a session that is
    not in the session list at all. Acting on that id made the agent report "the
    session exists but has no peer" when in truth there was no session to attach
    to; observed climbing 2 -> 3 -> 4 across failed attempts.

    So the id is treated as a hint and checked against the enumerated sessions:
    only a session that is present, active or connected, and has a user signed in
    counts.
    """
    hinted = child_session_id()
    if hinted is None:
        return None
    for entry in enumerate_sessions():
        if entry.session_id == hinted and entry.usable:
            return entry
    return None


def current_session_id() -> int:
    """The Windows session id this process is running in."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    sid = wintypes.ULONG()
    kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.ULONG),
    ]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    if not kernel32.ProcessIdToSessionId(
        kernel32.GetCurrentProcessId(), ctypes.byref(sid)
    ):
        return -1
    return int(sid.value)


def active_console_session_id() -> int | None:
    """The session the physical console (the main desktop) is on, or None."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        value = int(kernel32.WTSGetActiveConsoleSessionId())
    except Exception:  # noqa: BLE001 - older systems / non-Windows
        return None
    # 0xFFFFFFFF means "no console session attached".
    if value == 0xFFFFFFFF:
        return None
    return value


def inside_child_session() -> bool:
    """Whether this process is running inside the child session.

    Deliberately *not* based on :func:`child_session_id`. That call works only
    from the main session - it is how the main session asks "which session is my
    child?" - and returns failure when invoked *inside* the child session. Using
    it here made the peer refuse to start in the very session it exists for;
    measured live: run in the child session it reported "not a child session".

    The robust test is the one the session actually defines: a child session is
    an interactive session that is **not** the physical console's. The console
    is where the person is, and the child session is created alongside it, so
    "my session != the console session" is exactly "I am the far side".
    """
    console = active_console_session_id()
    mine = current_session_id()
    if console is None or mine < 0:
        # Cannot tell. Reporting False keeps the peer's guard conservative: it
        # will refuse rather than drive the wrong windows.
        return False
    return mine != console


def describe() -> dict[str, object]:
    """A status snapshot for diagnostics and the setup prompt.

    ``child_session_id`` is the raw hint and ``live_child_session_id`` the
    verified one. They differ exactly when a session is half-dead - an attempt
    whose logon was refused, or one that has been reset - which is the state the
    caller needs to tell apart from "a usable session is open".
    """
    live = live_child_session()
    return {
        "platform": sys.platform,
        "is_windows": sys.platform.startswith("win"),
        "supported": is_enabled(),
        "child_session_id": child_session_id(),
        "live_child_session_id": live.session_id if live else None,
        "child_session_user": live.user if live else "",
        "current_session_id": current_session_id(),
        "inside_child_session": inside_child_session(),
    }
