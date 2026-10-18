"""SomnusDroid command line interface.

Commands::

    somnus windows                       list the device's windows
    somnus observe                       print the L0 SUI-Text observation
    somnus call    "CALL b1.activate()"  run one action
    somnus call    "SWITCH launch 微信"   open an app through the platform API
    somnus repl                          interactive SUI-Text loop
    somnus mcp [provider]                start the MCP server (stdio)
    somnus rpc                           start the JSON-RPC server (stdio)

Global options::

    --provider {auto,android,mock}   select the accessibility provider
    --android-fixture PATH      replay a recorded Portal snapshot (offline)
    --fixture PATH              load a recorded fixture into the mock provider
    --demo                      use the built-in restaurant demo fixtures
    --pack ID                   force an app pack
    --allow PROCESS             allow-list a process (repeatable)
    --confirmed                 pre-confirm critical actions
    --allow-gesture             enable taps at node bounds (self-drawn apps)
    --allow-clipboard           permit the clipboard write channel
    --allow-keyboard            permit synthetic typing (host-side routes)
    --config PATH               read settings from PATH

Synthetic-input switches are ON by default and can be turned off for one run
(``--no-allow-clipboard`` / ``--no-allow-keyboard``) or persistently
(``somnus config --no-allow-clipboard``) - see :mod:`somnus.config`. The
gesture channel is OFF by default: a tap is a blind coordinate, so it must be
named on the command line (or enabled in the Portal's config) every time.
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

        gesture_flag = getattr(args, "allow_gesture", None)
        gesture = (
            gesture_flag
            if gesture_flag is not None
            else load_settings(getattr(args, "config", None)).allow_gesture
        )
        provider = get_provider(provider_name, allow_gesture=gesture)

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
    # Open on the current screen: on a phone that is the only screen there
    # is, and the observation header already names it (SCREEN home / qq). The
    # old desktop-era window list carried no signal here.
    print(session.observe().text)
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
        choices=["auto", "android", "mock"],
        help="accessibility source: android (Portal over adb), mock (fixtures)",
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
        "--allow-gesture",
        dest="allow_gesture",
        action="store_true",
        default=None,
        help=(
            "force the gesture channel on for this run (overrides the "
            "settings file). Taps at a node's bounds centre serve controls "
            "that publish no CLICK action (self-drawn apps); the channel is "
            "on by default - --no-allow-gesture turns it off"
        ),
    )
    parser.add_argument(
        "--no-allow-gesture",
        dest="allow_gesture",
        action="store_false",
        help="turn the gesture channel off for this run",
    )
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
        help="provider to bind (android/mock)"
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
