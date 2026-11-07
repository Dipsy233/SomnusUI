"""Per-run metric rows plus each scheme's static (injected) cost."""

from __future__ import annotations

import json
from typing import Any

from somnus.projection.budget import count_tokens


def static_cost(surface) -> dict[str, int]:
    """Tokens a model pays before its first step: prompt + tool schemas."""
    prompt = count_tokens(surface.system_prompt)
    schemas = 0
    for tool in surface.openai_tools:
        fn = tool["function"]
        schemas += count_tokens(fn["description"])
        schemas += count_tokens(json.dumps(fn["parameters"], ensure_ascii=False))
    return {
        "prompt_tokens": prompt,
        "schema_tokens": schemas,
        "injected_tokens": prompt + schemas,
        "tool_count": len(surface.openai_tools),
    }


def rows_from_runs(runs: list[Any]) -> list[dict[str, Any]]:
    """Flatten RunResults into metric rows (success filled by the runner)."""
    return [
        {
            "scheme": r.scheme,
            "task_id": r.task_id,
            "success": bool(r.success),
            "steps": r.steps,
            "tool_calls": r.tool_calls,
            "error_calls": r.error_calls,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "total_tokens": r.prompt_tokens + r.completion_tokens,
            "wall_s": round(r.wall_s, 3),
            "final_answer": r.final_answer,
        }
        for r in runs
    ]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Scheme-level summary: success rate, medians, error rate.

    Rows may be infrastructure failures (an exception outside the loop) with
    none of the counters; they still count as failures but contribute nothing
    to the medians.
    """
    by_scheme: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_scheme.setdefault(row["scheme"], []).append(row)
    out: dict[str, Any] = {}
    for scheme, rs in sorted(by_scheme.items()):
        n = len(rs)
        calls = [r["tool_calls"] for r in rs if "tool_calls" in r]
        out[scheme] = {
            "n": n,
            "success_rate": sum(1 for r in rs if r["success"]) / n,
            "median_tool_calls": _median(calls),
            "median_steps": _median(
                [r["steps"] for r in rs if "steps" in r]
            ),
            "median_total_tokens": _median(
                [r["total_tokens"] for r in rs if "total_tokens" in r]
            ),
            "error_call_rate": (
                sum(r.get("error_calls", 0) for r in rs) / max(1, sum(calls))
            ),
            "failed_tasks": sorted(
                {r["task_id"] for r in rs if not r["success"]}
            ),
        }
    return out


def _median(values: list[float]) -> float:
    vs = sorted(values)
    if not vs:
        return 0.0
    mid = len(vs) // 2
    if len(vs) % 2:
        return float(vs[mid])
    return (vs[mid - 1] + vs[mid]) / 2
