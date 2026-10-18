"""Start the peer with the child session's own logon.

How the far-side process gets started
-------------------------------------

The peer must exist inside the child session before the agent can drive
anything. Nothing in this session can create a process there: the session
boundary blocks it, and the supported alternative (a scheduled task, or
``CreateProcessAsUser`` with ``WTSQueryUserToken``) needs administrator or
SYSTEM rights and, for the task route, often lands the process in the *wrong*
session anyway - measured: ``schtasks /run`` put the process in session 1 while
a child session was open.

What does work is the mechanism ParaDesk uses, and for the same stated reason:
the child session is an **ordinary interactive logon for the same account**, so
it runs ``HKCU\\...\\Run`` like any other. That means:

* no administrator rights - the key is per-user;
* the user can see it in Settings → Apps → Startup and disable it;
* it fires on every logon of that session, including a reconnect after the
  window was closed and reopened.

Two details that are easy to get wrong, both handled here:

* ``StartupApproved\\Run`` carries a byte whose first value 0x03 means
  "disabled". A key written while that marker is present is silently ignored,
  so the marker is cleared after writing.
* The command must be an absolute path to an interpreter plus an absolute path
  to the project; a Run key runs with an unspecified working directory.
"""

from __future__ import annotations

import logging
import os
import sys
import winreg
from pathlib import Path

log = logging.getLogger("somnus.session.peerstart")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
)

#: The Run value's name. Deliberately descriptive: the user finds it in the
#: Startup list and must be able to tell what it is.
VALUE_NAME = "SomnusUI child-session peer"


def _python_for_run_key() -> str:
    """The interpreter to record: prefer pythonw so no console flashes up."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        quiet = exe.with_name("pythonw.exe")
        if quiet.exists():
            return str(quiet)
    return str(exe)


def _project_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def build_command(python: str | None = None, project: str | None = None) -> str:
    """The exact command line the Run key should hold."""
    interpreter = python or _python_for_run_key()
    root = Path(project) if project else _project_dir()
    return f'"{interpreter}" -m somnus.cli child-session peer --project "{root}"'


def register(
    *,
    python: str | None = None,
    project: str | None = None,
    remove: bool = False,
) -> tuple[bool, str]:
    """Write (or delete) the Run entry. Returns (ok, message)."""
    try:
        if remove:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                try:
                    winreg.DeleteValue(key, VALUE_NAME)
                    return True, f"removed the Run entry {VALUE_NAME!r}"
                except FileNotFoundError:
                    return True, "no Run entry to remove"
        command = build_command(python, project)
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
        cleared = _clear_approval_block()
        message = f"registered: {command}"
        if cleared:
            message += "\n    (cleared a previous \"disabled\" marker in Startup)"
        return True, message
    except PermissionError as exc:
        return False, f"cannot write the Run key: {exc}"
    except OSError as exc:
        return False, f"cannot write the Run key: {exc}"


def _clear_approval_block() -> bool:
    """Remove the StartupApproved marker that would suppress the new entry."""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, APPROVED_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            except FileNotFoundError:
                return False
            disabled = isinstance(value, (bytes, bytearray)) and len(value) and value[0] == 3
            winreg.DeleteValue(key, VALUE_NAME)
            return disabled
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.debug("could not clear the approval marker: %s", exc)
        return False


def status() -> str | None:
    """A one-line description of the registration, or None when absent."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"cannot read the Run key: {exc}"

    approved = True
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
            marker, _ = winreg.QueryValueEx(key, VALUE_NAME)
            approved = not (
                isinstance(marker, (bytes, bytearray)) and len(marker) and marker[0] == 3
            )
    except (FileNotFoundError, OSError):
        pass

    state = "enabled" if approved else "DISABLED (turn it on in Settings → Startup)"
    return f"{state}: {value}"
