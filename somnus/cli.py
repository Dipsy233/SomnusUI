"""SomnusUI command line interface.

Commands::

    somnus windows                       list windows
    somnus record  <window> -o tree.json record a RawTree fixture
    somnus observe [window]              print the L0 SUI-Text observation
    somnus call    "CALL p1.add()"       run one action
    somnus trace show <dir>              print a recorded decision trace
    somnus doctor                        diagnose the self-check environment
    somnus eval                          run generic self-checks (runtime verdict)
    somnus replay <trace>                replay a trace, report the first divergence
    somnus heal <script>                 replay a run script, patch a failed step
    somnus repl                          interactive SUI-Text loop
    somnus bench   [--runs 100]          run the acceptance benchmark
    somnus mcp                           start the MCP server (stdio)
    somnus rpc                           start the JSON-RPC server (stdio)

Global options::

    --provider {auto,uia,mock}   select the accessibility provider
    --fixture PATH               load a recorded fixture into the mock provider
    --demo                       use the built-in restaurant demo fixtures
    --pack ID                    force an app pack
    --allow PROCESS              allow-list a process (repeatable)
    --allow-clipboard            permit the Ctrl+V channel for this run
    --allow-keyboard             permit synthetic typing for this run
    --config PATH                read settings from PATH

The two synthetic-input switches are ON by default and can be turned off for
one run (``--no-allow-clipboard`` / ``--no-allow-keyboard``) or persistently
(``somnus config --no-allow-clipboard``) - see :mod:`somnus.config`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

from somnus.errors import ErrorCode, SomnusError
from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session


def _load_fixture(path: str):
    from somnus.providers.mock import load_tree

    return load_tree(path)


def build_session(args: argparse.Namespace) -> Session:
    from somnus.config import load as load_settings
    from somnus.runtime.settle import Settler

    # Read once per session so the flag/config/env precedence is applied in one
    # place (see somnus/config.py).
    settings = load_settings(getattr(args, "config", None))
    provider_name = args.provider
    if args.demo:
        provider_name = "mock"

    if provider_name == "mock" or args.fixture or getattr(args, "scenario", None):
        trees = []
        scripts = {}
        callbacks = None
        if args.demo:
            from tests.fixtures.builders import demo_scripts, restaurant_demo_tree

            trees.append(restaurant_demo_tree())
            scripts = demo_scripts()
        if getattr(args, "scenario", None):
            # The Scenario Suite screens, replayed offline. This is what makes
            # the suite usable without Windows: the same trees and the same
            # scripted behaviour the acceptance app produces are available here.
            from tests.fixtures import scenarios as S

            screen = args.scenario
            if screen not in S.SCREENS:
                raise SomnusError(
                    ErrorCode.PARSE_ERROR,
                    message=(
                        f"unknown scenario {screen!r}; choose from "
                        f"{', '.join(sorted(S.SCREENS))}"
                    ),
                )
            builder, scripts_factory = S.SCREENS[screen]
            trees.append(builder())
            scripts = scripts_factory()
            callbacks = S.callbacks()
        for path in args.fixture or ():
            trees.append(_load_fixture(path))
        provider = MockProvider(
            fixtures=trees, scripts=scripts, callbacks=callbacks
        )
    else:
        from somnus.providers.base import get_provider

        provider = get_provider(provider_name)

    policy = Policy(
        allow_processes=tuple(args.allow or ()) or settings.allow_processes,
        # Synthetic input is on by default in this build (see
        # somnus/config.py): the flag is a one-run override in either
        # direction, `settings` is the persistent answer, and None means "let
        # the settings decide".
        allow_keyboard=(
            args.allow_keyboard
            if getattr(args, "allow_keyboard", None) is not None
            else settings.allow_keyboard
        ),
        # The clipboard channel borrows a resource that belongs to the user, so
        # it stays overridable per run even though it is on by default.
        allow_clipboard=(
            args.allow_clipboard
            if getattr(args, "allow_clipboard", None) is not None
            else settings.allow_clipboard
        ),
    )
    session = Session(
        provider,
        pack_id=args.pack,
        policy=policy,
        settler=Settler(
            quiet_ms=settings.settle_quiet_ms,
            poll_interval_ms=settings.settle_poll_ms,
            timeout_s=settings.settle_timeout_s,
            skip_when_deterministic=bool(getattr(provider, "is_deterministic", False)),
        ),
        allow_packs=not getattr(args, "no_pack", False),
    )
    # Session-scope switch, also updated later by ``configure``.
    session.post_action_enum = settings.post_action_enum
    # Environment tolerance for a blocked action (somnus/runtime/preflight.py).
    session.actionable_timeout_s = settings.actionable_timeout_s

    # Decision trace: ``--trace DIR`` for this run, else ``SOMNUS_TRACE``.
    # Traces are written outside the repository by default (see
    # somnus/observability/jsonl.py) and are never required for a normal run.
    trace_value = getattr(args, "trace", None)
    if trace_value is None:
        trace_value = os.environ.get("SOMNUS_TRACE")
    if trace_value is not None:
        from somnus.observability.jsonl import attach_trace

        sink = attach_trace(
            session,
            trace_value,
            settings=session._current_settings(),
            session_label=session.session_label,
        )
        if sink is not None:
            print(f"trace -> {sink.root}", file=sys.stderr)

    # Remember how to rebuild a local provider. ``Session.switch_session`` swaps
    # providers (only one may hold UIA state, so the old one is released), and
    # coming back to "main" therefore has to construct a fresh one rather than
    # reuse a closed one. Only for the real Windows provider: a mock session has
    # no "other desktop" to be the far side of.
    if provider_name in ("uia", "windows") and not args.demo:
        from somnus.providers.base import get_provider as _get_provider

        session._local_provider_factory = lambda: _get_provider(provider_name)

    return session


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_config(args: argparse.Namespace) -> int:
    """Show or edit the persistent settings.

    This is the command a person runs when they have decided the focus cost is
    acceptable, and the one an agent is pointed at when a route is refused: an
    opt-in that can only be expressed as a flag on a process nobody controls is
    not an opt-in.
    """
    from somnus import config as config_module

    settings = config_module.load(getattr(args, "config", None))

    if args.path:
        print(settings.path)
        return 0

    changed = False
    if args.set_clipboard is not None:
        settings.allow_clipboard = args.set_clipboard
        changed = True
    if args.set_keyboard is not None:
        settings.allow_keyboard = args.set_keyboard
        changed = True

    if changed:
        written = config_module.save(settings)
        print(f"wrote {written}")
    elif not settings.path.exists():
        # Saying "here is the file" when it does not exist is a dead end; write
        # the defaults so the user has something concrete to edit.
        written = config_module.save(settings)
        print(f"wrote {written} (defaults; both channels on)")

    print(settings.render())
    return 0


def cmd_child_session(args: argparse.Namespace) -> int:
    """Inspect, configure, and start the child session.

    Three verbs, deliberately separate:

    * ``status``  - read-only; safe to run anywhere, changes nothing.
    * ``setup``   - the one step that writes system settings and needs admin.
    * ``start``   - run the loopback, opening the RDP window (visible by
                    default, ``--headless`` to hide it).

    Keeping ``status`` free of side effects is the point: a user must be able to
    ask "is this ready?" without enabling anything.
    """
    from somnus.session import childsession
    from somnus.session.launch import ChildSessionRun, HostMissing

    action = getattr(args, "child_action", "status")

    if action == "status":
        status = childsession.describe()
        if not status["is_windows"]:
            print("child sessions: not available (this is not Windows)")
            return 1
        print(f"supported           : {status['supported']}")
        print(f"current session id  : {status['current_session_id']}")
        print(f"child session id    : {status['child_session_id']} (raw hint)")
        live = status.get("live_child_session_id")
        if live is None and status["child_session_id"] is not None:
            print("live child session  : none (the hint names a session that is gone)")
        else:
            print(f"live child session  : {live}")
        if live is not None:
            print(f"child session user  : {status.get('child_session_user')}")
        print(f"inside child session: {status['inside_child_session']}")
        if not status["supported"]:
            print("\nrun `somnus child-session setup` (needs administrator rights)")
        return 0

    if action == "setup":
        from somnus.session import setup as setup_module

        disable = getattr(args, "disable", False)
        dry_run = getattr(args, "dry_run", False)
        report = (
            setup_module.undo(dry_run=dry_run)
            if disable
            else setup_module.run(dry_run=dry_run)
        )
        for line in report.lines():
            print(line)
        print()
        print(f"supported before: {report.before.get('supported')}")
        print(f"supported after : {report.after.get('supported')}")
        if report.dry_run:
            print("\n(dry run - nothing was changed)")
        elif report.ok and report.after.get("child_session_id") is None:
            # The session itself is created on first connect, so an absent id is
            # expected here, not a sign that a reboot is needed.
            print(
                "\nSetup complete. Start the session with "
                "`somnus child-session start`."
            )
        elif not report.ok:
            print("\nSome steps failed; see above. Re-run elevated if denied.")
            return 1
        return 0

    if action == "peer":
        # The far half: a thin process inside the child session that owns a
        # Session and answers tool calls over the pipe. No model, no policy of
        # its own - just perception and actuation.
        project = getattr(args, "project", None)
        if project and project not in sys.path:
            # The Run key passes this: a startup entry runs with no working
            # directory, so the import path cannot be assumed.
            sys.path.insert(0, project)

        from somnus.session.bridge import PeerServer
        from somnus.session.childsession import current_session_id, inside_child_session

        if not inside_child_session():
            print(
                f"this is session {current_session_id()}, which is not a child "
                "session; the peer must run inside one so its windows are the "
                "ones the agent drives",
                file=sys.stderr,
            )
            return 1
        server = PeerServer(
            config_path=getattr(args, "config", None),
            pack_id=getattr(args, "pack", None),
            allow_packs=not getattr(args, "no_pack", False),
        )
        print(f"peer starting in session {current_session_id()} (Ctrl+C to stop)")
        try:
            return server.serve_forever()
        except KeyboardInterrupt:
            server.stop()
            return 0

    if action == "peer-setup":
        # Register the peer to start with the child session's own logon. A Run
        # key is what ParaDesk uses and for the same reason: the child session
        # is an ordinary interactive logon for this account, so it runs the key
        # with no elevation, and the user can see and disable it in Settings.
        from somnus.session.peerstart import register, status

        if getattr(args, "remove", False):
            ok, message = register(remove=True)
        else:
            ok, message = register(
                python=getattr(args, "python", None),
                project=getattr(args, "project", None),
            )
        print(message)
        info = status()
        if info:
            print(f"    {info}")
        return 0 if ok else 1

    if action == "peer-status":
        from somnus.session.peerstart import status

        info = status()
        print(info or "no peer is registered to start with the child session")
        return 0

    if action == "credential":
        from somnus.session import credentials as creds

        if getattr(args, "clear", False):
            removed = creds.clear()
            print("removed the stored credential" if removed else "nothing stored")
            return 0

        if getattr(args, "status", False):
            path = creds.credential_path()
            if not creds.exists():
                print(f"no stored credential ({path})")
                return 0
            try:
                c = creds.load()
                print(f"stored: {c.username}")
                print("password: <decryptable on this machine and account>")
            except creds.CredentialUnavailable as err:
                print(f"stored but unusable: {err}")
            print(f"path: {path}")
            return 0

        # Save. The password is read from the environment or prompted for - it
        # is never taken from argv, which every process on the machine can read.
        import getpass
        import os

        account = getattr(args, "user", None) or creds.current_account_name()
        password = os.environ.get("SOMNUS_CHILD_SESSION_PASSWORD")
        if not password:
            try:
                password = getpass.getpass(
                    f"password for {account} (the account password, not a PIN): "
                )
            except (EOFError, KeyboardInterrupt):
                print("cancelled", file=sys.stderr)
                return 1
        if not password:
            print("an empty password cannot be used for the loopback", file=sys.stderr)
            return 1
        path = creds.save(account, password)
        print(f"stored the credential for {account} in {path}")
        print(
            "It is DPAPI-encrypted for this machine and account. "
            "Remove it with `somnus child-session credential --clear`."
        )
        return 0

    if action == "close":
        from somnus.session import lifecycle

        report = lifecycle.cleanup()
        print(
            f"loopback stopped: {report.get('loopback_stopped')}, "
            f"peers stopped: {report.get('peers_stopped')}"
        )
        if report.get("reset_session") is not None:
            print(
                f"cleared leftover session {report['reset_session']} "
                f"(ok={report.get('reset_ok')})"
            )
        from somnus.session import childsession

        live = childsession.live_child_session()
        print("child session: " + (f"{live.session_id} still present" if live else "none"))
        return 0

    if action == "probe":
        from somnus.session.bridge import PeerBridge

        bridge = PeerBridge()
        try:
            if not bridge.connect():
                print("no peer is listening on the child session")
                return 1
            response = bridge.request({"method": "ping"})
            print(f"peer answered: {response}")
            windows = bridge.windows()
            print(f"peer sees {len(windows)} window(s):")
            for w in windows[:20]:
                print(f"  {w['window_id']} {w['process']} {w['title']!r}")
        finally:
            bridge.close()
        return 0

    if action == "run":
        return _child_run(args)

    if action == "start":
        visible = not getattr(args, "headless", False)
        auto = getattr(args, "auto", False)
        run = ChildSessionRun(
            visible=visible,
            width=getattr(args, "width", 1280),
            height=getattr(args, "height", 800),
            clipboard=not getattr(args, "no_clipboard", False),
            auto_logon=auto,
            connect_timeout=getattr(args, "timeout", 120.0),
            logon_timeout=getattr(args, "logon_timeout", 60.0),
        )
        try:
            run.start()
        except HostMissing as err:
            print(str(err), file=sys.stderr)
            return 1
        except RuntimeError as err:
            print(str(err), file=sys.stderr)
            for line in run.output():
                print(f"  host: {line}", file=sys.stderr)
            return 1

        if auto:
            # Nothing to wait for from a person: report and return, leaving the
            # window open in the background. Callers that want the session gone
            # stop the process themselves (or call `child-session close`).
            print(f"child session {run.child_session_id} is up (auto-logon).")
            print("The RDP window stays open; close it to end the session.")
            for line in run.output():
                print(f"  host: {line}")
            if getattr(args, "keep_alive", False):
                try:
                    while run.running:
                        time.sleep(1.0)
                except KeyboardInterrupt:
                    pass
                finally:
                    run.stop()
            else:
                # Detach: the caller gets the id and the process is left running.
                run.detach()
            return 0

        print(f"RDP window open ({'visible' if visible else 'hidden'}).")
        print("Sign in to the child session in that window; it stays open until")
        print("you press Ctrl+C here.")
        for line in run.output():
            print(f"  host: {line}")

        # Wait for the session, reporting the transition when it happens. The
        # sign-in is the user's, so this polls rather than blocking on it; the
        # loop only ends on Ctrl+C or the window closing.
        try:
            last = run.state
            while True:
                time.sleep(1.0)
                state = run.refresh_state()
                if state != last:
                    last = state
                    print(f"child session: {state}")
                if not run.running:
                    print("the RDP window was closed")
                    break
        except KeyboardInterrupt:
            pass
        finally:
            run.stop()
        return 0

    print(f"unknown child-session action: {action}", file=sys.stderr)
    return 2


def _child_run(args: argparse.Namespace) -> int:
    """Run one command on the child desktop and report its exit code + output.

    The command is started *by the peer*, i.e. by a process that already lives in
    the child session. A program launched there inherits that session's window
    station and desktop without any privilege: no ``CreateProcessAsUser``, no
    token duplication, no elevation. That is the supported way to put test
    windows on the child desktop.

    Output is redirected to files under ``%TEMP%\\somnusui-child-run``; the last
    run's directory is kept and printed, older ones are removed.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from somnus.session import childsession, lifecycle
    from somnus.session.bridge import PeerBridge
    from somnus.session.launch import ChildSessionRun, HostMissing

    command = list(getattr(args, "command", []) or [])
    while command and command[0] == "--":
        command = command[1:]
    if not command:
        print(
            "usage: somnus child-session run [--cwd DIR] [--timeout S] -- "
            "CMD [ARGS...]",
            file=sys.stderr,
        )
        return 2

    timeout_s = float(getattr(args, "timeout", 600.0))
    if timeout_s <= 0:
        print("--timeout must be positive", file=sys.stderr)
        return 2
    cwd = getattr(args, "cwd", None) or None
    if cwd is not None and not Path(cwd).is_dir():
        print(f"cwd is not a directory: {cwd}", file=sys.stderr)
        return 2

    live = childsession.live_child_session()
    if live is None:
        print(
            "child session: none open; starting one (auto-logon, headless)...",
            file=sys.stderr,
        )
        run = ChildSessionRun(visible=False, auto_logon=True)
        try:
            run.start()
        except HostMissing as err:
            print(str(err), file=sys.stderr)
            return 1
        except RuntimeError as err:
            print(f"cannot open the child session: {err}", file=sys.stderr)
            return 1
        if run.child_session_id is None:
            print("child session did not complete its logon", file=sys.stderr)
            return 1
        # This process now owns it; the exit hook closes it unless it is detached.
        lifecycle.claim(run.child_session_id)
        run.detach()

    bridge = PeerBridge(connect_timeout=60.0, call_timeout=max(30.0, timeout_s + 30.0))
    if not bridge.connect():
        print(
            "no peer is listening in the child session; register it with "
            "`somnus child-session peer-setup` and reopen the session",
            file=sys.stderr,
        )
        return 1

    base = Path(tempfile.gettempdir()) / "somnusui-child-run"
    base.mkdir(parents=True, exist_ok=True)
    run_dir = base / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    for old in list(base.iterdir()):
        if old.is_dir() and old != run_dir:
            shutil.rmtree(old, ignore_errors=True)
    stdout_path = str(run_dir / "stdout.txt")
    stderr_path = str(run_dir / "stderr.txt")

    try:
        response = bridge.request(
            {
                "method": "process.start",
                "params": {
                    "command": command[0],
                    "args": command[1:],
                    "cwd": cwd,
                    "stdout_path": stdout_path,
                    "stderr_path": stderr_path,
                },
            }
        )
        if "error" in response:
            error = response["error"]
            print(
                f"ERR {error.get('code', 'NATIVE_EXECUTION_FAILED')} "
                f"{error.get('message', '')}",
                file=sys.stderr,
            )
            return 1
        pid = int((response.get("result") or {}).get("pid"))

        started = time.monotonic()
        timed_out = False
        exit_code: int | None = None
        while True:
            status = bridge.request(
                {"method": "process.status", "params": {"pid": pid}}
            )
            if "error" in status:
                error = status["error"]
                print(
                    f"ERR {error.get('code', 'NATIVE_EXECUTION_FAILED')} "
                    f"{error.get('message', '')}",
                    file=sys.stderr,
                )
                return 1
            result = status.get("result") or {}
            if not result.get("alive"):
                exit_code = result.get("exit_code")
                break
            if time.monotonic() - started > timeout_s:
                timed_out = True
                kill = bridge.request(
                    {"method": "process.kill", "params": {"pid": pid}}
                )
                killed = "error" not in kill and (kill.get("result") or {}).get("killed")
                # Give the OS a moment to reap, then take the final state.
                for _ in range(10):
                    time.sleep(0.2)
                    final = bridge.request(
                        {"method": "process.status", "params": {"pid": pid}}
                    )
                    if "error" not in final and not (final.get("result") or {}).get("alive"):
                        exit_code = (final.get("result") or {}).get("exit_code")
                        break
                print(
                    f"timeout after {timeout_s:.1f}s; kill="
                    f"{'ok' if killed else 'FAILED'}",
                    file=sys.stderr,
                )
                break
            time.sleep(0.2)
    finally:
        bridge.close()

    elapsed = time.monotonic() - started

    def _read(path: str) -> str:
        """Read a redirected stream without assuming the child's encoding.

        A Python child whose stdout is a file uses the locale encoding (cp936
        here), not UTF-8; reading with one fixed codec turned a correct
        observation into mojibake. Try UTF-8 first (the common case for tools
        that emit it), then fall back to the platform's default encoding.
        """
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            return f"<cannot read {path}: {exc}>"
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            # The child's stdout is a file, so its Python uses the Windows ANSI
            # code page (cp936 here). Ask Windows for that code page rather than
            # this process's locale, which PowerShell 7 may have set to UTF-8.
            try:
                import ctypes

                acp = int(ctypes.windll.kernel32.GetACP())
                return data.decode(f"cp{acp}", errors="replace")
            except (AttributeError, OSError, ValueError):
                import locale

                return data.decode(
                    locale.getpreferredencoding(False), errors="replace"
                )

    stdout_text = _read(stdout_path)
    stderr_text = _read(stderr_path)
    print(
        f"child-session run: pid={pid} exit={exit_code} "
        f"timeout={'yes' if timed_out else 'no'} elapsed={elapsed:.1f}s"
    )
    print(f"stdout [{stdout_path}]:")
    if stdout_text:
        print(stdout_text, end="" if stdout_text.endswith("\n") else "\n")
    else:
        print("(empty)")
    print(f"stderr [{stderr_path}]:")
    if stderr_text:
        print(stderr_text, end="" if stderr_text.endswith("\n") else "\n")
    else:
        print("(empty)")

    if timed_out:
        return 124
    return int(exit_code) if exit_code is not None else 1


