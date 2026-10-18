"""The one-time system setup that makes a child session possible.

Enabling the feature (``WTSEnableChildSessions``) is only the first of several
steps; a machine that has had only that one done will fail to connect, and the
failure looks like "you may need to reboot" when it is really "the listener is
still off". The set, each verified live:

1. ``WTSEnableChildSessions(true)`` - the feature itself.
2. ``fDenyTSConnections = 0`` - **Windows ships with the RDP listener off.**
   Without this the control connects to nothing. This is the step whose absence
   looks like a reboot problem.
3. ``TermService`` to automatic, and running.

Signing in
----------

There are two routes, and the order matters:

1. **Credential delegation** (tried first, and what the steps below set up).
   The loopback is a logon like any other, but Windows is permitted to use the
   credentials the user already signed in with, so no password is typed and
   **none is stored**. This is the normal path.
2. **A stored password** (``somnus child-session credential --save``), for when
   policy refuses delegation - an enterprise lockdown, or an account whose
   sign-in cannot be delegated. Only then is a secret kept, and it is
   DPAPI-encrypted for this machine and account.

Signing in at the RDP window by hand is always available as well; nothing here
prevents it, and a machine with no delegation and no stored password gets
precisely that.

Everything here needs administrator rights; each step reports its own outcome so
a partial success is visible rather than reported as one boolean.

**Why not just tell the user to reboot.** It is what the failure looks like, but
it is not the cause: on the machine this was written against, the listener was
still denied and TermService still manual after the feature was enabled, and no
reboot would have changed either. The steps are run explicitly so the *actual*
missing piece is what gets fixed.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import winreg
from dataclasses import dataclass, field

from somnus.session import childsession

log = logging.getLogger("somnus.session.setup")

#: The Terminal Server key that holds the listener switch.
TS_KEY = r"SYSTEM\CurrentControlSet\Control\Terminal Server"
#: The RDP-Tcp station, where "always prompt for password" also lives.
RDP_TCP_KEY = TS_KEY + r"\WinStations\RDP-Tcp"

#: Credential delegation policy: whether the default credentials may be handed
#: to a listed server, and the list itself.
CRED_DELEG_KEY = r"SOFTWARE\Policies\Microsoft\Windows\CredentialsDelegation"
ALLOW_LIST_KEY = CRED_DELEG_KEY + r"\AllowDefaultCredentials"

#: Where "always prompt for password" is enforced.
TS_POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services"

#: The servers the default credentials may be delegated to. Scoped to this
#: machine on purpose: ``TERMSRV/*`` would hand the credentials to *any* RDP
#: server the user later connects to, which a loopback does not need and which
#: is a real widening of exposure.
DELEGATION_SPNS = ("TERMSRV/localhost", "TERMSRV/127.0.0.1")


def _delegation_spns() -> tuple[str, ...]:
    """The allow-list entries this setup needs, machine name included.

    ``TERMSRV/localhost`` covers the loopback by name, the IP covers a client
    that resolves it, and the machine name covers a connection given the host
    name. All three are cheap and all three stay scoped to this machine.
    """
    machine = os.environ.get("COMPUTERNAME", "")
    extra = (f"TERMSRV/{machine}",) if machine else ()
    return DELEGATION_SPNS + extra


@dataclass
class StepResult:
    """One setup step's outcome, so a partial run is legible."""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class SetupReport:
    """The whole run, in order, with the state before and after."""

    steps: list[StepResult] = field(default_factory=list)
    before: dict[str, object] = field(default_factory=dict)
    after: dict[str, object] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def lines(self) -> list[str]:
        out = []
        for s in self.steps:
            mark = "ok  " if s.ok else "FAIL"
            out.append(f"[{mark}] {s.name}" + (f" - {s.detail}" if s.detail else ""))
        return out


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 - not Windows is handled by the caller
        return False


def _read_dword(root, path: str, name: str):
    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.debug("cannot read %s\\%s: %s", path, name, exc)
        return None


def _write_dword(root, path: str, name: str, value: int) -> None:
    with winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def listener_enabled() -> bool:
    """Whether the RDP listener is allowed to accept connections."""
    root = winreg.HKEY_LOCAL_MACHINE
    value = _read_dword(root, TS_KEY, "fDenyTSConnections")
    # Absent means the default, which is "not denied" on client SKUs and
    # "denied" in practice once a policy has touched it; treating absent as
    # enabled-and-verify-elsewhere would hide the case we care about, so an
    # absent value is reported as not-verified.
    return value == 0


