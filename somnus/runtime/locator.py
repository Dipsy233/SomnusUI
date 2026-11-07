"""Re-resolvable locators: find the same element again by recipe.

A ref is stable across revisions, but it names a *native element* and can go
stale when the application re-parents or rebuilds one (a calculator switching
from standard to scientific, a dialog opening over the same screen). The
registry relocates a stale ref with the fingerprint's last-seen structural
evidence first; this module is the second, deliberately narrower strategy: a
human-readable recipe made of role, name, entity, layer, states, capability,
position and container.

Why a second strategy exists at all:

* the registry needs an exact normalized name match and a unique structural
  winner, and returns ``None`` on a tie. A tie is a real answer ("do not guess"),
  but it can also be broken by *evidence the caller already has* - the
  capability it was about to call, the entity a pack assigned, the states the
  platform reported. Those filters turn a tie into one candidate;
* ``FIND`` is a user-facing search that deduplicates to one hit per element and
  prefers the parent row. A locator has no such preferences: it answers with
  every node that matches the recipe, so a caller can see the ambiguity instead
  of receiving an arbitrary pick.

This is a **pure function** module. It reads a compiled tree and returns nodes;
it never touches a provider, a session, a ref or the registry, which is what
makes the recipes testable without a Windows desktop. Nothing here is exposed
to the model: the locator is used inside the runtime (stale-ref second
relocation) and by the host-side ``heal`` loop. Whether it should also become
an optional ``locate`` tool remains an open design decision; until that is
decided, the model-visible tool surface stays unchanged.

Name matching follows the runtime's own normalization (whitespace collapsed,
case folded), so a locator and ``FIND`` agree about what "the same name" means.
A ``/regex/`` name is an opt-in widening for a caption that changed a little;
it is never the default, because a loose recipe silently retargets an action.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from somnus.ir.node import SemanticNode
from somnus.runtime import preflight
from somnus.sui.serialize import escape_text


def name_key(name: str | None) -> str:
    """The runtime's name normalization: whitespace collapsed, case folded."""
    return " ".join((name or "").split()).casefold()


