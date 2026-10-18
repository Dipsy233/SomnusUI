"""The benchmark harness must keep its promises.

`bench/run.py` is what produces the project's headline numbers, so it is itself
worth a regression test: a metric that silently degrades (compression falls
below the gate, a leak reappears, a task starts failing) would otherwise be
discovered only when someone re-reads the table in the README.

The run below is small (three iterations) because the harness is deterministic:
if a task or gate is broken, one iteration shows it.
"""

from __future__ import annotations

import contextlib
import io
import json
from argparse import Namespace

from bench.run import run_benchmark


def _run(runs: int = 3) -> dict:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_benchmark(Namespace(runs=runs))
    return {"exit_code": code, "summary": json.loads(buf.getvalue())}


def test_benchmark_passes_its_gates():
    """Every hard gate: L0 budget, action rate, task rate, zero leaks, >=10x."""
    result = _run()
    summary = result["summary"]
    assert result["exit_code"] == 0, summary
    assert summary["overview_tokens_max"] <= 300
    assert summary["action_success_rate"] >= 0.99
    assert summary["task_success_rate"] >= 0.90
    assert summary["leak_count"] == 0, summary["leaks"]
    assert summary["zero_vision"] is True
    assert summary["compression_ratio_min"] >= 10


def test_benchmark_is_deterministic():
    """Two runs of the same size must report the same numbers.

    The harness is the project's measurement device; a metric that drifts
    between identical runs cannot be compared over time.
    """
    assert _run(2)["summary"] == _run(2)["summary"]
