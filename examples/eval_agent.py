"""End-to-end LLM agent evaluation against a local model.

Drives the demo app with a real model and judges success from the *runtime
state* (not from the model's own claim), which is the only honest measure.

Each task declares a machine-checkable ``check`` over the session's cart.

Usage::

    python examples/eval_agent.py --runs 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from examples.agent import LlmClient, run_agent  # noqa: E402

#: (name, task, expected_cart_count, expected_total)
TASKS: list[tuple[str, str, int, float]] = [
    ("one_big_mac", "帮我在购物车里加一个巨无霸", 1, 5.99),
    ("big_mac_and_fries", "帮我点一个巨无霸和一份薯条", 2, 8.48),
    ("three_colas", "帮我加三份可乐到购物车", 3, 5.97),
]


def cart_state(session) -> tuple[int, float]:
    session.observe()
    for node in session.result.root.walk():
        if node.entity == "cart":
            return int(node.attrs.get("count", 0)), float(node.attrs.get("total", 0.0))
    return 0, 0.0


def run_once(client: LlmClient, task: str, max_steps: int) -> tuple[bool, int, str]:
    from somnus.providers.mock import MockProvider
    from somnus.runtime.policy import Policy
    from somnus.runtime.session import Session
    from tests.fixtures.builders import demo_scripts, restaurant_demo_tree

    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    session = Session(provider, pack_id="restaurant-demo", policy=Policy(confirmed=False))
    try:
        final = run_agent(session, task, client=client, max_steps=max_steps, verbose=False)
        count, total = cart_state(session)
    finally:
        session.close()
    return count, total, final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8787/v1")
    parser.add_argument("--model", default="hy3")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=8)
    args = parser.parse_args(argv)

    client = LlmClient(args.base_url, args.model, api_key=args.api_key)
    results: list[dict] = []

    for name, task, want_count, want_total in TASKS:
        ok = 0
        latencies: list[float] = []
        for _ in range(args.runs):
            start = time.monotonic()
            count, total, final = run_once(client, task, args.max_steps)
            latencies.append(time.monotonic() - start)
            success = count == want_count and abs(total - want_total) < 0.01
            ok += int(success)
            print(
                f"  {name:18s} count={count} total={total:.2f} "
                f"(want {want_count}/{want_total:.2f}) -> {'PASS' if success else 'FAIL'}"
            )
        results.append(
            {
                "task": name,
                "success_rate": ok / args.runs,
                "median_latency_s": round(statistics.median(latencies), 1),
            }
        )

    overall = sum(r["success_rate"] for r in results) / len(results)
    print(json.dumps({"overall_success_rate": round(overall, 3), "tasks": results},
                     ensure_ascii=False, indent=2))
    return 0 if overall >= 0.9 else 1


if __name__ == "__main__":
    raise SystemExit(main())
