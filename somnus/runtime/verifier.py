"""Verifier: check declared effects against the settled state.

Outcomes:

* effects declared and all satisfied      -> OK
* no effects declared, and nothing changed -> OK_UNVERIFIED
* no effects declared, but the tree moved -> OK
* effects declared but not satisfied      -> EFFECT_NOT_CONFIRMED
* native call failed                      -> NATIVE_EXECUTION_FAILED

The "no effects, but the tree moved" case is the status-honesty fix from the
optimisation plan (P2-4). A capability that declares no effects still has
evidence it worked when the settled tree shows a change: reporting
``OK_UNVERIFIED`` there made an agent *retry an action that already succeeded*
(the "semantic inflation" behind repeated set_text/toggle turns). What is
deliberately **not** flattened is the other direction - a settled UI with no
change stays ``OK_UNVERIFIED``, because "ran and nothing moved" is genuinely
unconfirmed. ``Session`` applies the last step of that rule (it is the only
layer that knows whether the settle completed); see the note in :meth:`verify`.
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


def _status_without_effects(delta: Any) -> str:
    """``OK`` when an undeclared effect is visible, else ``OK_UNVERIFIED``.

    The delta is the only evidence available at this layer. A change in it
    proves the UI reacted to the action, which is strictly stronger than "it
    ran"; an empty delta leaves the outcome undetermined. The ``settled`` half
    of the plan's rule stays in ``Session`` because it is the owner of the
    settle result.
    """
    empty = getattr(delta, "empty", True)
    return "OK_UNVERIFIED" if empty else "OK"


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
            # `delta` is the settled transition; a non-empty one is evidence the
            # action took effect even without a declared effect. See the module
            # docstring (P2-4).
            return VerifyResult(status=_status_without_effects(delta))
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
            # Declared effects that none of them could be evaluated: fall back
            # to what the tree itself says, exactly like the no-effect case.
            return VerifyResult(status=_status_without_effects(delta))
        return VerifyResult(status="OK", checked=checked)

    def verify_grouped(
        self,
        step_effects: list[tuple[int, str, list[Effect]]],
        delta,
    ) -> VerifyResult:
        """Verify a whole batch's effects against one combined delta.

        A batch settles once, at its end, so a step's effects are checked
        against the *final* state rather than the state right after that step.
        Cumulative declarations are therefore grouped per field: two steps that
        each declare ``cart.count += 1`` expect the final value to be the
        baseline + 2, and checking each step's own ``+1`` against the combined
        delta would report a false violation (the first step's ``+1`` is not
        visible as such once the second has also landed).

        ``step_effects`` carries ``(step number, action ref, effects)`` per
        executed step; the action ref is what ``$self`` resolves against. Steps
        that never ran must not be passed in - their effects did not happen.
        """
        groups: dict[tuple[str, str], list[tuple[int, str, Effect]]] = {}
        for step_no, action_ref, effects in step_effects:
            for effect in effects:
                target = effect.target
                if target == "$self":
                    target = action_ref
                node = self.by_ref.get(target) or self.by_entity.get(target)
                if node is None:
                    # Unknown targets are unverifiable, as in verify().
                    continue
                groups.setdefault((node.ref, effect.field), []).append(
                    (step_no, action_ref, effect)
                )

        checked = 0
        failed: list[str] = []
        for (node_ref, field), entries in groups.items():
            node = self.by_ref.get(node_ref)
            if node is None:
                continue
            outcome = self._check_group(node, field, entries, delta)
            if outcome is None:
                continue
            checked += 1
            if outcome is False:
                steps = ",".join(str(no) for no, _r, _e in entries)
                failed.append(f"step {steps}: {entries[-1][2].render()}")
        if failed:
            return VerifyResult(
                status="EFFECT_NOT_CONFIRMED",
                checked=checked,
                failed=failed,
            )
        if checked == 0:
            return VerifyResult(status=_status_without_effects(delta))
        return VerifyResult(status="OK", checked=checked)

    def _check_group(
        self,
        node: SemanticNode,
        field: str,
        entries: list[tuple[int, str, Effect]],
        delta,
    ) -> bool | None:
        """Check one field's cumulative expectation against the final state.

        ``=``/``!=``/``contains``/``startswith`` are absolute predicates: the
        last declaration on the field wins, and it must hold at the end.
        ``+=``/``-=`` accumulate: every declared increment sums onto the
        pre-batch baseline.
        """
        increments = [e for _n, _r, e in entries if e.op in ("+=", "-=")]
        if increments:
            total = 0.0
            saw_increment = False
            for effect in increments:
                value = _num(effect.value)
                if value is None:
                    return None  # a non-numeric increment cannot be summed
                saw_increment = True
                total += value if effect.op == "+=" else -value
            base = self.baseline.get(f"{node.ref}.{field}")
            if not saw_increment or base is None:
                return None
            # The final value comes from the delta, not from ``node``: the
            # verifier's node objects predate the batch, so their attributes
            # are the state *before* any step ran. Only a field the delta does
            # not mention can be read off the node - it did not change.
            current = None
            for ref, field_name, _old, new in getattr(delta, "changed", ()):
                if ref == node.ref and field_name == field:
                    current = new
            if current is None:
                current = _field_value(node, field)
            return _loose_eq(_num(current), _num(base) + total)

        # Absolute predicate: the last declaration on this field is the one the
        # final state must satisfy.
        _step_no, action_ref, effect = entries[-1]
        return self._check(effect, action_ref, delta)

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