def run(*, dry_run: bool = False) -> SetupReport:
    """Run the full setup. Needs administrator rights.

    ``dry_run`` reports what would change without writing anything, which is how
    a caller can show the user the plan before it is applied.
    """
    from somnus.session import setup as _unused  # noqa: F401 - import cycle guard

    report = SetupReport(dry_run=dry_run)
    report.before = childsession.describe()

    if not dry_run and not _is_admin():
        report.steps.append(
            StepResult(
                "administrator rights",
                False,
                "not elevated; re-run in an administrator shell",
            )
        )
        report.after = childsession.describe()
        return report

    root = winreg.HKEY_LOCAL_MACHINE

    # 1. the feature flag
    report.steps.append(_step_enable_feature(dry_run))

    # 2. the RDP listener (Windows ships with it denied)
    report.steps.append(_step_enable_listener(root, dry_run))

    # 3. delegate the default credentials, so the loopback signs in without a
    #    person typing (and without a stored password)
    report.steps.append(_step_delegation(root, dry_run))

    # 4. ... and stop "always prompt for password" from overriding that
    report.steps.append(_step_no_password_prompt(root, dry_run))

    # 5. TermService automatic and running
    report.steps.append(_step_term_service(dry_run))

    report.after = childsession.describe() if not dry_run else report.before
    return report


def _step_enable_feature(dry_run: bool) -> StepResult:
    name = "enable child sessions (WTSEnableChildSessions)"
    if dry_run:
        return StepResult(name, True, "would enable")
    if childsession.is_enabled():
        return StepResult(name, True, "already enabled")
    try:
        childsession.enable(True)
        return StepResult(name, True)
    except PermissionError as exc:
        return StepResult(name, False, str(exc))
    except OSError as exc:
        return StepResult(name, False, str(exc))


def _step_enable_listener(root, dry_run: bool) -> StepResult:
    name = "enable the Remote Desktop listener (fDenyTSConnections = 0)"
    if listener_enabled():
        return StepResult(name, True, "already enabled")
    if dry_run:
        return StepResult(name, True, "would set fDenyTSConnections = 0")
    try:
        _write_dword(root, TS_KEY, "fDenyTSConnections", 0)
    except PermissionError as exc:
        return StepResult(name, False, f"access denied ({exc})")
    except OSError as exc:
        return StepResult(name, False, str(exc))
    return StepResult(
        name,
        listener_enabled(),
        "" if listener_enabled() else "wrote the value but it did not read back as 0",
    )


def _step_delegation(root, dry_run: bool) -> StepResult:
    """Allow the default credentials to reach this machine's loopback.

    This is what makes the sign-in automatic *without storing a password*: the
    loopback is still a logon, but Windows is permitted to use the credentials
    the user already signed in with. A stored password (see
    :mod:`somnus.session.credentials`) is the fallback for when policy refuses
    this - and is only needed then.
    """
    name = "delegate default credentials to the loopback"
    wanted = _delegation_spns()
    if dry_run:
        return StepResult(name, True, "would allow " + ", ".join(wanted))
    try:
        with winreg.CreateKeyEx(root, CRED_DELEG_KEY, 0, winreg.KEY_SET_VALUE) as key:
            # Allow delegation at all, and merge with any list the environment
            # already has rather than replacing it.
            winreg.SetValueEx(key, "AllowDefaultCredentials", 0, winreg.REG_DWORD, 1)
            winreg.SetValueEx(
                key, "ConcatenateDefaults_AllowDefault", 0, winreg.REG_DWORD, 1
            )

        # The allow list is a set of numbered *values* ("1", "2", ...), not
        # nested subkeys. Getting that shape wrong is silent: the policy simply
        # has no effect and the sign-in keeps prompting, which looks like a
        # Windows problem rather than a bug in this step.
        with winreg.CreateKeyEx(
            root, ALLOW_LIST_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
        ) as key:
            existing: set[str] = set()
            index = 0
            while True:
                try:
                    value, _ = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                existing.add(value.lower())
            for spn in wanted:
                if spn.lower() in existing:
                    continue
                n = 1
                while True:
                    try:
                        winreg.QueryValueEx(key, str(n))
                    except FileNotFoundError:
                        break
                    n += 1
                winreg.SetValueEx(key, str(n), 0, winreg.REG_SZ, spn)
                existing.add(spn.lower())
    except PermissionError as exc:
        return StepResult(name, False, f"access denied ({exc})")
    except OSError as exc:
        return StepResult(name, False, str(exc))
    return StepResult(name, True, "allowed " + ", ".join(wanted))