def _regex(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Locator:
    """A recipe for one element, independent of any ref or native handle."""

    role: str | None = None
    #: The exact name the element carried when the recipe was recorded.
    name: str | None = None
    #: An optional ``/regex/`` alternative; mutually exclusive with ``name``.
    name_re: str | None = None
    entity: str | None = None
    layer: str | None = None
    #: Every state listed must be present (``disabled``, ``readonly``, …).
    states: frozenset[str] = frozenset()
    #: The capability the caller intends to run; the strongest tie-breaker.
    capability: str | None = None
    #: Zero-based position among all matches; a negative value counts from the
    #: end. Omitted means "all matches" (the caller decides about ties).
    index: int | None = None
    #: A container ref to search inside ("FIND ... in REF").
    scope: str | None = None

    @classmethod
    def parse_name(cls, value: str) -> str:
        """Return the regex source of ``/pattern/``, or raise ValueError."""
        if value.startswith("/") and value.endswith("/") and len(value) > 2:
            return value[1:-1]
        raise ValueError("not a regex literal")

    def describe(self) -> str:
        """One-line evidence for a ``~ healed`` note or a heal report."""
        parts: list[str] = []
        if self.role:
            parts.append(f"role={self.role}")
        if self.name_re:
            parts.append(f"name=/{escape_text(self.name_re)}/")
        elif self.name:
            parts.append(f'name="{escape_text(self.name)}"')
        if self.entity:
            parts.append(f"entity={self.entity}")
        if self.states:
            parts.append("states=" + ",".join(sorted(self.states)))
        if self.capability:
            parts.append(f"cap={self.capability}")
        if self.index is not None:
            parts.append(f"index={self.index}")
        if self.scope:
            parts.append(f"in={self.scope}")
        return "locator " + " ".join(parts)


@dataclass(frozen=True, slots=True)
class LocatorResult:
    """Every node that matched one recipe, in document order."""

    locator: Locator
    matches: tuple[SemanticNode, ...] = ()

    @property
    def found(self) -> int:
        return len(self.matches)

    @property
    def unique(self) -> bool:
        return len(self.matches) == 1

    @property
    def ref(self) -> str | None:
        """The one ref the recipe names, or ``None`` when it is not unique."""
        return self.matches[0].ref if self.unique else None

    def candidate_lines(self, limit: int = 3) -> list[str]:
        """``ref role "name"`` for the first ``limit`` matches."""
        out: list[str] = []
        for node in self.matches[:limit]:
            label = f"{node.ref} {node.role}"
            if node.name:
                label += f' "{escape_text(node.name)}"'
            out.append(label)
        return out


def match(root: SemanticNode, locator: Locator) -> LocatorResult:
    """Find every node in *root* that satisfies *locator*, in document order."""
    nodes = _scope(root, locator.scope)
    pattern = _regex(locator.name_re) if locator.name_re else None
    wanted_states = locator.states
    matches: list[SemanticNode] = []
    for node in nodes:
        if locator.role is not None and node.role != locator.role:
            continue
        if locator.entity is not None and node.entity != locator.entity:
            continue
        if locator.layer is not None and node.layer != locator.layer:
            continue
        if locator.name or pattern is not None:
            if not _name_matches(node.name, locator.name, pattern):
                continue
        if wanted_states and not wanted_states.issubset(node.states):
            continue
        if locator.capability and not preflight.capability_present(
            node, locator.capability
        ):
            continue
        matches.append(node)

    if locator.index is not None and matches:
        index = locator.index
        if index < 0:
            index += len(matches)
        selected = [matches[index]] if 0 <= index < len(matches) else []
        return LocatorResult(locator=locator, matches=tuple(selected))
    return LocatorResult(locator=locator, matches=tuple(matches))


def _name_matches(
    name: str | None, exact: str | None, pattern: re.Pattern[str] | None
) -> bool:
    if pattern is not None:
        # A regex is a widening: an element with no name cannot satisfy a
        # recipe that named something, so it is not a match.
        return name is not None and bool(pattern.search(name))
    return name_key(name) == name_key(exact)


def _scope(root: SemanticNode, scope: str | None) -> list[SemanticNode]:
    if not scope:
        return list(root.walk())
    container = next((node for node in root.walk() if node.ref == scope), None)
    if container is None:
        return []
    out: list[SemanticNode] = []
    for child in container.children:
        out.extend(child.walk())
    return out


def from_record(
    record: object, *, capability: str | None = None, scope: str | None = None
) -> Locator:
    """A locator for the element a registry record last named.

    Duck-typed on ``role``/``name``/``entity`` so this module never imports the
    registry (which imports the compiler), and so a test can pass a lightweight
    stand-in. The record's name is exact evidence; the capability is the
    caller's own requirement and is what turns a structural tie into a
    unique answer.
    """
    return Locator(
        role=getattr(record, "role", None),
        name=getattr(record, "name", None),
        entity=getattr(record, "entity", None),
        capability=capability,
        scope=scope,
    )


def ambiguous_candidates(
    root: SemanticNode, ref: str, *, limit: int = 3
) -> list[str]:
    """Other nodes that share ``ref``'s fingerprint, as candidate lines.

    The runtime defers ambiguity (the named ref is tried first, its twin is the
    fallback), so this hint is attached where a ref genuinely stopped
    resolving: the ``STALE_TARGET`` refusal. It is read from the compiled tree,
    not from the registry, so it costs one walk and never a COM call.
    """
    node = next((item for item in root.walk() if item.ref == ref), None)
    if node is None or not node.fingerprint:
        return []
    same = [
        item
        for item in root.walk()
        if item.ref != ref and item.fingerprint == node.fingerprint
    ]
    result = LocatorResult(locator=Locator(), matches=tuple(same))
    return result.candidate_lines(limit)
