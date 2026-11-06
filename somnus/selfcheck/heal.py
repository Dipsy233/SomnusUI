"""``somnus heal``: replay a ``run`` script, relocate a failed step by recipe.

The self-check loop has a doctor, an eval and a replay. This is the repair
half: take a script a planner or a person wrote, run it against the current
desktop, and when a step fails because the element it names is stale or was
rebound onto a lookalike, find the intended element again *by recipe* and
return the corrected script.

What it is, and is not:

* it is **host code**, outside the runtime. It drives a normal ``Session``
  through the same tools a model uses, so it exercises the surface the model
  actually sees;
* it is **model-free** - the relocation is a deterministic locator match
  (:mod:`somnus.runtime.locator`), not an inference;
* it is **bounded** - one failing step, one replacement, one re-run. A failure
  the recipe cannot resolve is reported as-is; there is no retry loop.

The repair rule is deliberately narrow. A step is patched only when the
locator names exactly one element that carries the capability the step wants.
A tie is reported with the candidates; guessing would turn a failed script into
an action against the wrong control, which is worse than stopping.

Two facts make this useful beyond the runtime's own stale-ref relocation:

* the runtime relocates inside a *single* ``call``; a batch script validates
  every step before the first one runs, so one stale ref refuses the whole
  batch without acting;
* a step can fail for a reason other than staleness - a ref that still
  resolves but names a node without the capability (``CAPABILITY_NOT_FOUND``).
  The runtime is right to refuse; the recipe can still name the control that
  does have the capability, and the patch says which one.

Like every other self-check the desktop rule is fixed: the child session is
the default, fixtures are the offline path, the user's desktop needs an
explicit ``--real``. A missing child session is an error, never a silent fall
back to the main desktop.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from somnus.errors import SomnusError
from somnus.ir.action import Action
from somnus.runtime import locator as locators
from somnus.selfcheck import nodes
from somnus.sui.parse import parse_script

_STEP_LINE_RE = re.compile(r"^\s*(\d+)\.\s+(.*?)\s+->\s+(\S.*)$")
_FAILED_AT_RE = re.compile(r"failed-at=(\d+)")
_STEP_MENTION_RE = re.compile(r"step (\d+)")


@dataclass(slots=True)
class HealStep:
    index: int
    statement: str
    capability: str
    status: str = "not-run"
    error_code: str | None = None
    ref: str | None = None
    healed_ref: str | None = None
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.index,
            "statement": self.statement,
            "capability": self.capability,
            "status": self.status,
            "error_code": self.error_code,
            "ref": self.ref,
            "healed_ref": self.healed_ref,
            "evidence": self.evidence,
        }


@dataclass(slots=True)
class HealReport:
    script: str
    desktop: str
    provider: str
    #: A display label for the script (the file path), never its content.
    source: str = ""
    steps: list[HealStep] = field(default_factory=list)
    failed_at: int | None = None
    error_code: str | None = None
    candidates: list[str] = field(default_factory=list)
    patched_script: str | None = None
    rerun: str = ""
    ok: bool = False
    output: str = ""
    trace_out: str | None = None
    duration_ms: float = 0.0

    @property
    def healed(self) -> bool:
        return any(step.healed_ref for step in self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "script": self.source or self.script,
            "desktop": self.desktop,
            "provider": self.provider,
            "ok": self.ok,
            "steps": len(self.steps),
            "failed_at": self.failed_at,
            "error_code": self.error_code,
            "healed": self.healed,
            "candidates": self.candidates,
            "patched_script": self.patched_script,
            "rerun": self.rerun,
            "output": self.output,
            "trace_out": self.trace_out,
            "step_reports": [step.as_dict() for step in self.steps],
            "duration_ms": round(self.duration_ms, 1),
        }


def heal_script(
    session,
    script: str,
    *,
    source: str = "",
    window: str | None = None,
    desktop: str = "",
    provider: str = "",
    retry: bool = True,
    trace_out: str | None = None,
) -> HealReport:
    """Run *script*, repair one failed step by locator, optionally re-run."""
    started = time.perf_counter()
    steps = parse_script(script)
    report = HealReport(
        script=script,
        source=source,
        desktop=desktop or session.session_label,
        provider=provider or str(getattr(session.provider, "name", "unknown")),
        trace_out=trace_out,
    )
    report.steps = [
        HealStep(
            index=index,
            statement=action.raw,
            capability=(action.capability or action.kind),
            ref=action.ref,
        )
        for index, action in enumerate(steps, start=1)
    ]

    output = ""
    error: SomnusError | None = None
    try:
        output = session.run_script(steps, window=window)
    except SomnusError as exc:
        error = exc
    except Exception as exc:  # noqa: BLE001 - a heal reports, it does not raise
        report.output = f"{type(exc).__name__}: {exc}"
        report.duration_ms = (time.perf_counter() - started) * 1000
        return report

    report.output = output or (error.render() if error is not None else "")
    if output.startswith("RUN OK"):
        _apply_step_lines(report, output)
        report.ok = True
        report.rerun = "not needed"
        report.duration_ms = (time.perf_counter() - started) * 1000
        return report

    failed_at, error_code = _failure(output, error, steps)
    report.failed_at = failed_at
    report.error_code = error_code
    _apply_step_lines(report, output)
    if failed_at is None or not 1 <= failed_at <= len(steps):
        report.duration_ms = (time.perf_counter() - started) * 1000
        return report
    failing_report = report.steps[failed_at - 1]
    if failing_report.status == "not-run":
        # A batch refuses before its step lines exist; the exception is still
        # the failing step's answer, so the timeline names it.
        failing_report.status = f"ERR {error_code or 'ERR'}"
        failing_report.error_code = error_code

    failing = steps[failed_at - 1]
    rewritten, evidence, candidates = _relocate_failing(session, failing)
    report.candidates = candidates
    if rewritten is None:
        report.duration_ms = (time.perf_counter() - started) * 1000
        return report

    report.steps[failed_at - 1].healed_ref = rewritten
    report.steps[failed_at - 1].evidence = evidence
    report.patched_script = _rewrite_script(steps, failing, rewritten)

    if retry:
        try:
            rerun_output = session.run_script(parse_script(report.patched_script))
            report.rerun = "PASS" if rerun_output.startswith("RUN OK") else "FAIL"
            report.ok = rerun_output.startswith("RUN OK")
            report.output = rerun_output
        except SomnusError as exc:
            report.rerun = "FAIL"
            report.output = exc.render()
        except Exception as exc:  # noqa: BLE001
            report.rerun = "FAIL"
            report.output = f"{type(exc).__name__}: {exc}"
    else:
        report.ok = True
        report.rerun = "skipped"

    report.duration_ms = (time.perf_counter() - started) * 1000
    return report


def render(report: HealReport) -> str:
    """A readable timeline: what ran, what was patched, what the re-run did."""
    lines = [
        f"heal {report.source or '<script>'} desktop={report.desktop} "
        f"provider={report.provider} steps={len(report.steps)}"
    ]
    for step in report.steps:
        mark = step.status
        lines.append(f"  {step.index}. {step.statement} -> {mark}")
    if report.failed_at is not None:
        step = report.steps[report.failed_at - 1]
        if step.healed_ref:
            lines.append(
                f"healed step {step.index}: {step.ref}->{step.healed_ref} "
                f"({step.evidence})"
            )
        else:
            reason = (
                f"; candidates: {', '.join(report.candidates)}"
                if report.candidates
                else ""
            )
            lines.append(
                f"UNHEALED step={step.index} err={report.error_code}{reason}"
            )
    if report.patched_script:
        lines.append("PATCH")
        lines.extend(f"  {line}" for line in report.patched_script.splitlines())
        lines.append(f"rerun: {report.rerun}")
    elif report.ok:
        lines.append("PASS (no heal needed)")
    elif report.failed_at is not None:
        lines.append("FAIL (no patch)")
    if report.trace_out:
        lines.append(f"trace -> {report.trace_out}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# failure parsing
# ---------------------------------------------------------------------------


def _failure(
    output: str, error: SomnusError | None, steps: list[Action]
) -> tuple[int | None, str | None]:
    """The failing step index and error code, from the reply or the exception."""
    if output:
        match = _FAILED_AT_RE.search(output)
        if match:
            return int(match.group(1)), _error_code_of(output)
    if error is None:
        return None, None
    index = _step_for_ref(error.ref, steps)
    if index is None and error.message:
        match = _STEP_MENTION_RE.search(error.message)
        if match:
            index = int(match.group(1))
    return index, str(error.code)


def _step_for_ref(ref: str | None, steps: list[Action]) -> int | None:
    if not ref:
        return None
    for index, step in enumerate(steps, start=1):
        if step.ref == ref:
            return index
    return None


def _error_code_of(output: str) -> str | None:
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("ERR "):
            parts = stripped.split()
            if len(parts) >= 2:
                return parts[1]
    return None


def _apply_step_lines(report: HealReport, output: str) -> None:
    """Copy the per-step states the runtime printed onto the report."""
    states: dict[int, str] = {}
    for line in output.splitlines():
        match = _STEP_LINE_RE.match(line)
        if match:
            states[int(match.group(1))] = match.group(3).strip()
    for step in report.steps:
        state = states.get(step.index)
        if state is None:
            continue
        step.status = state
        if state.startswith("ERR"):
            step.error_code = state.split()[1] if len(state.split()) > 1 else "ERR"


# ---------------------------------------------------------------------------
# relocation and rewrite
# ---------------------------------------------------------------------------


def _relocate_failing(
    session, failing: Action
) -> tuple[str | None, str, list[str]]:
    """Find the ref the failing step should have used, or explain why not."""
    if failing.kind != "call" or not failing.ref or failing.ref == "$it":
        return None, "", []
    record = session.registry.records.get(failing.ref)
    if record is None or session.result is None:
        return None, "", []
    if record.window_id and record.window_id != session.target_window:
        return None, "", []

    recipes = _recipes(record, failing.capability)
    candidates: list[str] = []
    for recipe in recipes:
        result = locators.match(session.result.root, recipe)
        if result.unique and result.ref != failing.ref:
            return result.ref, result.locator.describe(), []
        if result.found > 1 and not candidates:
            candidates = result.candidate_lines(3)
    return None, "", candidates


def _recipes(record, capability: str | None) -> list[locators.Locator]:
    """Recipes from strongest to weakest; the first unique match wins.

    The capability is the caller's own requirement and the best tie-breaker.
    Entity and name-only recipes follow, because a pack may type the element
    differently after a screen change, and a role may be presentation
    vocabulary that the rebuilt tree renders another way.
    """
    base = locators.from_record(record)
    out: list[locators.Locator] = []
    if capability:
        out.append(
            locators.Locator(
                role=base.role,
                name=base.name,
                entity=base.entity,
                capability=capability,
            )
        )
    out.append(base)
    if base.name:
        if capability:
            out.append(locators.Locator(name=base.name, capability=capability))
        out.append(locators.Locator(name=base.name))
    if base.entity:
        out.append(locators.Locator(entity=base.entity, capability=capability))
    # De-duplicate identical recipes while preserving strength order.
    seen: set[tuple] = set()
    unique: list[locators.Locator] = []
    for recipe in out:
        key = (
            recipe.role,
            recipe.name,
            recipe.entity,
            recipe.capability,
            recipe.states,
            recipe.scope,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(recipe)
    return unique


def _rewrite_script(
    steps: list[Action], failing: Action, new_ref: str
) -> str:
    lines: list[str] = []
    for step in steps:
        if step is failing:
            lines.append(_rewrite_statement(step.raw, step.ref, new_ref))
        else:
            lines.append(step.raw)
    return "\n".join(lines)


def _rewrite_statement(raw: str, old_ref: str | None, new_ref: str) -> str:
    if not old_ref:
        return raw
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(old_ref)}(?![A-Za-z0-9_])"
    return re.sub(pattern, new_ref, raw, count=1)


def window_ref(session, hint: str | None) -> str | None:
    """The window to observe: a ref, a title, or the foreground window."""
    text = session.observe(windows_only=True).text
    if hint:
        line = next(
            (item for item in nodes.parse_observation(text) if item.is_window
             and item.ref == hint),
            None,
        )
        if line is None:
            line = next(
                (item for item in nodes.parse_observation(text) if item.is_window
                 and item.name == hint),
                None,
            )
        return line.ref if line is not None else None
    window = nodes.pick_window(text)
    return window.ref if window is not None else None
