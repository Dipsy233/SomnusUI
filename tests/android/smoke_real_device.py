"""One-shot M1 smoke test against a real Portal (no pytest).

Runs the four acceptance RPCs and prints the SUI-Text observation, so a
phone-in-hand verification is one command:

    python tests/android/smoke_real_device.py --serial 1965905d
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from somnus.providers.android.transport import PortalError, PortalTransport  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default=None)
    ap.add_argument("--local-port", type=int, default=18677)
    args = ap.parse_args()

    if args.serial:
        subprocess.run(
            ["adb", "-s", args.serial, "forward",
             f"tcp:{args.local_port}", "tcp:8677"],
            check=False, capture_output=True,
        )

    t = PortalTransport(local_port=args.local_port, timeout=10.0, skip_adb=True)
    try:
        t.connect()
    except PortalError as err:
        print(f"FAIL connect: {err}")
        print("checklist: 服务已开启? adb forward tcp:18677 tcp:8677 已执行?")
        return 1

    # 1. ping / events: the channel is alive
    print("ping       :", t.call("ping"))
    print("events     :", t.call("events"))

    # 2. snapshot: the tree comes back
    started = time.monotonic()
    snap = t.call("snapshot")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    windows = snap.get("windows", [])
    total_nodes = count_nodes(windows)
    print(f"snapshot   : rev={snap.get('rev')} windows={len(windows)} "
          f"nodes={total_nodes} wire_ms={elapsed_ms}")
    for w in windows:
        print(f"  window {w.get('id')} pkg={w.get('pkg')!r} active={w.get('active')}")

    # 3. global back (safe: at worst leaves the current screen)
    print("global back:", t.call("global", {"action": "GLOBAL_ACTION_BACK"}))

    # 4. apps list (first 5)
    apps = t.call("apps")
    print(f"apps       : {len(apps)} launchable; first 5:")
    for a in apps[:5]:
        print(f"  {a.get('package')} {a.get('label')!r}")

    # Full observation through the real pipeline.
    from somnus.providers.android.rawtree import portal_json_to_tree
    from somnus.compiler.rules.android import prepare_android_tree
    from somnus.compiler.compile import Compiler
    from somnus.projection.budget import count_tokens
    from somnus.projection.engine import ProjectionEngine
    from somnus.projection.rules import apply_projection

    trees = portal_json_to_tree(snap)
    if trees:
        tree = trees[0]
        prepare_android_tree(tree)
        result = Compiler().compile(tree)
        proj = apply_projection(result.root, [])
        plan = ProjectionEngine(l0=True).plan(result.root, proj)
        text = "\n".join(
            getattr(l, "marker_text", "") or node_line(l) for l in plan.lines
        )
        from somnus.sui.serialize import render_line
        text = "\n".join(render_line(l) for l in plan.lines)
        print(f"\n--- L0 ({count_tokens(text)} tokens, snapshot wire {elapsed_ms}ms) ---")
        print(text)

    t.close()
    return 0


def count_nodes(windows: list) -> int:
    def walk(n) -> int:
        # null placeholders (skipped inert children) are not nodes.
        return 1 + sum(walk(c) for c in n.get("children", []) if isinstance(c, dict))
    return sum(
        walk(w["root"]) for w in windows if isinstance(w.get("root"), dict)
    )


def node_line(line):
    from somnus.sui.serialize import render_line
    return render_line(line)


if __name__ == "__main__":
    raise SystemExit(main())
