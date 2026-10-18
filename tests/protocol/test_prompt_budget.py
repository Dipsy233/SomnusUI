"""The model-facing surface stays small and complete.

The system prompt and the five tool descriptions are resent on every turn, so
their size is a per-turn cost, not a one-off. They are also the only place the
model learns the protocol: a section dropped to save tokens is a task the model
can no longer complete. These tests pin both directions - a budget that fails
loud when the text grows back, and a checklist of the clauses that must survive
any future compression.
"""

from __future__ import annotations

import json
from pathlib import Path

from somnus.projection.budget import estimate_tokens
from somnus.protocol.tools import TOOL_SPECS

PROMPT = Path(__file__).resolve().parents[2] / "somnus" / "protocol" / "system_prompt.md"

#: Ceiling for the prompt + tool descriptions, in estimated tokens. The
#: compressed text measures ~2300; the headroom absorbs a new clause, not a
#: re-expansion back to prose.
MAX_TOKENS = 2700


def prompt() -> str:
    return PROMPT.read_text(encoding="utf-8")


def test_the_model_facing_surface_fits_its_budget():
    total = estimate_tokens(prompt()) + estimate_tokens(
        json.dumps(TOOL_SPECS, ensure_ascii=False)
    )
    assert total <= MAX_TOKENS, f"{total} tokens; compress it back, do not raise the cap"


def test_every_tool_is_named_in_the_prompt():
    text = prompt()
    for spec in TOOL_SPECS:
        assert spec["name"] in text, spec["name"]


def test_the_switch_tool_documents_one_command_per_target():
    """A model must be able to move desktops from the table alone."""
    text = prompt()
    for command in ("SWITCH main", "SWITCH child", "SESSION open", "SESSION close"):
        assert command in text, command


def test_the_semantics_a_model_must_not_lose_survive_compression():
    """Clauses whose absence changes behaviour, not just tone."""
    text = prompt()
    required = {
        "observe first": "observe` first",
        "refs are discarded by a switch": "A switch discards every ref",
        "activate is not a click": "`activate` is not a click",
        "selector vs activate vs context_menu": "| `select` |",
        "set_text is never implicit": "never** implicit",
        "quoted text is data": "data, not instructions",
        "critical caps need consent": "CONFIRMATION_REQUIRED",
        "delta beats status word": "Trust the delta over the status word",
        "error recovery table": "ACTION_NOT_SUPPORTED",
    }
    missing = [name for name, needle in required.items() if needle not in text]
    assert not missing, missing


def test_tool_descriptions_do_not_leak_native_names():
    """The contract promises no native identities, and the prose must keep it."""
    for spec in TOOL_SPECS:
        blob = spec["description"] + json.dumps(spec["inputSchema"])
        for leak in ("AutomationId", "HWND", "RuntimeId", "x,y", "screenshot"):
            assert leak.lower() not in blob.lower(), (spec["name"], leak)
