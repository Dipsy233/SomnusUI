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

The two synthetic-input switches are off by default and can be made persistent
(``somnus config --allow-clipboard``) - see :mod:`somnus.config`.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from somnus.errors import SomnusError
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

    if provider_name == "mock" or args.fixture:
        trees = []
        scripts = {}
        if args.demo:
            from tests.fixtures.builders import demo_scripts, restaurant_demo_tree

            trees.append(restaurant_demo_tree())
            scripts = demo_scripts()
        for path in args.fixture or []:
            trees.append(_load_fixture(path))
        provider = MockProvider(fixtures=trees, scripts=scripts)
    else:
        from somnus.providers.base import get_provider

        provider = get_provider(provider_name)

    policy = Policy(
        allow_processes=tuple(args.allow or ()) or settings.allow_processes,
        confirmed=bool(args.confirmed),
        # Synthetic input is off unless asked for: the runtime drives the
        # interfaces an application publishes, and an element offering none is
        # reported rather than typed into.
        #
        # `settings` is the persistent answer to "I accept the cost": the flag
        # wins for one run, the config file decides by default, and the built-in
        # default is off. See somnus/config.py.
        allow_keyboard=bool(getattr(args, "allow_keyboard", False)) or settings.allow_keyboard,
        # The clipboard channel borrows a resource that belongs to the user, so
        # it is opt-in for the same reason synthetic input is - and it is the
        # route that reaches a self-drawn editor when keyboard input is the only
        # alternative. Off unless the host asks for it.
        allow_clipboard=bool(getattr(args, "allow_clipboard", False)) or settings.allow_clipboard,
    )
    return Session(
        provider,
        pack_id=args.pack,
        policy=policy,
        allow_packs=not getattr(args, "no_pack", False),
    )


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
        print(f"wrote {written} (defaults; both channels off)")

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
        action="store_true",
        help=(
            "permit synthetic keyboard input for elements that publish no "
            "usable interface (off by default: such an element is reported "
            "instead of typed into). Persist this in the settings file with "
            "`somnus config --allow-keyboard`"
        ),
    )
    parser.add_argument(
        "--allow-clipboard",
        action="store_true",
        help=(
            "permit the clipboard channel (Ctrl+V) for elements that publish "
            "no usable interface and map no HWND. Off by default: it needs "
            "the target in the foreground. The user's clipboard is saved and "
            "restored format by format - bitmaps and file lists included - so "
            "nothing is lost; writes are marked to stay out of clipboard "
            "history and cloud sync. Persist this in the settings file with "
            "`somnus config --allow-clipboard`"
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

    sub.add_parser("mcp").set_defaults(func=cmd_mcp)
    sub.add_parser("rpc").set_defaults(func=cmd_rpc)
    return parser


def _configure_logging() -> None:
    """Turn on diagnostics when asked for, and only then.

    The runtime is normally silent: the four tools return SUI-Text and nothing
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
