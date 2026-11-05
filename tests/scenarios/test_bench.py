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
    """Two runs of the same size must report the same *metrics*.

    The harness is the project's measurement device; a metric that drifts
    between identical runs cannot be compared over time. Wall clock is the
    documented exception - it exists precisely because it does vary - so it is
    excluded here and checked separately below.
    """
    first = _run(2)["summary"]
    second = _run(2)["summary"]
    first.pop("timing", None)
    second.pop("timing", None)
    assert first == second


def test_benchmark_reports_wall_clock_p50_and_p95():
    """The timing block must be present and well-formed for comparison.

    A run without timings cannot answer "is this faster than before", which is
    the only reason the column exists.
    """
    timing = _run(2)["summary"]["timing"]
    for name in ("wall", "observe"):
        assert timing[name]["n"] > 0, timing
    assert timing["wall"]["p50"] <= timing["wall"]["p95"], timing
    # The per-call segments the runtime instruments must be there too.
    for segment in ("snapshot", "compile"):
        assert segment in timing, timing
