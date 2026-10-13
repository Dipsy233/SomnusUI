"""YAML App Pack loader (dev doc section 15, v0.2).

A pack is a single YAML file that replaces the v0.1 Python module. The loader
produces the exact same :class:`~somnus.compiler.pack_schema.Pack` objects the
compiler already consumes, so nothing downstream changes.

Format::

    app:
      id: notepad
      version: "0.2.0"
      match: { process: "notepad.exe" }
    projection:
      - { select: "role=statusbar", rule: "collapse" }
    named_refs:
      editor: { role: input, name: "/text editor/i" }
    entities:
      document:
        match:
          role: input
          has_name: true
        key: "$self.automation_id"
        attrs:
          name: "$self.name"
          words: { from: "$self.value", regex: "(\\\\w+)", cast: int }
        caps:
          set_text:
            via: ["$self.set_text"]
            params:
              text: { type: string, required: true }
            effects: []
            risk: normal

Selector expressions inside ``attrs``/``key``/``via`` accept either
``$member.attr`` (a captured ``as`` member) or ``$self.attr``.

``descendant(role=text, name=/.../)`` is also accepted as an attribute source:
it selects the first descendant matching the inline predicate, which mirrors the
documented ``attrs: { price: { from: "descendant(role=text)", ... } }`` form.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from somnus.compiler.pack_schema import (
    EntityCapDef,
    EntityDef,
    MatchRule,
    Pack,
    ProjectionRule,
    register,
)
from somnus.ir.capability import ParamSpec

try:  # PyYAML is a soft dependency; packs load only when present.
    import yaml
except Exception:  # pragma: no cover - optional
    yaml = None


#: Directory searched for built-in ``*.pack.yaml`` files.
BUILTIN_PACK_DIR = Path(__file__).resolve().parent.parent / "packs"


class PackLoadError(ValueError):
    """Raised when a YAML pack is malformed (fail loud, never guess)."""


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def load_pack_dict(data: dict[str, Any]) -> Pack:
    """Build a :class:`Pack` from an already-parsed YAML mapping."""
    app = data.get("app") or {}
    pack_id = app.get("id")
    if not pack_id:
        raise PackLoadError("pack is missing app.id")

    match = app.get("match") or {}
    process = match.get("process")
    window_class = match.get("window_class") or match.get("class")
    if isinstance(process, str):
        process = [process]
    if isinstance(window_class, str):
        window_class = [window_class]

    entities = [_entity_def(name, body) for name, body in (data.get("entities") or {}).items()]
    projection = [
        ProjectionRule(select=str(r.get("select", "")), rule=str(r.get("rule", "keep")))
        for r in (data.get("projection") or [])
    ]
    named_refs = {
        name: _match_rule(body or {}) for name, body in (data.get("named_refs") or {}).items()
    }

    return Pack(
        id=str(pack_id),
        match_process=tuple(process or ()),
        match_window_class=tuple(window_class or ()),
        entities=entities,
        projection=projection,
        named_refs=named_refs,
        version=str(app.get("version", "0.2.0")),
    )


def load_pack_file(path: str | Path) -> Pack:
    """Parse one ``*.pack.yaml`` file and register it."""
    if yaml is None:  # pragma: no cover - optional dependency
        raise PackLoadError("PyYAML is required to load YAML packs")
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise PackLoadError(f"{path}: top level must be a mapping")
    pack = load_pack_dict(data)
    register(pack)
    return pack


def load_builtin_packs() -> list[Pack]:
    """Load every ``*.pack.yaml`` next to this module (idempotent)."""
    if yaml is None:  # pragma: no cover - optional dependency
        return []
    packs: list[Pack] = []
    for path in sorted(BUILTIN_PACK_DIR.glob("*.pack.yaml")):
        try:
            packs.append(load_pack_file(path))
        except PackLoadError:
            raise
    return packs


# ---------------------------------------------------------------------------
# match rules
# ---------------------------------------------------------------------------


def _match_rule(body: dict[str, Any]) -> MatchRule:
    role = body.get("role")
    if isinstance(role, list):
        role = tuple(str(r) for r in role)

    has = []
    for sub in body.get("has") or []:
        rule = _match_rule(sub)
        rule.alias = sub.get("as")
        has.append(rule)

    return MatchRule(
        role=role,
        name=body.get("name"),
        value=body.get("value"),
        has_name=body.get("has_name"),
        min_children=body.get("min_children"),
        has_patterns=tuple(body.get("patterns") or ()),
        has=has,
    )


# ---------------------------------------------------------------------------
# entities
# ---------------------------------------------------------------------------


def _entity_def(name: str, body: dict[str, Any]) -> EntityDef:
    match = _match_rule(body.get("match") or {})
    caps = {
        cap_name: _cap_def(cap_name, cap_body or {})
        for cap_name, cap_body in (body.get("caps") or {}).items()
    }
    return EntityDef(
        name=name,
        match=match,
        key=body.get("key"),
        attrs=dict(body.get("attrs") or {}),
        caps=caps,
        aliases=dict(body.get("aliases") or {}),
        confidence=float(body.get("confidence", 1.0)),
        role_override=body.get("role"),
    )


def _cap_def(name: str, body: dict[str, Any]) -> EntityCapDef:
    params = {
        pname: _param_spec(pbody or {})
        for pname, pbody in (body.get("params") or {}).items()
    }
    via = body.get("via") or []
    if isinstance(via, str):
        via = [via]
    effects = body.get("effects") or []
    if isinstance(effects, str):
        effects = [effects]
    return EntityCapDef(
        name=name,
        via=[str(v) for v in via],
        effects=[str(e) for e in effects],
        params=params,
        risk=str(body.get("risk", "normal")),  # type: ignore[arg-type]
        aliases=tuple(body.get("aliases") or ()),
    )


def _param_spec(body: dict[str, Any]) -> ParamSpec:
    enum = body.get("enum")
    return ParamSpec(
        type=str(body.get("type", "string")),
        required=bool(body.get("required", False)),
        enum=tuple(enum) if enum else (),
        default=body.get("default"),
    )
