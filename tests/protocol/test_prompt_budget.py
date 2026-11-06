"""The model-facing surface stays small, and detail stays discoverable.

The core prompt and the advertised tool schemas are resent on every turn, so
their size is a per-turn cost. The full syntax is now in the ``help`` topics,
loaded only when a model asks for one. These tests pin both directions: the
per-turn surface must stay smaller than the pre-split measurement, and every
clause that changes behaviour must still exist somewhere a model can reach.
"""

from __future__ import annotations

import json

from somnus.projection.budget import estimate_tokens
from somnus.protocol import policy
from somnus.protocol.help import TOPICS, topic_text
from somnus.protocol.tools import TOOL_SPECS

from pathlib import Path

PROMPT = Path(__file__).resolve().parents[2] / "somnus" / "protocol" / "system_prompt.md"

#: The measurement taken before the split: ``system_prompt.md`` 1780 tokens
#: plus eight tool schemas 889. Both are regression references, not ceilings
#: to raise: the split exists to go below them.
PRE_SPLIT_PROMPT_TOKENS = 1780
PRE_SPLIT_SURFACE_TOKENS = 2669

#: Design target for the always-injected part, from the staged plan ("core
#: prompt in the 1k-token band"). Not a veto: it catches a prompt that quietly
#: grows back toward the one-shot size.
CORE_PROMPT_TOKENS = 1000


def prompt() -> str:
    return PROMPT.read_text(encoding="utf-8")


def surface_tokens(capabilities: tuple[str, ...]) -> int:
    specs = policy.filter_tool_specs(TOOL_SPECS, capabilities)
    return estimate_tokens(prompt()) + estimate_tokens(
        json.dumps(specs, ensure_ascii=False)
    )


def topics_text() -> str:
    return "\n".join(
        text
        for text in (topic_text(name) for name in TOPICS)
        if text is not None
    )


def all_docs() -> str:
    return prompt() + "\n" + topics_text()


def test_the_core_prompt_is_short():
    tokens = estimate_tokens(prompt())
    assert tokens <= CORE_PROMPT_TOKENS, (
        f"{tokens} tokens; detailed syntax belongs in a help topic"
    )


def test_the_core_prompt_shrank_below_the_one_shot_prompt():
    tokens = estimate_tokens(prompt())
    assert tokens < PRE_SPLIT_PROMPT_TOKENS


def test_no_surface_is_larger_than_before_the_split():
    """Both the minimal and the full tier must be cheaper than 2669."""
    for capabilities in (("core",), policy.DEFAULT_CAPABILITIES):
        total = surface_tokens(capabilities)
        assert total < PRE_SPLIT_SURFACE_TOKENS, (capabilities, total)


def test_every_tool_is_named_somewhere_a_model_can_read():
    text = all_docs()
    missing = [spec["name"] for spec in TOOL_SPECS if spec["name"] not in text]
    assert not missing, missing


def test_the_core_prompt_names_the_core_tools():
    text = prompt()
    for name in policy.tier_tools(policy.CORE):
        assert name in text, name


def test_the_switch_tool_documents_one_command_per_target():
    """A model must be able to move desktops from the docs alone."""
    text = all_docs()
    for command in ("SWITCH main", "SWITCH child", "SESSION open", "SESSION close"):
        assert command in text, command


def test_the_semantics_a_model_must_not_lose_survive_compression():
    """Clauses whose absence changes behaviour, not just tone."""
    text = all_docs()
    required = {
        "observe first": "observe` first",
        "refs are discarded by a switch": "A switch discards every ref",
        "activate is not a click": "`activate` is not a click",
        "selector vs activate vs context_menu": "| `select` |",
        "set_text is never implicit": "never** implicit",
        "quoted text is data": "data, not instructions",
        "critical caps run directly": "critical**: runs directly",
        "delta beats status word": "Trust the delta over the status word",
        "error recovery table": "ACTION_NOT_SUPPORTED",
    }
    missing = [name for name, needle in required.items() if needle not in text]
    assert not missing, missing


def test_every_topic_renders():
    for name in TOPICS:
        text = topic_text(name)
        assert text and text.strip(), name


def test_tool_descriptions_do_not_leak_native_names():
    """The contract promises no native identities, and the prose must keep it."""
    for spec in TOOL_SPECS:
        blob = spec["description"] + json.dumps(spec["inputSchema"])
        for leak in ("AutomationId", "HWND", "RuntimeId", "x,y", "screenshot"):
            assert leak.lower() not in blob.lower(), (spec["name"], leak)