def cmd_windows(args: argparse.Namespace) -> int:
    session = build_session(args)
    for w in session.refresh_windows():
        flag = " *" if w.active else ""
        print(f"{w.window_id} {w.process} {w.title!r}{flag}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from somnus.providers.mock import save_tree

    session = build_session(args)
    session.refresh_windows()
    if args.window:
        session.target_window = args.window
    tree = session._snapshot()
    out = save_tree(tree, args.output)
    print(f"recorded {out} ({sum(1 for _ in tree.walk())} nodes)")
    return 0


def cmd_observe(args: argparse.Namespace) -> int:
    session = build_session(args)
    try:
        text = session.observe(args.window).text
        if args.out:
            # Large-result offload: the observation goes to the file, stdout
            # only carries the receipt, so an agent can save the full tree and
            # page through it with its own tools instead of a terminal buffer.
            from pathlib import Path

            from somnus.projection.budget import count_tokens

            Path(args.out).write_text(text, encoding="utf-8")
            lines = text.count("\n") + (0 if (not text or text.endswith("\n")) else 1)
            print(f"wrote {args.out} ({lines} lines, {count_tokens(text)} tokens)")
            return 0
        print(text)
        return 0
    finally:
        # A one-shot command releases its session here, not at process exit:
        # the trace sink is buffered, so a following `trace to-script` (and
        # any other reader) needs the flush that close() performs.
        session.close()


def cmd_call(args: argparse.Namespace) -> int:
    session = build_session(args)
    try:
        session.observe()
        text = session.dispatch(args.action)
        print(text)
        return 0 if not text.startswith("ERR") else 1
    finally:
        # Same as cmd_observe: flush the trace deterministically.
        session.close()


def cmd_help(args: argparse.Namespace) -> int:
    """Print the discovery summary or one topic page.

    The same text backs the ``help`` tool, so a CLI user and a model cannot be
    told different grammars.
    """
    from somnus.protocol.help import render_help

    print(render_help(args.topic))
    return 0


def cmd_trace(args: argparse.Namespace) -> int:
    """Print a recorded trace as a readable timeline."""
    from somnus.observability.view import render_timeline

    try:
        text = render_timeline(
            args.path, snapshots=args.snapshots, limit=args.limit
        )
    except OSError as exc:
        print(f"ERR TRACE_READ {exc}", file=sys.stderr)
        return 1
    print(text)
    return 0


def cmd_trace_to_script(args: argparse.Namespace) -> int:
    """Distil a recorded trace into a ``run``-script draft."""
    from pathlib import Path

    from somnus.selfcheck.distill_script import distill_script

    try:
        report = distill_script(args.path, await_timeout=args.await_timeout)
    except OSError as exc:
        print(f"ERR TRACE_READ {exc}", file=sys.stderr)
        return 1
    except SomnusError as exc:
        print(exc.render(), file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(report.script, encoding="utf-8")
        print(
            f"wrote {args.out} ({report.distilled} steps distilled, "
            f"{report.skipped} need review)"
        )
    else:
        print(report.script, end="")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Diagnose the environment, then optionally smoke observe/find/call.

    The smoke follows the self-check sandbox rule: fixtures offline, the child
    session by default, the user's desktop only with ``--real``. The command
    itself never opens a child session - it reports whether one is live and
    what to run if it is not.
    """
    from somnus import config as config_module
    from somnus.selfcheck import doctor, evaluate
    from somnus.selfcheck.sandbox import SandboxError, enter

    offline = _offline_mode(args)
    if offline and args.real:
        print(
            "ERR INVALID_ARGUMENT hint=fix-syntax msg=\"--real cannot be used "
            "with an offline provider (mock/scenario fixture)\"",
            file=sys.stderr,
        )
        return 2

    settings = config_module.load(getattr(args, "config", None))
    env = doctor.diagnose(offline=offline, real=args.real, settings=settings)

    smoke = None
    if not args.no_smoke and env.can_smoke:
        session = build_session(args)
        try:
            if offline:
                desktop = "offline"
            else:
                desktop = enter(session, real=args.real).desktop
            smoke = evaluate.run(session, desktop=desktop)
        except SandboxError as err:
            env.lines.append(doctor.CheckLine("sandbox", doctor.FAIL, err.message))
            env.can_smoke = False
            env.smoke_skip = err.message
        finally:
            session.close()

    print(doctor.render(env, smoke))
    failed = env.failed or (smoke is not None and not smoke.ok)
    return 1 if failed else 0


def cmd_eval(args: argparse.Namespace) -> int:
    """Run the generic self-check and print a JSON verdict.

    The tasks and the verdict are model-free: observe, find and call are driven
    by this process, and success is read from the session's compiled result and
    call outcomes. The default desktop is the child session; fixtures are the
    offline path; ``--real`` is the explicit opt-in to the user's desktop.
    """
    from somnus.selfcheck import evaluate
    from somnus.selfcheck.sandbox import SandboxError, enter

    offline = _offline_mode(args)
    if offline and args.real:
        print(
            "ERR INVALID_ARGUMENT hint=fix-syntax msg=\"--real cannot be used "
            "with an offline provider (mock/scenario fixture)\"",
            file=sys.stderr,
        )
        return 2

    session = build_session(args)
    report = None
    try:
        _ensure_trace(session, args)
        if offline:
            desktop = "offline"
        else:
            try:
                desktop = enter(session, real=args.real).desktop
            except SandboxError as err:
                print(err.render(), file=sys.stderr)
                return 1
        report = evaluate.run(session, desktop=desktop)
    finally:
        trace = _trace_where(session)
        session.close()

    print(evaluate.render_json(report, trace=trace))
    return 0 if report.ok else 1


def cmd_replay(args: argparse.Namespace) -> int:
    """Replay a trace on the sandbox desktop and name the first divergence."""
    from somnus.observability.view import load_events, trace_path
    from somnus.selfcheck import replay
    from somnus.selfcheck.sandbox import SandboxError, enter

    offline = _offline_mode(args)
    if offline and args.real:
        print(
            "ERR INVALID_ARGUMENT hint=fix-syntax msg=\"--real cannot be used "
            "with an offline provider (mock/scenario fixture)\"",
            file=sys.stderr,
        )
        return 2

    try:
        events = load_events(args.path)
    except OSError as exc:
        print(f"ERR TRACE_READ {exc}", file=sys.stderr)
        return 1
    if not any(record.get("event") == "call.start" for record in events):
        print(f"ERR TRACE_EMPTY {trace_path(args.path)} has no call to replay",
              file=sys.stderr)
        return 1

    session = build_session(args)
    report = None
    try:
        _ensure_trace(session, args)
        if offline:
            desktop = "offline"
        else:
            try:
                desktop = enter(session, real=args.real).desktop
            except SandboxError as err:
                print(err.render(), file=sys.stderr)
                return 1
        report = replay.replay_trace(
            session,
            args.path,
            desktop=desktop,
            trace_out=_trace_where(session),
        )
    finally:
        report_out = report
        session.close()

    if args.json:
        print(
            json.dumps(
                report_out.as_dict(), ensure_ascii=False, indent=2, default=str
            )
        )
    else:
        _print_replay(report_out)
    return 0 if report_out.ok else 1


def _print_replay(report) -> None:
    print(
        f"replay {report.trace} desktop={report.desktop} "
        f"provider={report.provider} steps={len(report.steps)}"
    )
    for step in report.steps:
        mark = "OK" if step.actual_status == "OK" else step.actual_status
        source = (
            f"{step.recorded_ref}->{step.resolved_ref}"
            if step.resolved and step.resolved_ref != step.recorded_ref
            else step.recorded_ref
        )
        print(
            f"  {step.index}. CALL {source}.{step.capability}() "
            f"-> {mark}"
            + (f" ({step.detail})" if step.detail and not step.divergence else "")
        )
        if step.output and step.divergence:
            print(f"     {step.output}")
    first = report.diverged
    if first is not None:
        print(
            f"DIVERGED step={first.index} kind={first.divergence} "
            f"expected={first.expected_status} actual={first.actual_status}"
        )
    else:
        print(f"PASS steps={len(report.steps)}")
    if report.trace_out:
        print(f"trace -> {report.trace_out}")


def cmd_heal(args: argparse.Namespace) -> int:
    """Replay a ``run`` script on the sandbox desktop and patch one failed step."""
    from pathlib import Path

    from somnus.selfcheck import heal
    from somnus.selfcheck.sandbox import SandboxError, enter

    offline = _offline_mode(args)
    if offline and args.real:
        print(
            "ERR INVALID_ARGUMENT hint=fix-syntax msg=\"--real cannot be used "
            "with an offline provider (mock/scenario fixture)\"",
            file=sys.stderr,
        )
        return 2

    try:
        script = Path(args.script).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"ERR SCRIPT_READ {exc}", file=sys.stderr)
        return 1

    session = build_session(args)
    report = None
    try:
        _ensure_trace(session, args)
        if offline:
            desktop = "offline"
        else:
            try:
                desktop = enter(session, real=args.real).desktop
            except SandboxError as err:
                print(err.render(), file=sys.stderr)
                return 1
        window = heal.window_ref(session, args.window)
        if window is None:
            target = f" {args.window}" if args.window else ""
            print(
                f'ERR TARGET_NOT_FOUND{target} hint=observe msg="no such '
                'window on this desktop"',
                file=sys.stderr,
            )
            return 1
        # Observe before the script runs: the session's records for these refs
        # are the recipes the healer needs when a later step goes stale.
        session.observe(window)
        try:
            report = heal.heal_script(
                session,
                script,
                source=args.script,
                desktop=desktop,
                retry=not args.no_retry,
                trace_out=_trace_where(session),
            )
        except SomnusError as err:
            print(err.render(), file=sys.stderr)
            return 1
    finally:
        report_out = report
        session.close()

    if args.json:
        print(
            json.dumps(
                report_out.as_dict(), ensure_ascii=False, indent=2, default=str
            )
        )
    else:
        print(heal.render(report_out))
    return 0 if report_out.ok else 1


def _offline_mode(args: argparse.Namespace) -> bool:
    """Whether this command should use fixtures rather than a live desktop.

    ``--demo`` / ``--scenario`` / ``--fixture`` / ``--provider mock`` are the
    explicit offline forms. A bare run is live, so an accidental missing flag
    cannot silently turn a live check into a fixture replay.
    """
    return bool(
        getattr(args, "demo", False)
        or getattr(args, "scenario", None)
        or getattr(args, "fixture", None)
        or getattr(args, "provider", "auto") == "mock"
    )


def _ensure_trace(session, args: argparse.Namespace) -> None:
    """Turn tracing on for a self-check unless the caller explicitly turned it off.

    Evidence is the point of these commands: the report says what happened, and
    the trace says why. The sink is still the same one the runtime uses, written
    outside the repository; ``--no-trace`` (or an explicit ``--trace 0`` /
    ``SOMNUS_TRACE=0``) keeps it off.
    """
    if getattr(args, "no_trace", False):
        if session.trace_sink.enabled:
            from somnus.observability.trace import NullTraceSink

            session.trace_sink.close()
            session.trace_sink = NullTraceSink()
        return
    if session.trace_sink.enabled:
        return
    if getattr(args, "trace", None) is not None:
        return
    if os.environ.get("SOMNUS_TRACE"):
        return
    from somnus.observability.jsonl import attach_trace

    sink = attach_trace(
        session,
        "1",
        settings=session._current_settings(),
        session_label=session.session_label,
    )
    if sink is not None:
        print(f"trace -> {sink.root}", file=sys.stderr)


def _trace_where(session) -> str | None:
    root = getattr(session.trace_sink, "root", None)
    return str(root) if root is not None else None


def cmd_repl(args: argparse.Namespace) -> int:
    session = build_session(args)
    # Start from the desktop: the window list is the entry point of every
    # workflow (pick a window, then observe it), so the REPL opens on that
    # instead of whatever window happens to have focus.
    print(session.observe(windows_only=True).text)
    try:
        while True:
            line = input("sui> ").strip()
            if not line:
                continue
            if line in ("quit", "exit", ":q"):
                break
            print(session.dispatch(line))
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


def cmd_bench(args: argparse.Namespace) -> int:
    from bench.run import run_benchmark

    return run_benchmark(args)


def cmd_mcp(args: argparse.Namespace) -> int:
    from somnus.protocol.mcp_server import main as mcp_main

    return mcp_main([args.provider])


def cmd_rpc(args: argparse.Namespace) -> int:
    from somnus.protocol.jsonrpc import JsonRpcServer

    session = build_session(args)
    JsonRpcServer(session).serve()
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="somnus", description="SomnusUI")
    parser.add_argument(
        "--provider",
        default="auto",
        choices=["auto", "uia", "mock"],
        help="accessibility source: uia (Windows), mock (fixtures)",
    )
    parser.add_argument("--fixture", action="append", default=None)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--scenario",
        default=None,
        metavar="NAME",
        help=(
            "replay one Scenario Suite screen offline through the mock provider "
            "(store | form | chat | console | tree). This is the test app the "
            "acceptance suite drives, available without Windows"
        ),
    )
    parser.add_argument("--pack", default=None)
    parser.add_argument(
        "--no-pack",
        action="store_true",
        help=(
            "compile every window generically; never apply an App Pack. Use this "
            "to drive an application the runtime has no pack for."
        ),
    )
    parser.add_argument("--allow", action="append", default=None)
    parser.add_argument(
        "--config",
        default=None,
        metavar="PATH",
        help=(
            "settings file to read (default: the platform config directory, "
            "see `somnus config --path`). Settings are the persistent form of "
            "--allow-clipboard / --allow-keyboard; a flag still wins for one run"
        ),
    )
    parser.add_argument(
        "--trace",
        nargs="?",
        const="1",
        default=None,
        metavar="DIR",
        help=(
            "record a decision trace (routes, settle, delta, verify, snapshots) "
            "into DIR. Omit DIR to use a fresh run directory under the platform "
            "data dir (outside the repository, never git-tracked). Env: "
            "SOMNUS_TRACE. Read one with `somnus trace show DIR`"
        ),
    )
    parser.add_argument(
        "--allow-keyboard",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "permit synthetic keyboard input (SendInput) for elements that "
            "publish no usable interface. ON by default; use "
            "--no-allow-keyboard to report such an element instead of typing "
            "into it. Persist with "
            "`somnus config --allow-keyboard` / `--no-allow-keyboard`"
        ),
    )
    parser.add_argument(
        "--allow-clipboard",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "permit the clipboard channel (Ctrl+V) for elements that publish "
            "no usable interface and map no HWND. ON by default; use "
            "--no-allow-clipboard to refuse it. It needs the target in the "
            "foreground. The user's clipboard is saved and restored format "
            "by format - bitmaps and file lists included - so nothing is "
            "lost; writes are marked to stay out of clipboard history and "
            "cloud sync. Persist with `somnus config --allow-clipboard`"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("windows").set_defaults(func=cmd_windows)

    p = sub.add_parser(
        "config",
        help="show or edit the persistent settings (the opt-in switches live here)",
    )
    p.add_argument(
        "--path",
        action="store_true",
        help="print only the settings file path, for scripting",
    )
    p.add_argument(
        "--allow-clipboard",
        dest="set_clipboard",
        action="store_const",
        const=True,
        default=None,
        help="persist allow_clipboard = true",
    )
    p.add_argument(
        "--no-allow-clipboard",
        dest="set_clipboard",
        action="store_const",
        const=False,
        help="persist allow_clipboard = false",
    )
    p.add_argument(
        "--allow-keyboard",
        dest="set_keyboard",
        action="store_const",
        const=True,
        default=None,
        help="persist allow_keyboard = true",
    )
    p.add_argument(
        "--no-allow-keyboard",
        dest="set_keyboard",
        action="store_const",
        const=False,
        help="persist allow_keyboard = false",
    )
    p.set_defaults(func=cmd_config)

    p = sub.add_parser(
        "child-session",
        help=(
            "run the agent in a second Windows session (own input desktop and "
            "foreground), so synthetic input cannot disturb the user"
        ),
    )
    csub = p.add_subparsers(dest="child_action", required=True)

    csub.add_parser(
        "status",
        help="report whether child sessions are available (changes nothing)",
    ).set_defaults(func=cmd_child_session)

    setup_parser = csub.add_parser(
        "setup",
        help=(
            "enable the child-session feature; needs administrator rights and "
            "may require a reboot"
        ),
    )
    setup_parser.add_argument(
        "--disable",
        action="store_true",
        help="turn the feature back off instead of on",
    )
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would change without writing anything",
    )
    setup_parser.set_defaults(func=cmd_child_session)

    start_parser = csub.add_parser(
        "start",
        help="open the RDP loopback to the child session",
    )
    start_parser.add_argument(
        "--headless",
        action="store_true",
        help="hide the RDP window (it is visible by default, for testing)",
    )
    start_parser.add_argument("--width", type=int, default=1280)
    start_parser.add_argument("--height", type=int, default=800)
    start_parser.add_argument(
        "--no-clipboard",
        action="store_true",
        help="do not share the clipboard between the sessions",
    )
    start_parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="seconds to wait for the session to connect",
    )
    start_parser.add_argument(
        "--auto",
        action="store_true",
        help=(
            "sign in with the stored credential instead of showing a sign-in "
            "screen, so no one has to type; needs "
            "`child-session credential --save` first"
        ),
    )
    start_parser.add_argument(
        "--logon-timeout",
        type=float,
        default=60.0,
        help="seconds to wait for the child session to appear (with --auto)",
    )
    start_parser.add_argument(
        "--keep-alive",
        action="store_true",
        help="with --auto, keep this process running instead of detaching",
    )
    start_parser.set_defaults(func=cmd_child_session)

    cred_parser = csub.add_parser(
        "credential",
        help=(
            "store or remove the account password the loopback signs in with "
            "(DPAPI-encrypted for this machine and account)"
        ),
    )
    cred_parser.add_argument(
        "--save",
        action="store_true",
        help=(
            "store the password (this is the default action; the flag exists "
            "so the command reads the same as it is documented)"
        ),
    )
    cred_parser.add_argument(
        "--user",
        default=None,
        help="account name (default: the signed-in account as MACHINE\\user)",
    )
    cred_parser.add_argument(
        "--status",
        action="store_true",
        help="report whether a credential is stored, and for whom",
    )
    cred_parser.add_argument(
        "--clear",
        action="store_true",
        help="delete the stored credential",
    )
    cred_parser.set_defaults(func=cmd_child_session)

    peer_parser = csub.add_parser(
        "peer",
        help=(
            "run the far half inside the child session: answers the agent's "
            "tool calls over a pipe (no model, no planning - just hands and eyes)"
        ),
    )
    peer_parser.add_argument(
        "--project",
        default=None,
        help=(
            "project directory to put on sys.path. The Run key passes this "
            "because a startup entry has no working directory"
        ),
    )
    peer_parser.set_defaults(func=cmd_child_session)

    peer_setup = csub.add_parser(
        "peer-setup",
        help=(
            "start the peer automatically with the child session's logon "
            "(writes a per-user Run key; no administrator rights)"
        ),
    )
    peer_setup.add_argument(
        "--remove",
        action="store_true",
        help="delete the Run entry instead of writing it",
    )
    peer_setup.add_argument(
        "--python",
        default=None,
        help="interpreter to record (default: this one, preferring pythonw)",
    )
    peer_setup.add_argument(
        "--project",
        default=None,
        help="project directory to record (default: this checkout)",
    )
    peer_setup.set_defaults(func=cmd_child_session)

    csub.add_parser(
        "close",
        help=(
            "stop the loopback and clear anything a previous attempt left "
            "behind (a stale loopback process, an orphaned peer, a session "
            "left over from a refused logon)"
        ),
    ).set_defaults(func=cmd_child_session)

    csub.add_parser(
        "peer-status",
        help="show whether the peer is registered to start with the child session",
    ).set_defaults(func=cmd_child_session)

    csub.add_parser(
        "probe",
        help="ask the peer what it can see (a smoke test for the pipe)",
    ).set_defaults(func=cmd_child_session)

    run_parser = csub.add_parser(
        "run",
        help=(
            "run a command on the child desktop through the peer, then print "
            "its exit code and full stdout/stderr (output files are kept under "
            "%%TEMP%%\\somnusui-child-run)"
        ),
    )
    run_parser.add_argument(
        "--cwd",
        default=None,
        help="working directory for the command (default: the peer's own)",
    )
    run_parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="seconds to wait before killing the command (default 600)",
    )
    run_parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="the command and its arguments, after `--`",
    )
    run_parser.set_defaults(func=cmd_child_session)

    p = sub.add_parser("help", help="print the discovery summary or one topic page")
    p.add_argument(
        "topic",
        nargs="?",
        default=None,
        help="format, gestures, batching, desktops, errors or settings",
    )
    p.set_defaults(func=cmd_help)

    p = sub.add_parser("record")
    p.add_argument("window", nargs="?")
    p.add_argument("-o", "--output", default="fixture.json")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("trace", help="inspect a recorded decision trace")
    tsub = p.add_subparsers(dest="trace_action", required=True)
    show = tsub.add_parser(
        "show",
        help=(
            "print the event timeline; with --snapshots also print the "
            "before/after SUI-Text bodies"
        ),
    )
    show.add_argument("path", help="trace directory, or one of its .jsonl files")
    show.add_argument("--snapshots", action="store_true")
    show.add_argument("--limit", type=int, default=None)
    show.set_defaults(func=cmd_trace)

    toscript = tsub.add_parser(
        "to-script",
        help=(
            "distil a recorded trace into a run-script draft: successful "
            "CALL steps become AWAIT \"name\" + CALL $it.cap(args); steps "
            "the trace cannot name become # TODO comments"
        ),
    )
    toscript.add_argument("path", help="trace directory, or one of its .jsonl files")
    toscript.add_argument(
        "--out",
        metavar="PATH",
        help="write the script to a file (stdout otherwise)",
    )
    toscript.add_argument(
        "--await-timeout",
        type=float,
        default=0.0,
        help=(
            "timeout=s on each generated AWAIT (default 0: one search, the "
            "element was there when the step ran)"
        ),
    )
    toscript.set_defaults(func=cmd_trace_to_script)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)
    p.add_argument(
        "--real",
        action="store_true",
        help=(
            "run the smoke on the user's main desktop. The default is the child "
            "session (or the offline fixture when --demo/--scenario is used)"
        ),
    )
    p.add_argument(
        "--no-smoke",
        action="store_true",
        help="report the environment only; do not drive any desktop",
    )

    p = sub.add_parser("eval")
    p.set_defaults(func=cmd_eval)
    p.add_argument(
        "--real",
        action="store_true",
        help="run on the user's main desktop instead of the child session",
    )
    p.add_argument(
        "--no-trace",
        action="store_true",
        help="do not record a trace (one is written automatically otherwise)",
    )

    p = sub.add_parser("replay")
    p.set_defaults(func=cmd_replay)
    p.add_argument("path", help="trace directory, or one of its .jsonl files")
    p.add_argument(
        "--real",
        action="store_true",
        help=(
            "replay against the user's main desktop. The default is the child "
            "session; offline traces replay against --demo/--scenario fixtures"
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="print the replay report as JSON instead of a timeline",
    )
    p.add_argument(
        "--no-trace",
        action="store_true",
        help="do not record a trace of the replay (one is written otherwise)",
    )

    p = sub.add_parser("heal")
    p.set_defaults(func=cmd_heal)
    p.add_argument(
        "script",
        help="a file holding a run script (CALL / AWAIT lines, one per step)",
    )
    p.add_argument(
        "--window",
        default=None,
        help="target window ref or title; default is the foreground window",
    )
    p.add_argument(
        "--real",
        action="store_true",
        help=(
            "heal against the user's main desktop. The default is the child "
            "session; offline scripts heal against --demo/--scenario fixtures"
        ),
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="print the heal report as JSON instead of a timeline",
    )
    p.add_argument(
        "--no-retry",
        action="store_true",
        help="print the patch only; do not re-run the corrected script",
    )
    p.add_argument(
        "--no-trace",
        action="store_true",
        help="do not record a trace of the heal (one is written otherwise)",
    )

    p = sub.add_parser("observe")
    p.add_argument("window", nargs="?")
    p.add_argument(
        "-o",
        "--out",
        metavar="PATH",
        help="write the observation to a file instead of stdout (stdout gets "
        "a one-line receipt)",
    )
    p.set_defaults(func=cmd_observe)

    p = sub.add_parser("call")
    p.add_argument("action")
    p.set_defaults(func=cmd_call)

    sub.add_parser("repl").set_defaults(func=cmd_repl)

    p = sub.add_parser("bench")
    p.add_argument("--runs", type=int, default=100)
    p.set_defaults(func=cmd_bench)

    sub.add_parser("mcp").set_defaults(func=cmd_mcp)
    sub.add_parser("rpc").set_defaults(func=cmd_rpc)
    return parser


def _configure_logging() -> None:
    """Turn on diagnostics when asked for, and only then.

    The runtime is normally silent: the tools return SUI-Text and nothing
    else, so stray log lines would land in the model's context. ``SOMNUS_LOG``
    is the general switch; ``SOMNUS_TRACE_STATES`` implies it, because tracing
    state decisions is useless without a handler to print them.
    """
    level_name = os.environ.get("SOMNUS_LOG", "").strip()
    trace = os.environ.get("SOMNUS_TRACE_STATES", "") not in ("", "0", "false")
    if not level_name and not trace:
        return
    level = getattr(logging, level_name.upper(), logging.INFO) if level_name else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SomnusError as err:
        print(err.render(), file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
