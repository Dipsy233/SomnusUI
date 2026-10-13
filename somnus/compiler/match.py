"""Pattern matching: raw tree -> entities.

This is the stage that turns "a group containing a text, a text and a button"
into "``product`` with ``+add``". It is the heart of SomnusUI's value
proposition (dev doc section 11.1, "the key step").

Two sources of entity knowledge, in priority order:

1. **Packs** - declarative :class:`EntityDef` rules, confidence 1.0.
2. **Generic heuristics** - weak structural signals, confidence < 1.0. These
   never upgrade a node to an entity unless the score exceeds ``threshold``;
   otherwise the node stays a primitive (dev doc section 11.2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from somnus.compiler.pack_schema import EntityDef, Pack, regex_search
from somnus.ir.capability import Capability, Effect, PrimitiveStep
from somnus.ir.node import RawNode

#: Minimum heuristic confidence required to promote a node to an entity.
HEURISTIC_THRESHOLD = 0.9


@dataclass(slots=True)
class EntityMatch:
    """Result of matching one raw node against the pack/heuristic model."""

    entity: str
    node: RawNode
    def_: EntityDef | None
    members: dict[str, RawNode] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    caps: list[Capability] = field(default_factory=list)
    #: Primary identity value for the fingerprint (automation id or name).
    identity: str | None = None


# ---------------------------------------------------------------------------
# Pack matching
# ---------------------------------------------------------------------------


def match_entities(root: RawNode, pack: Pack | None) -> dict[int, EntityMatch]:
    """Return ``id(raw_node) -> EntityMatch`` for the whole tree.

    A node matched by a pack is not descended into for further *pack* matches of
    the same entity family, but children may still match other entity defs.
    """
    out: dict[int, EntityMatch] = {}
    entity_defs = list(pack.entities) if pack else []
    for node in root.walk():
        best: EntityMatch | None = None
        for edef in entity_defs:
            if not edef.match.matches(node):
                continue
            em = _build_match(edef, node)
            if best is None or em.confidence > best.confidence:
                best = em
        if best is not None:
            out[id(node)] = best
    return out


def _build_match(edef: EntityDef, node: RawNode) -> EntityMatch:
    members = edef.match.bind(node)
    # Bind the node itself under $self for uniformity.
    members.setdefault("self", node)
    attrs = _eval_attrs(edef.attrs, members)
    identity = _eval_key(edef.key, members) or _default_identity(node, members)
    return EntityMatch(
        entity=edef.name,
        node=node,
        def_=edef,
        members=members,
        attrs=attrs,
        confidence=edef.confidence,
        identity=identity,
    )


def _eval_key(expr: str | None, members: dict[str, RawNode]) -> str | None:
    """Resolve an attribute/key expression to a string.

    Supported forms (all deterministic):

    * ``$member.attr`` - an attribute of a captured ``as`` member (or ``$self``).
    * ``descendant(role=text)`` / ``descendant(role=text, name=/re/)`` - the
      first descendant matching an inline predicate (dev doc section 15).
    """
    if not expr:
        return None
    expr = expr.strip()

    m = re.fullmatch(r"\$(\w+)\.(\w+)", expr)
    if m:
        member, attr = m.groups()
        node = members.get(member)
        if node is None:
            return None
        return _node_attr(node, attr)

    if expr.startswith("descendant("):
        node = _descendant_match(expr, members)
        return node.name if node is not None else None

    return None


def _descendants(node: RawNode) -> Iterable[RawNode]:
    yield node
    for child in node.children:
        yield from _descendants(child)


def _re_search(pattern: str, text: str | None) -> bool:
    return regex_search(pattern, text)


def _descendant_match(expr: str, members: dict[str, RawNode]) -> RawNode | None:
    """Evaluate ``descendant(role=text, name=/re/, has_name=true)``."""
    inner = expr[len("descendant(") :].rstrip(")")
    root = members.get("self")
    if root is None:
        return None
    role = None
    name = None
    has_name = None
    for clause in inner.split(","):
        clause = clause.strip()
        if not clause or "=" not in clause:
            continue
        key, _, value = clause.partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"')
        if key == "role":
            role = value
        elif key == "name":
            name = value
        elif key == "has_name":
            has_name = value.lower() in ("true", "1", "yes")
    for cand in _descendants(root):
        if cand is root:
            continue
        if role and cand.role != role:
            continue
        if name and not _re_search(name, cand.name):
            continue
        if has_name is not None and bool(cand.name) != has_name:
            continue
        return cand
    return None


def _node_attr(node: RawNode, attr: str) -> str | None:
    if attr in ("automation_id", "id"):
        return node.native.automation_id
    if attr == "name":
        return node.name
    if attr == "value":
        return node.value
    if attr == "class_name":
        return node.native.class_name
    if attr == "role":
        return node.role
    return None


def _eval_attrs(spec: dict[str, Any], members: dict[str, RawNode]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, rule in spec.items():
        if isinstance(rule, str):
            out[key] = _eval_key(rule, members)
            continue
        if not isinstance(rule, dict):
            out[key] = rule
            continue
        source = _eval_key(rule.get("from"), members)
        if source is None:
            if "default" in rule:
                out[key] = rule["default"]
            continue
        value: Any = source
        if rule.get("regex"):
            m = re.search(rule["regex"], source)
            if not m:
                if "default" in rule:
                    out[key] = rule["default"]
                continue
            value = m.group(1) if m.groups() else m.group(0)
        cast = rule.get("cast")
        if cast == "number":
            value = _to_number(value)
        elif cast == "int":
            value = _to_int(value)
        out[key] = value
    return out


def _to_number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    try:
        return int(float(str(value).replace(",", ".")))
    except (TypeError, ValueError):
        return None


def _default_identity(node: RawNode, members: dict[str, RawNode]) -> str | None:
    if node.native.automation_id:
        return node.native.automation_id
    for key in ("title", "add_btn", "open_btn", "count_text", "price_text"):
        member = members.get(key)
        if member is not None and member.name:
            return member.name
    return node.name


# ---------------------------------------------------------------------------
# Entity capabilities -> runtime Capabilities
# ---------------------------------------------------------------------------


def entity_capabilities(em: EntityMatch) -> list[Capability]:
    """Translate pack ``EntityCapDef`` declarations into runtime capabilities.

    Steps whose pattern condition is not met by the matched node are dropped
    here, at compile time - the resolver never sees a step it cannot run. This
    is what makes the keyboard fallback deterministic: a ``type_text`` step is
    only present when the target really lacks a Value pattern (dev doc §12.2).
    """
    caps: list[Capability] = []
    for name, cdef in em.def_.caps.items():
        steps: list[PrimitiveStep] = []
        for ref_expr in cdef.via:
            step = _compile_step(ref_expr, em)
            if _step_applies(step, em):
                steps.append(step)
        if not steps:
            continue
        effects = [_parse_effect(e) for e in cdef.effects]
        cap = Capability(
            name=name,
            level="semantic",
            params=dict(cdef.params),
            via=steps,
            effects=[e for e in effects if e is not None],
            risk=cdef.risk,
            aliases=cdef.aliases,
        )
        caps.append(cap)
    return caps


def _step_applies(step: PrimitiveStep, em: EntityMatch) -> bool:
    """Evaluate a step's pattern condition against the matched node."""
    if not step.condition:
        return True
    kind, _, pattern = step.condition.partition(":")
    target_node = em.members.get("self")
    if step.target.startswith("member:"):
        target_node = em.members.get(step.target.split(":", 1)[1]) or target_node
    patterns = target_node.patterns if target_node is not None else frozenset()
    if kind == "has":
        return pattern in patterns
    if kind == "missing":
        return pattern not in patterns
    return True


