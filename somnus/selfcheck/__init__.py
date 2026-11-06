"""Self-experience loops for SomnusUI: diagnose, replay, judge and heal.

These commands run *outside* the runtime. They drive a normal ``Session`` - the
same tools a model would use - and report what happened in evidence the caller
can check: a runtime-state verdict for ``eval``, the first divergent step for
``replay``, an environment report for ``doctor``, and a locator-based patch for
a failed ``run`` step in ``heal``. No language model is involved, so the loop
works offline and costs nothing in inference.

The sandbox rule is stated once, in :mod:`somnus.selfcheck.sandbox`: the child
session is the default desktop, mock/scenario fixtures are the offline path, and
the user's main desktop requires an explicit ``--real``.
"""

from __future__ import annotations

from somnus.selfcheck.checks import CheckReport, CheckResult, run_self_checks
from somnus.selfcheck.heal import HealReport, HealStep, heal_script

__all__ = [
    "CheckReport",
    "CheckResult",
    "HealReport",
    "HealStep",
    "heal_script",
    "run_self_checks",
]
