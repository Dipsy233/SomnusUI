"""Paired scheme-vs-baseline comparison and the verdict table.

Pairing unit is the *task instance*: (task_id, rep) matched across schemes, so
a scheme with a repeat that the baseline lacks is compared only on the overlap.
Bootstrap (10k resamples, fixed seed) gives 95% CIs for the paired differences.
The verdict rule is deliberately light — the heavy pre-registration protocol of
the previous attempt (plan-v3) is out of scope for round 1:

* REJECT if the success rate drops by ≥10pp with the bootstrap CI lower bound
  above 0 (a real regression);
* ADOPT_CANDIDATE if success is non-inferior AND tool rounds drop ≥15% or
  total tokens drop ≥10% with CI lower bound above 0;
* PENDING otherwise.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from typing import Any

BOOTSTRAP_N = 10_000
SEED = 20261107


@dataclass(frozen=True)
class Comparison:
    scheme: str
    n_pairs: int
    success_delta_pp: float
    success_ci: tuple[float, float]
    rounds_delta_pct: float
    rounds_ci: tuple[float, float]
    tokens_delta_pct: float
    tokens_ci: tuple[float, float]
    verdict: str


def compare(rows: list[dict[str, Any]], baseline: str = "H0") -> list[Comparison]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        # Later reps overwrite earlier ones is wrong; key must be unique per rep.
        by_key[(row["scheme"], row["task_id"], row.get("rep", 0))] = row
    base_keys = {k[1:] for k in by_key if k[0] == baseline}
    out = []
    for scheme in sorted({row["scheme"] for row in rows}):
        if scheme == baseline:
            continue
        pairs = [
            (by_key[(baseline, task, rep)], by_key[(scheme, task, rep)])
            for (task, rep) in sorted(base_keys)
            if (scheme, task, rep) in by_key
            and "tool_calls" in by_key[(baseline, task, rep)]
            and "tool_calls" in by_key[(scheme, task, rep)]
        ]
        if not pairs:
            continue
        out.append(_compare_pairs(scheme, pairs))
    return out


def _compare_pairs(scheme: str, pairs) -> Comparison:
    rng = random.Random(SEED)
    n = len(pairs)
    base_success = [1.0 if b["success"] else 0.0 for b, _ in pairs]
    cand_success = [1.0 if c["success"] else 0.0 for _, c in pairs]
    base_rounds = [float(b["tool_calls"]) for b, _ in pairs]
    cand_rounds = [float(c["tool_calls"]) for _, c in pairs]
    base_tokens = [float(b["total_tokens"]) for b, _ in pairs]
    cand_tokens = [float(c["total_tokens"]) for _, c in pairs]

    d_success = [
        c - b for b, c in zip(base_success, cand_success)
    ]
    d_rounds = [_pct_drop(b, c) for b, c in zip(base_rounds, cand_rounds)]
    d_tokens = [_pct_drop(b, c) for b, c in zip(base_tokens, cand_tokens)]

    success_delta = 100 * _mean(d_success)
    rounds_delta = 100 * _mean(d_rounds)
    tokens_delta = 100 * _mean(d_tokens)
    success_ci = _bootstrap_ci(rng, d_success, scale=100)
    rounds_ci = _bootstrap_ci(rng, d_rounds, scale=100)
    tokens_ci = _bootstrap_ci(rng, d_tokens, scale=100)

    verdict = _verdict(
        success_delta, success_ci, rounds_delta, rounds_ci, tokens_delta, tokens_ci
    )
    return Comparison(
        scheme=scheme,
        n_pairs=n,
        success_delta_pp=round(success_delta, 1),
        success_ci=tuple(round(v, 1) for v in success_ci),
        rounds_delta_pct=round(rounds_delta, 1),
        rounds_ci=tuple(round(v, 1) for v in rounds_ci),
        tokens_delta_pct=round(tokens_delta, 1),
        tokens_ci=tuple(round(v, 1) for v in tokens_ci),
        verdict=verdict,
    )


def _verdict(
    success_delta: float,
    success_ci: tuple[float, float],
    rounds_delta: float,
    rounds_ci: tuple[float, float],
    tokens_delta: float,
    tokens_ci: tuple[float, float],
) -> str:
    """The round-1 rule: no regression, then win on rounds or tokens."""
    if success_delta <= -10 and success_ci[0] > 0:
        return "REJECT"
    noninferior = success_delta > -10 or success_ci[1] <= 0
    rounds_win = rounds_delta >= 15 and rounds_ci[0] > 0
    tokens_win = tokens_delta >= 10 and tokens_ci[0] > 0
    if noninferior and (rounds_win or tokens_win):
        return "ADOPT_CANDIDATE"
    return "PENDING"


def _pct_drop(base: float, cand: float) -> float:
    if base <= 0:
        return 0.0
    return (base - cand) / base


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _bootstrap_ci(
    rng: random.Random, deltas: list[float], scale: float = 1.0
) -> tuple[float, float]:
    if not deltas:
        return (0.0, 0.0)
    means = []
    n = len(deltas)
    for _ in range(BOOTSTRAP_N):
        sample = [deltas[rng.randrange(n)] for _ in range(n)]
        means.append(_mean(sample) * scale)
    means.sort()
    lo = means[int(0.025 * len(means))]
    hi = means[min(len(means) - 1, int(0.975 * len(means)))]
    return (lo, hi)


def render_report(
    comparisons: list[Comparison],
    static_costs: dict[str, dict[str, int]],
    summary: dict[str, Any],
    title: str = "工具暴露方案对比",
) -> str:
    """A markdown verdict table + per-scheme summary."""
    lines = [
        f"# {title}",
        "",
        "配对单位 = (任务, 重复);bootstrap 10k,95% CI;基线 H0。",
        "",
        "| 方案 | N | 成功率差(pp) | 回合差(%) | token 差(%) | 判定 |",
        "|---|---|---|---|---|---|",
    ]
    for c in comparisons:
        lines.append(
            f"| {c.scheme} | {c.n_pairs} "
            f"| {c.success_delta_pp:+.1f} [{c.success_ci[0]:+.1f}, {c.success_ci[1]:+.1f}] "
            f"| {c.rounds_delta_pct:+.1f} [{c.rounds_ci[0]:+.1f}, {c.rounds_ci[1]:+.1f}] "
            f"| {c.tokens_delta_pct:+.1f} [{c.tokens_ci[0]:+.1f}, {c.tokens_ci[1]:+.1f}] "
            f"| {c.verdict} |"
        )
    lines += ["", "## 静态注入成本", "", "| 方案 | 工具数 | prompt | schema | 合计 |", "|---|---|---|---|---|"]
    for scheme_id, cost in static_costs.items():
        lines.append(
            f"| {scheme_id} | {cost['tool_count']} | {cost['prompt_tokens']} "
            f"| {cost['schema_tokens']} | {cost['injected_tokens']} |"
        )
    lines += ["", "## 汇总", "", "```json", json.dumps(summary, ensure_ascii=False, indent=2), "```"]
    return "\n".join(lines) + "\n"
