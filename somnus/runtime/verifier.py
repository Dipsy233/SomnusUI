"""Verifier: check declared effects against the settled state.

Outcomes:

* effects declared and all satisfied      -> OK
* no effects declared                     -> OK_UNVERIFIED
* effects declared but not satisfied      -> EFFECT_NOT_CONFIRMED
* native call failed                      -> NATIVE_EXECUTION_FAILED
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from somnus.ir.capability import Effect
from somnus.ir.node import SemanticNode


@dataclass(slots=True)
class VerifyResult:
    status: str  # OK | OK_UNVERIFIED | EFFECT_NOT_CONFIRMED
    checked: int = 0
    failed: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.failed is None:
            self.failed = []


class Verifier:
    def __init__(self, by_ref: dict[str, SemanticNode], by_entity: dict[str, SemanticNode]):
        self.by_ref = by_ref
        self.by_entity = by_entity
        #: baseline values captured before the action, keyed "ref.field".
        self.baseline: dict[str, Any] = {}

    def capture(self, ref: str) -> None:
        node = self.by_ref.get(ref)
        if node is None:
            return
        for key, value in node.attrs.items():
            self.baseline[f"{ref}.{key}"] = value

    def verify(self, effects: list[Effect], action_ref: str, delta) -> VerifyResult:
        if not effects:
            return VerifyResult(status="OK_UNVERIFIED")
        failed: list[str] = []
        checked = 0
        for effect in effects:
            outcome = self._check(effect, action_ref, delta)
            if outcome is None:
                continue  # unverifiable field -> not counted as failure
            checked += 1
            if outcome is False:
                failed.append(effect.render())
        if failed:
            return VerifyResult(
                status="EFFECT_NOT_CONFIRMED", checked=len(effects), failed=failed
            )
        if checked == 0:
            return VerifyResult(status="OK_UNVERIFIED")
        return VerifyResult(status="OK", checked=checked)

    # -- effect evaluation --------------------------------------------------

    def _check(self, effect: Effect, action_ref: str, delta) -> bool | None:
        """Return True (satisfied), False (violated) or None (unverifiable)."""
        target = effect.target
        if target == "$self":
            target = action_ref
        node = self.by_ref.get(target) or self.by_entity.get(target)
        if node is None:
            # An effect may reference an entity that is itself an output, e.g.
            # `cart.opened = true`; unknown targets are unverifiable.
            return None

        current = _field_value(node, effect.field)
        op = effect.op
        expected = effect.value
        if _field_value(node, effect.field) is None and effect.field not in (
            "count",
            "total",
            "value",
            "exists",
            "state",
            "rows",
        ):
            # Attribute may be synthesised post-action (e.g. opened=true).
            changed = _delta_has(delta, effect)
            if changed is not None:
                return changed
            return None
        if op == "=":
            return _loose_eq(current, expected)
        if op == "!=":
            return not _loose_eq(current, expected)
        if op == "contains":
            return expected in str(current or "")
        if op == "startswith":
            return str(current or "").startswith(str(expected))
        if op in ("+=", "-="):
            delta_value = expected if op == "+=" else -expected
            for ref, field_name, old, new in getattr(delta, "changed", ()):
                if field_name != effect.field:
                    continue
                if _loose_eq(_num(new), (_num(old) or 0) + delta_value):
                    return True
            base = self.baseline.get(f"{node.ref}.{effect.field}")
            if base is None:
                return None
            return _loose_eq(_num(current), _num(base) + delta_value)
        return None


def _field_value(node: SemanticNode, field: str) -> Any:
    if field in ("count", "total", "value"):
        return node.attrs.get(field, node.value)
    if field == "exists":
        return True
    if field == "state":
        return sorted(node.states)
    if field == "rows":
        return len(node.children)
    return node.attrs.get(field)


def _delta_has(delta, effect: Effect) -> bool | None:
    """Check the delta for a change matching the effect; None if absent."""
    for ref, field_name, old, new in getattr(delta, "changed", ()):
        if field_name == effect.field and _loose_eq(new, effect.value):
            return True
    # A `= true` effect may be satisfied by a newly added node of that type.
    for node in getattr(delta, "added", ()):  # pragma: no cover - rarely hit
        if effect.field in ("exists",) or effect.target == node.entity:
            return True
    return None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _loose_eq(a: Any, b: Any) -> bool:
    if a == b:
        return True
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-9
    return str(a) == str(b)