def _compile_step(expr: str | dict[str, Any], em: EntityMatch) -> PrimitiveStep:
    """Compile one ``via`` step into a :class:`PrimitiveStep`.

    Accepts either the string form ``"$add_btn.activate"`` or the object form
    used for conditional steps::

        { via: { target: "$self", primitive: "type_text",
                 value: "$text", when_missing: value } }

    Target is stored as the member key so the resolver can locate the native
    element without re-running the match.
    """
    if isinstance(expr, dict):
        return _compile_step_dict(expr, em)

    m = re.fullmatch(r"\$(\w+)\.(\w+)(?:\((.*)\))?", expr.strip())
    if not m:
        return PrimitiveStep(primitive=expr, target="$self", label=expr)
    member, primitive, arg = m.groups()
    value: Any = None
    if arg:
        value = arg.strip().strip('"')
        if value.startswith("$"):
            value = value  # forwarded parameter
    return PrimitiveStep(
        primitive=primitive, target=f"member:{member}", value=value, label=expr
    )


def _compile_step_dict(spec: dict[str, Any], em: EntityMatch) -> PrimitiveStep:
    target = str(spec.get("target", "$self"))
    if target.startswith("$") and target != "$self":
        target = "member:" + target[1:]
    condition = None
    if spec.get("when_has"):
        condition = f"has:{spec['when_has']}"
    elif spec.get("when_missing"):
        condition = f"missing:{spec['when_missing']}"
    return PrimitiveStep(
        primitive=str(spec.get("primitive", "")),
        target=target,
        value=spec.get("value"),
        label=str(spec.get("label", spec.get("primitive", ""))),
        condition=condition,
    )


