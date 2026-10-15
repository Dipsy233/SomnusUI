"""Resolver: semantic capability call -> ordered native primitive steps.

The resolver is the only place that knows how a semantic capability maps onto
native elements. It never invents coordinates or keystrokes (dev doc section
12.2): every step must be a declared Native Pattern.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from somnus.compiler.compile import CompileResult
from somnus.errors import (
    ErrorCode,
    SomnusError,
    capability_not_found,
    invalid_argument,
    stale_target,
)
from somnus.ir.capability import Capability, Effect, PrimitiveStep
from somnus.ir.node import NativeRef, SemanticNode

log = logging.getLogger("somnus.resolver")

#: Set SOMNUS_TRACE_STATES=1 for the same trace the provider emits.
_TRACE_STATES = os.environ.get("SOMNUS_TRACE_STATES", "") not in ("", "0", "false")


@dataclass(slots=True)
class ResolvedStep:
    """One concrete native action."""

    native: NativeRef
    primitive: str
    value: Any = None
    label: str = ""


@dataclass(slots=True)
class Resolution:
    """The full plan for one capability call."""

    ref: str
    capability: str
    node: SemanticNode
    steps: list[ResolvedStep] = field(default_factory=list)
    effects: list[Effect] = field(default_factory=list)
    risk: str = "safe"
    requires_confirmation: bool = False


class Resolver:
    """Resolve actions against a compiled result + registry data."""

    def __init__(self, compile_result: CompileResult) -> None:
        self.result = compile_result
        self._by_ref: dict[str, SemanticNode] = {}
        for node in compile_result.root.walk():
            self._by_ref[node.ref] = node

    # -- lookup -------------------------------------------------------------

    def node(self, ref: str) -> SemanticNode:
        node = self._by_ref.get(ref)
        if node is None:
            raise SomnusError(ErrorCode.TARGET_NOT_FOUND, ref=ref)
        return node

    def find_refs(self, predicate) -> list[SemanticNode]:
        return [n for n in self._by_ref.values() if predicate(n)]

    # -- capability resolution ---------------------------------------------

    def resolve(self, action) -> Resolution:
        node = self.node(action.ref)
        cap = node.get_cap(action.capability)
        if cap is None:
            # Try aliases.
            for candidate in node.caps:
                if action.capability in getattr(candidate, "aliases", ()):
                    cap = candidate
                    break
        if cap is None:
            raise capability_not_found(action.ref, action.capability)

        self._validate_args(node, cap, action.args)

        if "disabled" in node.states and cap.name not in ("read", "focus", "inspect"):
            if _TRACE_STATES:
                log.info(
                    "refuse ref=%s cap=%s role=%s name=%r states=%s caps=%s",
                    action.ref,
                    cap.name,
                    node.role,
                    node.name,
                    sorted(node.states),
                    [c.name for c in node.caps],
                )
            raise SomnusError(ErrorCode.TARGET_DISABLED, ref=action.ref)

        steps = self._build_steps(node, cap, action.args)
        if not steps and cap.level == "primitive":
            raise SomnusError(ErrorCode.ACTION_NOT_SUPPORTED, ref=action.ref)

        return Resolution(
            ref=action.ref,
            capability=cap.name,
            node=node,
            steps=steps,
            effects=list(cap.effects),
            risk=cap.risk,
            requires_confirmation=cap.requires_confirmation,
        )

    def _validate_args(
        self, node: SemanticNode, cap: Capability, args: dict[str, Any]
    ) -> None:
        positional = sorted(k for k in args if k.startswith("_"))
        # A positional argument may satisfy the single required parameter, so the
        # documented ``CALL q1.set_text("Big Mac")`` form works even when the
        # pack names the parameter (dev doc section 6.2).
        required = [n for n, s in cap.params.items() if s.required]
        implicit = (
            positional[0] if len(positional) == 1 and len(required) == 1 else None
        )
        for name, spec in cap.params.items():
            present = name in args or (implicit is not None and name == required[0])
            if spec.required and not present:
                raise invalid_argument(node.ref, f"missing {name}")
            if name in args and not spec.accepts(args[name]):
                raise invalid_argument(node.ref, f"bad {name}")
            if implicit is not None and name == required[0] and not spec.accepts(
                args[implicit]
            ):
                raise invalid_argument(node.ref, f"bad {name}")
        # Reject unknown named args (positional `_N` are tolerated).
        for key in args:
            if key.startswith("_"):
                continue
            if key not in cap.params:
                raise invalid_argument(node.ref, f"unknown arg {key}")

    def _build_steps(
        self, node: SemanticNode, cap: Capability, args: dict[str, Any]
    ) -> list[ResolvedStep]:
        source = self.result.sources.get(node.ref)
        if source is None:
            raise stale_target(node.ref)
        members = self.result.members_by_ref.get(node.ref, {})

        steps: list[ResolvedStep] = []
        for step in cap.via:
            native = self._resolve_target(node, step, members)
            # Runtime pattern gate. A cap may carry several alternative `via`
            # paths (e.g. set_text by Value pattern *or* by keyboard) and the
            # choice depends on the live element, which pack-time matching
            # cannot always know. Steps whose condition is not met by the
            # matched node are dropped here, so the executor only ever runs a
            # path the element really supports.
            if not self._condition_holds(step, node):
                continue
            value = step.value
            if isinstance(value, str) and value.startswith("$"):
                value = args.get(value[1:].strip())
            if value is None and step.primitive in ("set_text", "type_text"):
                value = _text_argument(args)
            steps.append(
                ResolvedStep(
                    native=native,
                    primitive=step.primitive,
                    value=value,
                    label=step.label or step.primitive,
                )
            )
        return steps

    def _condition_holds(self, step: PrimitiveStep, node: SemanticNode) -> bool:
        """Evaluate a step's ``has:``/``missing:``/``hwnd`` gate against ``node``.

        The node carries its own native pattern names, so the gate is exact and
        does not depend on a raw-tree lookup. A missing condition means "always
        run".

        ``hwnd`` / ``no-hwnd`` are a separate predicate because a native window
        handle is not a UIA pattern: it is a property of how the control is
        implemented, and it is what gates the ``set_text_hwnd`` route. Expressing
        it as a condition keeps every write route in one ordered ``via`` list
        instead of branching in code.
        """
        if not step.condition:
            return True
        if step.condition == "hwnd":
            return self._has_hwnd(node)
        if step.condition == "no-hwnd":
            return not self._has_hwnd(node)
        kind, _, pattern = step.condition.partition(":")
        patterns = node.patterns
        if kind == "has":
            return pattern in patterns
        if kind == "missing":
            return pattern not in patterns
        return True

    def _has_hwnd(self, node: SemanticNode) -> bool:
        """Whether the node maps a real Win32 window handle.

        The compiled node does not carry the handle itself (the semantic layer
        never exposes native identity), so the native ref is consulted. A node
        whose ref is gone is treated as not having one, which makes the resolver
        fall through to the keyboard route rather than sending a message to a
        dead handle.
        """
        source = self.result.sources.get(node.ref)
        if source is None:
            return False
        return bool(getattr(source.native, "native_window_handle", 0))

    def _resolve_target(
        self, node: SemanticNode, step: PrimitiveStep, members: dict[str, str]
    ) -> NativeRef:
        target = step.target
        if target in ("$self", "", None):
            source = self.result.sources.get(node.ref)
            if source is None:
                raise stale_target(node.ref)
            return source.native
        if target.startswith("member:"):
            key = target.split(":", 1)[1]
            member_ref = members.get(key)
            if member_ref is None:
                raise stale_target(node.ref)
            msource = self.result.sources.get(member_ref)
            if msource is None:
                raise stale_target(node.ref)
            return msource.native
        # A direct ref.
        source = self.result.sources.get(target)
        if source is None:
            raise stale_target(target)
        return source.native


def _first_positional(args: dict[str, Any]) -> Any:
    keys = sorted(k for k in args if k.startswith("_"))
    if not keys:
        return None
    return args[keys[0]]


def _text_argument(args: dict[str, Any]) -> Any:
    """The text to write for a ``set_text`` step: positional, ``text``, or ``value``."""
    for key in ("text", "value"):
        if key in args:
            return args[key]
    return _first_positional(args)
