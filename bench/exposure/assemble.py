"""Turn a :class:`~bench.exposure.schemes.Scheme` into a concrete tool surface.

Everything here is mechanical: schemas for merged tools are composed from the
existing ``TOOL_SPECS`` entries (never hand-copied), the system prompt keeps
its body and gets only its ``## Tools`` table regenerated, and the whole
surface is hashed so a run directory proves which surface produced it.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from somnus.protocol.tools import TOOL_SPECS

_SPEC_BY_NAME = {spec["name"]: spec for spec in TOOL_SPECS}

#: The syntax column of the prompt's Tools table, per base tool. Kept here
#: rather than parsed out of ``system_prompt.md`` so a scheme's generated table
#: cannot drift from the wording of the flat baseline.
SYNTAX = {
    "observe": "``OBSERVE [ref] [windows|desktop|full] [expand ref]``",
    "find": "``FIND [type] \"query\" [in ref]``",
    "inspect": "``INSPECT ref [depth=N] [range=A:B]``",
    "call": "``CALL ref.cap(args)``",
    "run": "``CALL …`` / ``AWAIT [TYPE] \"name\" [in ref] [timeout=s]``",
    "help": "``help`` or ``help <topic>``",
    "switch": "``SWITCH main|child`` / ``SESSION open|close|detach``",
    "launch": "``launch(command, args, cwd, window, wait_s)``",
    "configure": "``configure(settle_quiet_ms, settle_poll_ms, …)``",
}

#: Short "use" cell per base tool.
USE = {
    "observe": "L0 overview; assigns refs. First, and when lost",
    "find": "locate by name, including folded lists",
    "inspect": "all caps, args and relations of one node",
    "call": "act; several lines run as one batch",
    "run": "a whole flow in one call",
    "help": "a topic page, on demand",
    "switch": "switch the desktop main/child",
    "launch": "start a program",
    "configure": "tune settle timing",
}

#: Per-router one-line descriptions. A merged tool's schema is composed from
#: its parts; its description states the routing so the model never has to
#: guess which parameter selects which verb.
ROUTER_DESCRIPTION = {
    "look": (
        "One look tool with three modes. Default: L0 overview; assigns refs; "
        "use first/after switch. query: search incl folded lists (find); "
        "FOUND N or 0. ref: one node's full caps/args/relations (inspect). "
        "Returns @rev, WINDOWS, tree."
    ),
    "look_help": (
        "One look tool with four modes. Default: L0 overview; assigns refs. "
        "query: search incl folded lists (find). ref: one node's full detail "
        "(inspect). topic: a help page (help). Returns @rev, WINDOWS, tree."
    ),
    "act": (
        "One action tool. kind=invoke (default): CALL ref.cap(args); ≤20 "
        "lines run as one batch. kind=run: a CALL/AWAIT script, ≤40 steps, "
        "AWAIT binds $it. kind=launch: start a program; returns pid + windows."
    ),
    "act_switch_configure": (
        "One action tool. kind=invoke (default): CALL ref.cap(args), ≤20 "
        "lines one batch. kind=run: CALL/AWAIT script, ≤40 steps. "
        "kind=launch: start a program. target: switch the desktop main/child. "
        "configure: tune settle timing."
    ),
}

#: The folded-verb mapping printed under the regenerated Tools table, per
#: router, so the prompt body's references to find/inspect/run/launch stay
#: resolvable on a folded surface.
ROUTER_NOTE = {
    "look": "`find` = `{name} query=\"…\"`; `inspect` = `{name} ref`.",
    "look_help": "`help <topic>` = `{name} topic=\"<topic>\"`; "
    "`find` = `{name} query=\"…\"`; `inspect` = `{name} ref`.",
    "act": "`run` = `{name} kind=run script=…`; "
    "`launch` = `{name} kind=launch command=…`.",
    "act_switch_configure": "`run` = `{name} kind=run script=…`; "
    "`launch` = `{name} kind=launch command=…`; "
    "`switch` = `{name} target=…`; `configure` = `{name} configure={{…}}`.",
}


@dataclass(frozen=True)
class Surface:
    """One scheme's concrete LLM-facing surface."""

    scheme_id: str
    label: str
    #: TOOL_SPECS-format specs, in exposure order
    specs: tuple[dict[str, Any], ...]
    #: OpenAI ``tools`` payloads built from those specs
    openai_tools: tuple[dict[str, Any], ...]
    #: system prompt with the Tools table regenerated for this surface
    system_prompt: str
    #: sha256 over specs + prompt; frozen into run directories
    surface_hash: str
    #: staged schemes only: the full tool set, revealed when the model calls
    #: ``help``; ``openai_tools`` then holds only the initial subset
    full_openai_tools: tuple[dict[str, Any], ...] | None = None

    def tool_names(self) -> list[str]:
        return [spec["name"] for spec in self.specs]