def _parse_effect(raw: str) -> Effect | None:
    """Parse ``"cart.count += 1"`` into an :class:`Effect`."""
    m = re.fullmatch(
        r"\s*(\$?\w+)\.(\w+)\s*(\+=|-=|=|!=|contains|startswith)\s*(.*?)\s*", raw
    )
    if not m:
        return None
    target, field_name, op, value = m.groups()
    parsed: Any = value
    if re.fullmatch(r"-?\d+", value):
        parsed = int(value)
    elif re.fullmatch(r"-?\d+\.\d+", value):
        parsed = float(value)
    elif value in ("true", "false"):
        parsed = value == "true"
    return Effect(target=target, field=field_name, op=op, value=parsed, raw=raw)


# ---------------------------------------------------------------------------
# Generic heuristics (low confidence)
# ---------------------------------------------------------------------------

_VERB_BUTTONS = re.compile(
    r"^(add|buy|remove|delete|select|choose|apply|subscribe|加入|购买|删除|选择|应用)$",
    re.IGNORECASE,
)
_PRICE = re.compile(r"[$¥€]\s?\d+[.,]\d{2}|\d+[.,]\d{2}\s?(元|USD|CNY)")


def heuristic_entities(root: RawNode) -> dict[int, EntityMatch]:
    """Detect entity-like groups without a pack.

    A group qualifies as a generic "item" when it contains exactly one named
    text and one verb button. Score is computed from the signals and compared to
    :data:`HEURISTIC_THRESHOLD`; below it, the group is left primitive.
    """
    out: dict[int, EntityMatch] = {}
    for node in root.walk():
        if node.role not in ("group", "listitem", "row"):
            continue
        texts = [c for c in node.children if c.role == "text" and c.name]
        buttons = [c for c in node.children if c.role == "button" and c.name]
        verb_buttons = [b for b in buttons if _VERB_BUTTONS.match(b.name or "")]
        score = 0.0
        if texts:
            score += 0.4
        if verb_buttons:
            score += 0.4
        if any(_PRICE.search(c.name or "") for c in texts):
            score += 0.15
        if "automation_id" in (node.native.automation_id or "") or node.native.automation_id:
            score += 0.05
        if score < HEURISTIC_THRESHOLD or not texts or not verb_buttons:
            continue
        members = {
            "self": node,
            "title": texts[0],
            "add_btn": verb_buttons[0],
        }
        out[id(node)] = EntityMatch(
            entity="item",
            node=node,
            def_=None,
            members=members,
            attrs={"name": texts[0].name},
            confidence=round(score, 3),
            identity=node.native.automation_id or texts[0].name,
        )
    return out
