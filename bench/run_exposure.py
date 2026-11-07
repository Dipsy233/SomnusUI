"""Run the tool-exposure hierarchy benchmark.

Examples::

    # one scheme, mock layer, real local model, 3 reps
    python -m bench.run_exposure --scheme H1 --reps 3 --out runs/exposure

    # the whole round-1 matrix
    python -m bench.run_exposure --scheme all --reps 3 --out runs/exposure

    # wiring smoke test without an endpoint (scripted fake model)
    python -m bench.run_exposure --scheme all --fake --reps 1 --out runs/smoke

Outputs, under ``--out``: one JSON per (scheme, task, rep) run, a rows.jsonl
of metric rows, and report.md with the paired verdict table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bench.exposure.assemble import assemble  # noqa: E402
from bench.exposure.dispatch import dispatch_tool  # noqa: E402
from bench.exposure.schemes import SCHEMES  # noqa: E402
from bench.loop.agent import FakeClient, LlmClient  # noqa: E402
from bench.score.metrics import rows_from_runs, static_cost, summarize  # noqa: E402
from bench.score.report import compare, render_report  # noqa: E402
from bench.tasks.runner import load_tasks, run_task  # noqa: E402

BASELINE = "H0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tool-exposure hierarchy benchmark")
    parser.add_argument("--scheme", default="all", help="H0..H6, 'all', or comma list")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--tasks", default=None, help="tasks JSON; default mock-v1")
    parser.add_argument(
        "--layer",
        choices=["mock", "live"],
        default="mock",
        help="mock = deterministic fixtures; live = real ScenarioSuite.exe windows",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="comma list of task ids to run (default: all tasks in the set)",
    )
    parser.add_argument("--out", default="runs/exposure")
    parser.add_argument(
        "--fake",
        action="store_true",
        help="scripted fake model instead of an endpoint (wiring smoke only)",
    )
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    args = parser.parse_args(argv)

    scheme_ids = (
        list(SCHEMES)
        if args.scheme in ("all", "")
        else [s.strip() for s in args.scheme.split(",") if s.strip()]
    )
    for sid in scheme_ids:
        if sid not in SCHEMES:
            parser.error(f"unknown scheme {sid!r}; known: {', '.join(SCHEMES)}")

    env = None
    if args.layer == "live":
        from bench.tasks.live import LiveEnv, find_app, load_live_tasks, run_live_task

        app = find_app()
        env = LiveEnv()
        env.open()  # refuses loudly if the child session is unavailable
        tasks = load_live_tasks()
        run_fn = lambda task, surface, fn, client, **kw: run_live_task(  # noqa: E731
            task, surface, fn, client, env=env, app=app, **kw
        )
    else:
        tasks = load_tasks(Path(args.tasks) if args.tasks else None)
        run_fn = run_task
    if args.only:
        wanted = {t.strip() for t in args.only.split(",") if t.strip()}
        tasks = [t for t in tasks if t.id in wanted]
    if args.fake:
        # The scripted model solves two store tasks; anything else would just
        # burn a final answer and fail. The smoke is about wiring, not score.
        wanted = (
            {"live-store-find-add"}
            if args.layer == "live"
            else {"mock-store-find-add", "mock-store-checkout"}
        )
        tasks = [t for t in tasks if t.id in wanted]
    out_dir = Path(args.out) / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.fake:
        make_client = lambda scheme, task: fake_client_for(scheme, task)  # noqa: E731
    else:
        shared = LlmClient(args.base_url, args.model, api_key=args.api_key)
        make_client = lambda scheme, task: shared  # noqa: E731

    all_rows: list[dict] = []
    try:
        for sid in scheme_ids:
            scheme = SCHEMES[sid]
            surface = assemble(scheme)
            dispatch_fn = lambda s, name, a, _sc=scheme: dispatch_tool(  # noqa: E731
                s, _sc, name, a
            )
            print(f"== {sid} {scheme.label} tools={surface.tool_names()}")
            if scheme.native_caps:
                from somnus.compiler import caps as caps_mod

                caps_mod.set_native_cap_names(True)
            try:
                for rep in range(args.reps):
                    for task in tasks:
                        client = make_client(scheme, task)
                        try:
                            run = run_fn(
                                task,
                                surface,
                                dispatch_fn,
                                client,
                                max_steps=args.max_steps,
                            )
                        except Exception as exc:  # noqa: BLE001 - one task
                            run = None  # infra/harness error, recorded
                            row = {
                                "task_id": task.id,
                                "scheme": sid,
                                "success": False,
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        if run is not None:
                            row = run.as_row()
                        row["rep"] = rep
                        all_rows.append(row)
                        flag = "PASS" if run is not None and run.success else "FAIL"
                        print(
                            f"  rep{rep} {task.id}: {flag} "
                            + (
                                f"steps={run.steps} calls={run.tool_calls} "
                                f"tok={run.prompt_tokens}+{run.completion_tokens}"
                                if run is not None
                                else f"error={row.get('error', '')[:120]}"
                            )
                        )
                        payload = (
                            run.as_row() | {"rep": rep, "transcript": run.transcript}
                            if run is not None
                            else row
                        )
                        (out_dir / f"{sid}-{task.id}-rep{rep}.json").write_text(
                            json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
            finally:
                if scheme.native_caps:
                    from somnus.compiler import caps as caps_mod

                    caps_mod.set_native_cap_names(False)
    finally:
        if env is not None:
            env.close()

    (out_dir / "rows.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in all_rows) + "\n",
        encoding="utf-8",
    )
    summary = summarize(all_rows)
    comparisons = compare(all_rows, baseline=BASELINE)
    costs = {sid: static_cost(assemble(SCHEMES[sid])) for sid in scheme_ids}
    report = render_report(comparisons, costs, summary)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    print(f"\nreport: {out_dir / 'report.md'}")
    return 0


def fake_client_for(scheme, task):
    """A scripted model that solves two store tasks on any scheme's surface.

    This exists so the whole loop (surface → dispatch → session → assertions)
    can be exercised in CI with no endpoint. It is not a benchmark result.
    """
    look = _expose_any(scheme, look_of=("observe", "look"))
    find = _expose_any(scheme, flat_of="find")
    act = _expose_any(scheme, flat_of="call", router="act")
    if look is None or act is None:
        raise ValueError(f"{scheme.id} exposes neither look nor act")
    overview_kw: dict = {}

    def do_find(query: str) -> dict:
        if find:
            return {"name": find, "arguments": {"query": query}}
        return {"name": look, "arguments": {"query": query}}

    def call_action(ref: str, cap: str) -> dict:
        return {"name": act, "arguments": {"action": f"CALL {ref}.{cap}()"}}

    def aim_at_store(messages):
        # The current window is not necessarily the scenario window (the child
        # desktop's foreground may be the taskbar), so aim first — the same
        # thing a real model must do off the WINDOWS lines.
        return {
            "name": look,
            "arguments": {"window": _ref_of(messages, "Scenario Suite - store")},
        }

    if task.id in ("mock-store-find-add", "live-store-find-add"):
        return FakeClient(
            [
                {"name": look, "arguments": overview_kw},
                aim_at_store,
                do_find("Special Item 17"),
                lambda messages: call_action(
                    _ref_of(messages, "Special Item 17"), "add"
                ),
                {"content": "已加入 Special Item 17"},
            ]
        )
    if task.id == "mock-store-checkout":
        return FakeClient(
            [
                {"name": look, "arguments": overview_kw},
                aim_at_store,
                do_find("Item 1"),
                lambda messages: call_action(_ref_of(messages, "Item 1"), "add"),
                lambda messages: call_action(_ref_of(messages, "结算"), "activate"),
                {"content": "已结算"},
            ]
        )
    return FakeClient([{"content": "fake model has no script for this task"}])


def _expose_any(
    scheme, *, flat_of: str | None = None, router: str | None = None,
    look_of: tuple[str, str] | None = None,
) -> str | None:
    """The exposed name standing for a base verb, flat or merged."""
    if flat_of:
        for exposed, base in scheme.flat:
            if base == flat_of:
                return exposed
    if router:
        for exposed, r in scheme.merged:
            if r == router or r.startswith(router + "_"):
                return exposed
    if look_of:
        for exposed, base in scheme.flat:
            if base in look_of:
                return exposed
        for exposed, r in scheme.merged:
            if r.split("_")[0] in look_of:
                return exposed
    return None  # the verb is folded away on this surface


def _ref_of(messages: list[dict], label: str) -> str:
    """The ref of the newest mention of ``label`` in the tool results.

    Matches both node lines (``ITEM p18 "Special Item 17"``) and the
    ``WINDOWS``-line window segments (``WINDOWS w1 "…" | w3 "Scenario Suite -
    store"``).
    """
    node_re = re.compile(r'^\s*\w+\s+(\S+)\s+"([^"]*)"')
    window_re = re.compile(r'\b(w\d+)\s+"([^"]*)"')
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            continue
        # Newest tool result wins, but within one result the FIRST hit is the
        # one a model would act on (a FIND lists its best row first).
        for line in (msg.get("content") or "").splitlines():
            for rx in (node_re, window_re):
                for m in rx.finditer(line):
                    if label in m.group(2):
                        return m.group(1)
    raise AssertionError(f"no ref named {label!r} in tool results")


if __name__ == "__main__":
    raise SystemExit(main())