def assemble(scheme) -> Surface:
    """Build the surface for ``scheme`` (flat tools pass through untouched)."""
    overrides = dict(scheme.desc_overrides)
    specs: list[dict[str, Any]] = []
    notes: list[str] = []
    for exposed, base in scheme.flat:
        source = _SPEC_BY_NAME[base]
        spec = {**source, "name": exposed}
        if exposed in overrides:
            spec["description"] = overrides[exposed]
        specs.append(spec)
    for exposed, router in scheme.merged:
        specs.append(_merged_spec(exposed, router))
        notes.append(ROUTER_NOTE[router].format(name=exposed))

    if scheme.staged_initial:
        # Staged exposure: only the initial tools go into the payload and the
        # prompt's table; the rest exist and are revealed on `help`.
        initial = [s for s in specs if s["name"] in scheme.staged_initial]
        prompt = _build_prompt(scheme, initial, notes)
        prompt += (
            "\nMore tools exist (find, inspect, run, launch, switch, "
            "configure). Call `help` once to reveal them.\n"
        )
        payload = json.dumps({"specs": specs, "prompt": prompt}, ensure_ascii=False)
        return Surface(
            scheme_id=scheme.id,
            label=scheme.label,
            specs=tuple(specs),
            openai_tools=tuple(_openai_format(s) for s in initial),
            system_prompt=prompt,
            surface_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            full_openai_tools=tuple(_openai_format(s) for s in specs),
        )

    prompt = _build_prompt(scheme, specs, notes)
    payload = json.dumps({"specs": specs, "prompt": prompt}, ensure_ascii=False)
    return Surface(
        scheme_id=scheme.id,
        label=scheme.label,
        specs=tuple(specs),
        openai_tools=tuple(_openai_format(spec) for spec in specs),
        system_prompt=prompt,
        surface_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def _merged_spec(exposed: str, router: str) -> dict[str, Any]:
    """One merged tool spec, composed from the base specs it absorbs."""
    props: dict[str, Any] = {}
    if router in ("look", "look_help"):
        props.update(_props("observe"))
        props.update(_props("find"))
        props.update(_props("inspect"))
        if router == "look_help":
            props.update(_props("help"))
    else:
        props.update(_props("call"))
        props.update(_props("run"))
        props.update(_props("launch"))
        if router == "act_switch_configure":
            props.update(_props("switch"))
            props["configure"] = {
                "type": "object",
                "description": "configure tool's arguments verbatim",
                "properties": _SPEC_BY_NAME["configure"]["inputSchema"][
                    "properties"
                ],
            }
    return {
        "name": exposed,
        "description": ROUTER_DESCRIPTION[router],
        "inputSchema": {"type": "object", "properties": props},
    }


def _props(base: str) -> dict[str, Any]:
    """The ``properties`` of one base tool's schema, verbatim."""
    return dict(_SPEC_BY_NAME[base]["inputSchema"]["properties"])


def _openai_format(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["inputSchema"],
        },
    }


_TOOLS_SECTION = re.compile(
    r"## Tools\n.*?(?=\n## )", re.DOTALL
)


def _build_prompt(scheme, specs, notes: list[str]) -> str:
    """The stock system prompt with only its Tools table regenerated."""
    flat_base = dict(scheme.flat)  # exposed -> base name (H5 namespaces, H0)
    rows = ["| Tool | Use | Syntax |", "| --- | --- | --- |"]
    for spec in specs:
        exposed = spec["name"]
        base = flat_base.get(exposed)
        syntax = SYNTAX.get(base or exposed)
        use = USE.get(base or exposed)
        if syntax is None:
            # A merged tool: the syntax cell lists its modes.
            router = next(r for e, r in scheme.merged if e == exposed)
            if router in ("look", "look_help"):
                syntax = "overview · ``query=\"…\"`` · ``ref [depth=N]``"
            else:
                syntax = (
                    "``kind=invoke``: ``CALL ref.cap(args)`` · "
                    "``kind=run``: ``script=\"CALL … AWAIT …\"`` · "
                    "``kind=launch``: ``command=\"…\"``"
                )
            use = ROUTER_DESCRIPTION[router].split(".")[0]
        rows.append(f"| `{exposed}` | {use} | {syntax} |")
    table = "\n".join(rows)
    if notes:
        table += "\n\nFolded verbs: " + " ".join(notes)
    replacement = f"## Tools\n\n{table}\n"
    prompt = _TOOLS_SECTION.sub(replacement, _read_system_prompt())
    # A renamed verb must stay resolvable in the prompt body (rules, error
    # table): word-boundary replace of the old names, after the table was
    # already regenerated so it is never double-hit.
    for old, new in scheme.body_renames:
        prompt = re.sub(rf"\b{old}\b", new, prompt)
    return prompt


def _read_system_prompt() -> str:
    """The stock system prompt, located beside the module that ships it."""
    import somnus

    path = Path(somnus.__file__).parent / "protocol" / "system_prompt.md"
    return path.read_text(encoding="utf-8")
