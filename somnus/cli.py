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
"""

from __future__ import annotations

import argparse
import sys

from somnus.errors import SomnusError
from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session


def _load_fixture(path: str):
    from somnus.providers.mock import load_tree

    return load_tree(path)


def build_session(args: argparse.Namespace) -> Session:
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
        allow_processes=tuple(args.allow or ()),
        confirmed=bool(args.confirmed),
    )
    return Session(provider, pack_id=args.pack, policy=policy)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


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
        choices=["auto", "uia", "cdp", "mock"],
        help="accessibility source: uia (Windows), cdp (browser), mock (fixtures)",
    )
    parser.add_argument("--fixture", action="append", default=None)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--pack", default=None)
    parser.add_argument("--allow", action="append", default=None)
    parser.add_argument("--confirmed", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("windows").set_defaults(func=cmd_windows)

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


def main(argv: list[str] | None = None) -> int:
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
