"""SomnusUI command line interface.

Commands::

    somnus windows                       list windows
    somnus record  <window> -o tree.json record a RawTree fixture
    somnus observe [window]              print the L0 SUI-Text observation
    somnus call    "CALL p1.add()"       run one action
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
    --confirmed                  pre-confirm critical actions
    --allow-clipboard            permit the Ctrl+V channel for this run
    --allow-keyboard             permit synthetic typing for this run
    --config PATH                read settings from PATH

The two synthetic-input switches are ON by default and can be turned off for
one run (``--no-allow-clipboard`` / ``--no-allow-keyboard``) or persistently
(``somnus config --no-allow-clipboard``) - see :mod:`somnus.config`.
"""

from __future__ import annotations

import argparse
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

    # Read once per session so the flag/config/env precedence is applied in one
    # place (see somnus/config.py).
    settings = load_settings(getattr(args, "config", None))
    provider_name = args.provider
    if args.demo:
        provider_name = "mock"

    if getattr(args, "android_fixture", None):
        # SomnusDroid M0: recorded Portal snapshots replayed through the real
        # Android conversion + shared compiler (doc §12 acceptance: `somnus
        # --android-fixture settings observe`).
        provider_name = "android"
        from somnus.providers.android.mock import AndroidMockProvider

        provider = AndroidMockProvider(snapshot_paths=args.android_fixture)
    elif provider_name == "mock" or args.fixture or getattr(args, "scenario", None):
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
        confirmed=bool(args.confirmed),
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
        allow_packs=not getattr(args, "no_pack", False),
    )

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
    print(session.observe(args.window).text)
    return 0


def cmd_call(args: argparse.Namespace) -> int:
    session = build_session(args)
    session.observe()
    text = session.dispatch(args.action, confirmed=args.confirmed)
    print(text)
    return 0 if not text.startswith("ERR") else 1


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
        choices=["auto", "uia", "mock", "android"],
        help="accessibility source: uia (Windows), mock (fixtures)",
    )
    parser.add_argument("--fixture", action="append", default=None)
    parser.add_argument(
        "--android-fixture",
        action="append",
        default=None,
        metavar="PATH",
        help=(
            "replay a recorded Portal snapshot (SomnusDroid): the file holds "
            "the JSON a Portal app answers `snapshot` with, and the mock "
            "Android provider serves it through the real conversion + "
            "compiler. Implies --provider android"
        ),
    )
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
    parser.add_argument("--confirmed", action="store_true")
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

    p = sub.add_parser("record")
    p.add_argument("window", nargs="?")
    p.add_argument("-o", "--output", default="fixture.json")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("observe")
    p.add_argument("window", nargs="?")
    p.set_defaults(func=cmd_observe)

    p = sub.add_parser("call")
    p.add_argument("action")
    p.set_defaults(func=cmd_call)

    sub.add_parser("repl").set_defaults(func=cmd_repl)

    p = sub.add_parser("bench")
    p.add_argument("--runs", type=int, default=100)
    p.set_defaults(func=cmd_bench)

    _mcp = sub.add_parser("mcp")
    _mcp.add_argument(
        "provider", nargs="?", default="auto",
        help="provider to bind (auto/uia/mock/android)"
    )
    _mcp.set_defaults(func=cmd_mcp)
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