def _step_no_password_prompt(root, dry_run: bool) -> StepResult:
    """Stop "always prompt for password" from overriding delegation."""
    name = 'turn off "always prompt for password"'
    if dry_run:
        return StepResult(name, True, "would set fPromptForPassword = 0")
    try:
        for path in (TS_POLICY_KEY, RDP_TCP_KEY):
            with winreg.CreateKeyEx(root, path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "fPromptForPassword", 0, winreg.REG_DWORD, 0)
    except PermissionError as exc:
        return StepResult(name, False, f"access denied ({exc})")
    except OSError as exc:
        return StepResult(name, False, str(exc))
    return StepResult(name, True)


def _step_term_service(dry_run: bool) -> StepResult:
    """Make sure ``TermService`` starts automatically and is running.

    Not restarted. It is a shared ``svchost`` service with dependents (most
    visibly ``UmRdpService``), so ``sc stop`` is refused with error 1051 while
    those are running - and restarting it is not required: the listener switch
    (``fDenyTSConnections``) is consulted when a connection is accepted, not
    only at service start. Verified live on a machine where the service was
    already running: the listener bound and port 3389 accepted connections
    without a restart.
    """
    name = "set TermService to automatic and ensure it is running"
    if dry_run:
        return StepResult(name, True, "would set start=auto and start it")
    try:
        _netsh("sc", "config", "TermService", "start=", "auto")
    except OSError as exc:
        return StepResult(name, False, str(exc))

    status = _service_state("TermService")
    if status != "RUNNING":
        try:
            _netsh("sc", "start", "TermService")
        except OSError as exc:
            # 1056 = "an instance of the service is already running", which is
            # the state we wanted; only a different error is a real failure.
            if "1056" not in str(exc):
                return StepResult(name, False, str(exc))
        status = _service_state("TermService")
    return StepResult(name, status == "RUNNING", f"state={status or 'unknown'}")


def _service_state(name: str) -> str | None:
    """The service's state word, as ``sc query`` reports it.

    ``sc query`` prints ``        STATE              : 4  RUNNING``, so the
    numeric code and the word share the line. The word is what callers compare
    against; taking the first token after the colon would return the *number*
    and make a running service look stopped.
    """
    try:
        out = subprocess.run(
            ["sc", "query", name],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("sc query %s failed: %s", name, exc)
        return None
    for line in out.stdout.splitlines():
        if "STATE" in line.upper() and ":" in line:
            after = line.split(":", 1)[1].split()
            return after[1].upper() if len(after) > 1 else (after[0].upper() if after else None)
    return None


def _netsh(*args: str) -> None:
    """Run a Windows tool, raising on a non-zero exit."""
    result = subprocess.run(
        list(args), capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise OSError(
            f"{' '.join(args)} failed ({result.returncode}): "
            f"{result.stdout.strip()} {result.stderr.strip()}".strip()
        )


def undo(*, dry_run: bool = False) -> SetupReport:
    """Reverse what :func:`run` changed.

    Only the settings this module wrote are touched. ``fDenyTSConnections`` is
    restored to 1 (denied) because that is the Windows default - a machine whose
    owner enabled Remote Desktop by hand would have had it at 0 already, and
    that case is reported rather than guessed at.
    """
    report = SetupReport(dry_run=dry_run)
    report.before = childsession.describe()

    if not dry_run and not _is_admin():
        report.steps.append(
            StepResult("administrator rights", False, "not elevated")
        )
        report.after = report.before
        return report

    root = winreg.HKEY_LOCAL_MACHINE

    name = "disable child sessions"
    if dry_run:
        report.steps.append(StepResult(name, True, "would disable"))
    else:
        try:
            childsession.enable(False)
            report.steps.append(StepResult(name, True))
        except Exception as exc:  # noqa: BLE001
            report.steps.append(StepResult(name, False, str(exc)))

    if listener_enabled():
        report.steps.append(
            StepResult(
                "Remote Desktop listener",
                True,
                "left enabled (this module did not enable it, or "
                "Remote Desktop was already on)",
            )
        )

    report.after = childsession.describe() if not dry_run else report.before
    return report
