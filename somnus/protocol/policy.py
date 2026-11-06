"""Internal tool metadata and capability tiers.

This table is the single source of truth for **what a model is allowed to see**.
It is not part of the wire format: :mod:`somnus.protocol.tools` still owns the
schemas, and this module only decides which of them a transport lists. Keeping
the two apart means a metadata change can never alter a schema by accident.

Tiers exist so a host can pay for the surface it needs:

* ``core`` - the perception/action loop: ``observe``, ``find``, ``inspect``,
  ``call``, ``run``. Always on.
* ``desktop`` - moving between desktops and starting programs: ``switch``,
  ``launch``, ``configure``.
* ``docs`` - the ``help`` tool that loads a topic document on demand.

Selection comes from ``SOMNUS_CAPABILITIES`` (comma-separated tier names, or
``all``). Unset means every tier, so an existing host keeps the surface it
already had. ``core`` is implied: selecting ``desktop`` adds to the core loop
rather than replacing it. An unknown tier name is ignored and the defaults are
used; a wrong selection must not silently take tools away.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

CORE = "core"
DESKTOP = "desktop"
DOCS = "docs"

#: Declaration order is also listing order: core first, optional tiers after.
CAPABILITY_ORDER: tuple[str, ...] = (CORE, DESKTOP, DOCS)

#: What an unset or invalid selection means.
DEFAULT_CAPABILITIES: tuple[str, ...] = CAPABILITY_ORDER

ENV_CAPABILITIES = "SOMNUS_CAPABILITIES"


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """What one tool *is*, in terms the protocol can reason about.

    ``capability`` decides visibility. ``kind`` separates observation from
    action from configuration so a host can, for example, render them apart.
    ``includes_delta`` marks a tool whose reply carries a tree delta; read-only
    tools never do.
    """

    capability: str
    kind: str
    includes_delta: bool = False


#: Every advertised tool, by name. The parity with ``TOOL_SPECS`` is checked by
#: a test, so a tool cannot be added without a tier.
TOOL_POLICY: dict[str, ToolPolicy] = {
    "observe": ToolPolicy(CORE, "read"),
    "find": ToolPolicy(CORE, "read"),
    "inspect": ToolPolicy(CORE, "read"),
    "call": ToolPolicy(CORE, "act", includes_delta=True),
    "run": ToolPolicy(CORE, "act", includes_delta=True),
    "help": ToolPolicy(DOCS, "read"),
    "switch": ToolPolicy(DESKTOP, "act"),
    "launch": ToolPolicy(DESKTOP, "act"),
    "configure": ToolPolicy(DESKTOP, "config"),
}


def tool_policy(name: str) -> ToolPolicy | None:
    return TOOL_POLICY.get(name)


def parse_capabilities(value: str) -> tuple[str, ...]:
    """Turn ``"core,desktop"`` into an ordered, de-duplicated tier tuple.

    ``all`` (or an empty value) means every tier. ``core`` is always included,
    because a selection chooses what to **add** to the core loop, not what to
    remove from it.
    """
    tokens = [
        token.strip().lower()
        for token in value.replace(";", ",").split(",")
        if token.strip()
    ]
    if not tokens or "all" in tokens:
        return DEFAULT_CAPABILITIES
    unknown = sorted({token for token in tokens if token not in CAPABILITY_ORDER})
    if unknown:
        raise ValueError(
            "unknown capability "
            + ", ".join(unknown)
            + f"; expected one of {', '.join(CAPABILITY_ORDER)} or all"
        )
    chosen = {token for token in tokens if token in CAPABILITY_ORDER}
    chosen.add(CORE)
    return tuple(tier for tier in CAPABILITY_ORDER if tier in chosen)


def resolve_capabilities(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """The tiers in force for this process.

    A malformed ``SOMNUS_CAPABILITIES`` falls back to the defaults instead of
    raising: the MCP server has no place to show a startup error, and losing
    ``call`` because of a typo would be worse than ignoring the typo.
    """
    env = os.environ if environ is None else environ
    raw = env.get(ENV_CAPABILITIES)
    if not raw or not raw.strip():
        return DEFAULT_CAPABILITIES
    try:
        return parse_capabilities(raw)
    except ValueError:
        return DEFAULT_CAPABILITIES


def active_capabilities(
    selection: Iterable[str] | str | None = None,
) -> tuple[str, ...]:
    """Normalise a tier selection.

    ``None`` reads this process's environment; a string is parsed the same way
    ``SOMNUS_CAPABILITIES`` is; any other iterable is taken as already parsed.
    Accepting a bare tier name keeps ``("core",)`` and ``"core"`` from meaning
    two different things at a call site.
    """
    if selection is None:
        return resolve_capabilities()
    if isinstance(selection, str):
        return parse_capabilities(selection)
    return tuple(selection)


def tool_names(
    capabilities: Iterable[str] | str | None = None,
) -> tuple[str, ...]:
    """The names visible under *capabilities* (default: this process)."""
    active = set(active_capabilities(capabilities))
    return tuple(
        name for name, policy in TOOL_POLICY.items() if policy.capability in active
    )


def filter_tool_specs(
    specs: Sequence[dict],
    capabilities: Iterable[str] | str | None = None,
) -> list[dict]:
    """Return the specs in *specs* whose tier is active, preserving order.

    A spec without metadata is kept: dropping a tool silently is worse than
    listing one whose tier was forgotten, and a test fails on that gap anyway.
    """
    active = set(active_capabilities(capabilities))
    out: list[dict] = []
    for spec in specs:
        policy = TOOL_POLICY.get(str(spec.get("name", "")))
        if policy is None or policy.capability in active:
            out.append(spec)
    return out


def tier_tools(tier: str) -> tuple[str, ...]:
    return tuple(
        name for name, policy in TOOL_POLICY.items() if policy.capability == tier
    )
