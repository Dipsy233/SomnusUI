"""CI smoke: the whole exposure-benchmark loop, no endpoint needed.

Runs the CLI with the scripted fake model on two contrasting schemes (flat H0
and fully-folded H4) and asserts the wiring end to end: surfaces assemble, the
loop dispatches through the shim, the state assertions pass and the report
lands with rows for both schemes.
"""

from __future__ import annotations

import json

import pytest

from bench.run_exposure import main


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path / "smoke"


def test_fake_matrix_end_to_end(out_dir):
    rc = main(
        [
            "--scheme",
            "H0,H4",
            "--fake",
            "--reps",
            "1",
            "--out",
            str(out_dir),
        ]
    )
    assert rc == 0
    run_dirs = sorted(p for p in out_dir.iterdir() if p.is_dir())
    assert len(run_dirs) == 1
    run = run_dirs[0]

    rows = [
        json.loads(line)
        for line in (run / "rows.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {r["scheme"] for r in rows} == {"H0", "H4"}
    # The scripted model solves both tasks on both surfaces.
    assert all(r["success"] for r in rows), rows
    # The report carries the paired comparison and the static costs.
    report = (run / "report.md").read_text(encoding="utf-8")
    assert "H4" in report and "静态注入成本" in report
