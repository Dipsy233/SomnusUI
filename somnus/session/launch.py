"""Launch and supervise the child session from Python.

This module is the Python half of the split described in :mod:`somnus.session.rdp`:
Python holds the policy and the agent, while the OLE container (the one thing
Python cannot do) is a small .NET helper. The helper is started here, its
stdout is parsed for the state transitions it reports, and it is stopped on the
way out.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from somnus.session import childsession

log = logging.getLogger("somnus.session.launch")


class HostMissing(RuntimeError):
    """The RDP host helper has not been built."""


def project_root() -> Path:
    """The repository root, found from this file's location."""
    return Path(__file__).resolve().parents[2]


def find_host() -> Path:
    """The newest built helper, or raise :class:`HostMissing`.

    Found by searching rather than by a fixed path. The output directory carries
    the target framework (``net8.0-windows10.0.22621.0`` since the WinRT UIA
    projection was needed), and it changes whenever the framework does. A
    hard-coded path silently kept running a *stale* build after that change -
    the symptom was the host rejecting an argument its source had supported for
    hours, which reads as "the flag does not work" rather than "the wrong exe
    ran".
    """
    root = project_root() / "tools" / "rdp-host" / "bin"
    if not root.is_dir():
        raise HostMissing(
            "the RDP host helper is not built; run:\n"
            f"  dotnet build {project_root() / 'tools' / 'rdp-host' / 'somnus-rdp-host.csproj'}"
        )
    candidates = [
        path
        for path in root.rglob("somnus-rdp-host.exe")
        # Skip the intermediate build dirs that also contain a copy.
        if "ref" not in path.parts and "refint" not in path.parts
    ]
    if not candidates:
        raise HostMissing(
            "the RDP host helper is not built; run:\n"
            f"  dotnet build {project_root() / 'tools' / 'rdp-host' / 'somnus-rdp-host.csproj'}"
        )
    # Prefer Debug when it is at least as new (the development default), else
    # the newest of whatever exists.
    debug = [p for p in candidates if "Debug" in p.parts]
    pool = debug or candidates
    return max(pool, key=lambda p: p.stat().st_mtime)


