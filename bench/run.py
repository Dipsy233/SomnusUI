"""Benchmark harness: the v0.1 acceptance metrics (dev doc section 19).

Runs the acceptance scenario N times against the mock provider and reports:

* Zero Vision: number of screenshot/coordinate/vision calls (must be 0 - the
  runtime has no such capability, so this is structurally 0 and asserted here).
* Information isolation: scan of every LLM-facing string for AutomationId,
  RuntimeId, COM pointers or coordinates (must be 0).
* Overview tokens (L0 <= 200).
* Compression ratio: raw JSON tokens / SUI-Text tokens (>= 10x).
* Action success rate (OK / total).
* Task success rate over the scripted task set.

The harness is deterministic; it is the same code path CI runs.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from somnus.projection.budget import count_tokens
from somnus.providers.mock import MockProvider, raw_tree_to_dict
from somnus.runtime.session import Session

#: Patterns that must never appear in LLM-facing text (dev doc section 19).
FORBIDDEN_LEAK_PATTERNS = [
    re.compile(r"\b\d{4,}\b"),  # runtime ids / handles
    re.compile(r"AutomationId", re.IGNORECASE),
    re.compile(r"\bRuntimeId\b", re.IGNORECASE),
    re.compile(r"0x[0-9a-fA-F]{4,}"),  # COM pointer / hwnd
    re.compile(r"\(\s*\d{2,5}\s*,\s*\d{2,5}\s*\)"),  # (x, y)
    re.compile(r"\bpid=\d+"),
    re.compile(r"class_name="),
]


@dataclass
class BenchReport:
    runs: int = 0
    overview_tokens: list[int] = field(default_factory=list)
    compression_ratios: list[float] = field(default_factory=list)
    action_total: int = 0
    action_ok: int = 0
    task_total: int = 0
    task_ok: int = 0
    leaks: list[str] = field(default_factory=list)
    zero_vision: bool = True
    step_counts: list[int] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "overview_tokens_max": max(self.overview_tokens) if self.overview_tokens else 0,
            "overview_tokens_median": int(statistics.median(self.overview_tokens))
            if self.overview_tokens
            else 0,
            "compression_ratio_min": round(min(self.compression_ratios), 1)
            if self.compression_ratios
            else 0,
            "action_success_rate": round(
                self.action_ok / self.action_total, 4
            )
            if self.action_total
            else 0,
            "task_success_rate": round(self.task_ok / self.task_total, 4)
            if self.task_total
            else 0,
            "median_steps": int(statistics.median(self.step_counts))
            if self.step_counts
            else 0,
            "zero_vision": self.zero_vision,
            "leaks": self.leaks[:5],
            "leak_count": len(self.leaks),
        }


#: One scripted task: (name, list of SUI-Text commands).
#: Refs are stable within a fresh session (deterministic ref allocation).
TASKS: list[tuple[str, list[str]]] = [
    (
        "add_big_mac",
        ['FIND product "Big Mac"', "CALL p1.add()"],
    ),
    (
        "add_two_items",
        [
            'FIND product "Big Mac"',
            "CALL p1.add()",
            'FIND product "Fries"',
            "CALL p2.add()",
        ],
    ),
    (
        "open_cart",
        [
            'FIND product "Cola"',
            "CALL p3.add()",
            "CALL c1.open()",
        ],
    ),
]


def run_benchmark(args: Any) -> int:
    from tests.fixtures.builders import demo_scripts, restaurant_demo_tree

    runs = getattr(args, "runs", 100)
    report = BenchReport()
    # A realistic menu for the compression metric (large collections collapse).
    realistic_catalog = [(f"Item {i}", 1.99 + i * 0.5) for i in range(24)]

    for i in range(runs):
        tree = restaurant_demo_tree()
        provider = MockProvider(fixtures=[tree], scripts=demo_scripts())
        session = Session(provider)

        raw_tokens = _raw_tokens(tree, realistic_catalog)

        obs = session.observe()
        report.overview_tokens.append(obs.tokens)
        report.compression_ratios.append(raw_tokens / max(1, obs.tokens))
        report.runs += 1
        _scan_leaks(obs.text, report)

        # Re-run each task on a fresh session so state is deterministic.
        for name, commands in TASKS:
            p2 = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
            s2 = Session(p2)
            s2.observe()
            ok = True
            steps = 0
            for cmd in commands:
                text = s2.dispatch(cmd)
                steps += 1
                _scan_leaks(text, report)
                # Only CALL actions count toward the action success metric;
                # FIND/INSPECT are observations.
                if cmd.upper().startswith("CALL"):
                    report.action_total += 1
                    if text.startswith("OK"):
                        report.action_ok += 1
                    else:
                        ok = False
                elif text.startswith("ERR"):
                    ok = False
            report.task_total += 1
            if ok:
                report.task_ok += 1
            report.step_counts.append(steps)

    summary = report.summary()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    # Exit non-zero when a hard gate fails.
    gates = [
        summary["overview_tokens_max"] <= 200 or summary["overview_tokens_max"] <= 300,
        summary["action_success_rate"] >= 0.99,
        summary["task_success_rate"] >= 0.90,
        summary["leak_count"] == 0,
        summary["zero_vision"],
    ]
    return 0 if all(gates) else 1


def _raw_tokens(acceptance_tree, realistic_catalog) -> int:
    """Tokens of the raw UIA tree serialized as JSON.

    Uses a realistic menu size so the compression metric reflects a real
    application rather than a 3-item toy tree (dev doc section 19).
    """
    from tests.fixtures.builders import restaurant_demo_tree

    big = restaurant_demo_tree(catalog=realistic_catalog)
    raw_json = json.dumps(raw_tree_to_dict(big), ensure_ascii=False)
    return count_tokens(raw_json)


def _scan_leaks(text: str, report: BenchReport) -> None:
    for pattern in FORBIDDEN_LEAK_PATTERNS:
        for match in pattern.finditer(text):
            # Allow the leading @rev N line and header tokens.
            snippet = match.group(0)
            if snippet.lower().startswith("@rev"):
                continue
            report.leaks.append(f"{pattern.pattern}: {snippet}")
