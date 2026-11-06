"""Self-experience loops for SomnusUI: diagnose, replay, and judge.

These commands run *outside* the runtime. They drive a normal ``Session`` - the
same tools a model would use - and report what happened in evidence the caller
can check: a runtime-state verdict for ``eval``, the first divergent step for
``replay``, and an environment report for ``doctor``. No language model is
involved, so the loop works offline and costs nothing in inference.

The sandbox rule is stated once, in :mod:`somnus.selfcheck.sandbox`: the child
session is the default desktop, mock/scenario fixtures are the offline path, and
the user's main desktop requires an explicit ``--real``.
"""

from __future__ import annotations

from somnus.selfcheck.checks import CheckReport, CheckResult, run_self_checks

__all__ = ["CheckReport", "CheckResult", "run_self_checks"]