@dataclass
class ChildSessionRun:
    """A running child session, viewed from the main session."""

    #: Debug mode: a real, visible window the tester can watch.
    visible: bool = True
    width: int = 1280
    height: int = 800
    clipboard: bool = True
    #: Sign in automatically with the stored credential, so no one has to type
    #: at the RDP window. Off means the window shows a sign-in screen and waits.
    auto_logon: bool = False
    #: Seconds to wait for the helper to report the control is up.
    #:
    #: Not a timeout on the *logon*: with ``auto_logon`` off the person signs in
    #: at the window and may take as long as they take. This only bounds the wait
    #: for the helper to start and reach the sign-in screen.
    connect_timeout: float = 120.0
    #: Seconds to wait, after connecting, for the child session to appear.
    #: Meaningful only with ``auto_logon``: it is the wait for Windows to finish
    #: the logon and create the session.
    logon_timeout: float = 60.0

    _process: subprocess.Popen | None = field(default=None, repr=False)
    _state: str = field(default="idle", repr=False)
    _lines: list[str] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ready: threading.Event | None = field(default=None, repr=False)
    _reader: threading.Thread | None = field(default=None, repr=False)
    _child_session_id: int | None = field(default=None, repr=False)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "ChildSessionRun":
        """Start the helper and wait until it reports connected (or times out)."""
        status = childsession.describe()
        if not status["supported"]:
            raise RuntimeError(
                "child sessions are not enabled on this machine; run "
                "`somnus child-session setup` first (needs administrator rights "
                "and may require a reboot)"
            )

        helper = find_host()
        args = [str(helper), "--width", str(self.width), "--height", str(self.height)]
        args.append("--clipboard" if self.clipboard else "--no-clipboard")
        if not self.visible:
            args.append("--headless")
        if self.auto_logon:
            from somnus.session import credentials as creds

            if creds.exists():
                # Explicit fallback: the account password, when delegation is
                # refused by policy.
                credential = creds.load()
                args += ["--user", credential.username, "--password", credential.password]
            # With no stored credential, nothing is passed and the control
            # relies on delegation: Windows uses the credentials the user
            # already signed in with. This is the preferred path - no secret is
            # kept anywhere - and it is what makes the sign-in free.

        # Never log the argv verbatim: it carries the password when a stored
        # credential is in use.
        if self.auto_logon and "--password" in args:
            idx = args.index("--password")
            safe = args[: idx + 1] + ["<redacted>"]
        else:
            safe = args
        log.info("starting RDP host: %s", " ".join(safe))
        self._process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._start_reader()
        self._wait_for_ready(self.connect_timeout)
        if self.auto_logon:
            self._wait_for_child_session(self.logon_timeout)
        return self

    def _wait_for_child_session(self, timeout: float) -> None:
        """Wait for the host to report that a user is actually signed in.

        The signal is the host's ``STATE=logged-on``, which comes from the RDP
        control's ``OnLoginComplete`` event - the only thing that distinguishes a
        completed sign-in from a transport that merely came up. Everything else
        is a guess that has already been wrong here:

        * the control's ``Connected`` flag is 1 even when the far side stayed at
          a sign-in screen;
        * ``WTSGetChildSessionId`` keeps returning an id after a refused logon,
          for a session that does not exist.

        ``STATE=logon-failed`` carries the control's status code and is reported
        as a failure immediately rather than waited out.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.refresh_state()
            if state == "logged-on":
                # The event says a desktop exists; the id is read only now, when
                # it can be trusted, and cross-checked against the live list.
                live = childsession.live_child_session()
                self._child_session_id = live.session_id if live else None
                # From here the session is this process's to clean up, unless a
                # caller detaches it.
                from somnus.session import lifecycle

                lifecycle.claim(self._child_session_id)
                log.info("child session logged on (id %s)", self._child_session_id)
                return
            if state.startswith("logon-failed"):
                raise RuntimeError(
                    f"the child session sign-in was refused ({state}); "
                    "check the stored account and password "
                    "(`somnus child-session credential --save`)"
                )
            if not self.running:
                raise RuntimeError(
                    "the RDP window closed before a sign-in completed"
                )
            time.sleep(0.5)

        # Report what the host did say, so the failure is actionable.
        tail = "; ".join(line for line in self.output() if line.startswith("STATE="))
        raise RuntimeError(
            f"no sign-in completed within {timeout:.0f}s"
            + (f" (host reported: {tail})" if tail else "")
            + "; if the window is showing a sign-in screen, the stored password "
            "is missing or wrong"
        )

    @property
    def child_session_id(self) -> int | None:
        """The session id that came up, once it is known."""
        return getattr(self, "_child_session_id", None)

    def _start_reader(self) -> None:
        """Drain the helper's output on a thread and fold it into the state.

        A thread rather than polling: the helper keeps printing (sign-in
        progress, reconnects), and nothing else can read the pipe without
        racing this. The reader also means :meth:`refresh_state` never blocks on
        a read that has no data behind it - which a pipe read would.
        """
        import threading

        proc = self._process
        assert proc is not None and proc.stdout is not None
        stdout = proc.stdout
        self._ready = threading.Event()

        def pump() -> None:
            for raw in stdout:
                line = raw.strip()
                if not line:
                    continue
                with self._lock:
                    self._lines.append(line)
                    if line == "READY=1":
                        self._state = "ready"
                        self._ready.set()
                    elif line.startswith("STATE="):
                        self._state = line.split("=", 1)[1]
                self._ready.set()

        self._reader = threading.Thread(target=pump, name="rdp-host-reader", daemon=True)
        self._reader.start()

    def _wait_for_ready(self, timeout: float) -> None:
        """Wait until the helper reports READY, or reports a hard failure.

        Returns as soon as the helper is up, which is *before* anyone signs in:
        the sign-in is the user's to perform at the window. Waiting for
        ``connected`` here would block for as long as they take to type and
        would wrongly report a timeout when they simply have not started.
        """
        event = getattr(self, "_ready", None)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if event is not None and event.wait(0.2):
                with self._lock:
                    state = self._state
                if state == "failed":
                    raise RuntimeError(
                        "the RDP host could not create the session control; "
                        "see its output above"
                    )
                if state in ("ready", "connected", "connecting", "logging-in", "logged-on"):
                    return
                if state.startswith("logon-failed"):
                    # A refusal is a valid "the host is up" answer; the caller
                    # decides what to do with it (see _wait_for_child_session).
                    return
            proc = self._process
            if proc is not None and proc.poll() is not None:
                break
        if self.state == "idle":
            log.warning(
                "RDP host did not report READY within %.0fs; it may still be "
                "starting", timeout,
            )

    def refresh_state(self) -> str:
        """The latest state, without blocking.

        The reader thread has already folded any output into ``_state``; this
        only reads it under the lock, so a caller may poll for "has the user
        signed in yet" cheaply.
        """
        with self._lock:
            return self._state

    # -- state ---------------------------------------------------------------

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def connected(self) -> bool:
        return self.state == "connected"

    @property
    def running(self) -> bool:
        """Whether the helper process is still alive (the window is open)."""
        proc = self._process
        return proc is not None and proc.poll() is None

    def output(self) -> list[str]:
        """Every line the helper has printed, for diagnostics."""
        with self._lock:
            return list(self._lines)

    def stop(self) -> None:
        """Stop the helper, which ends the child session and its programs."""
        from somnus.session import lifecycle

        lifecycle.disown(self._child_session_id)
        proc = self._process
        self._process = None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log.warning("RDP host did not exit; killing it")
            proc.kill()

    def detach(self) -> None:
        """Let the helper keep running after this process exits.

        The child session lives exactly as long as its RDP connection does, so a
        caller that starts one and returns (the agent opening a session on its
        own) must not let the helper die with it. The reader thread and the pipe
        are released; the helper process is not.

        The session is also *disowned*: the exit hook only cleans up what this
        process still owns, and killing a session that was deliberately left
        running would undo the point of detaching.
        """
        from somnus.session import lifecycle

        lifecycle.disown(self._child_session_id)
        self._process = None
        self._reader = None
        ready, self._ready = self._ready, None
        if ready is not None:
            ready.set()

    def __enter__(self) -> "ChildSessionRun":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def enable_child_sessions(enable: bool = True) -> dict[str, object]:
    """Enable (or disable) the child-session feature flag, reporting what changed.

    This is the *single* ``wtsapi32`` step, kept here because it is the one a
    test may want in isolation. The full setup - listener, firewall, delegation,
    TermService - lives in :mod:`somnus.session.setup` and is what a real machine
    needs; enabling the flag alone leaves the listener denied and the session
    unable to connect.
    """
    before = childsession.describe()
    childsession.enable(enable)
    after = childsession.describe()
    return {
        "before": before,
        "after": after,
        "changed": before["supported"] != after["supported"],
        "reboot_may_be_needed": after["supported"] and after["child_session_id"] is None,
    }
