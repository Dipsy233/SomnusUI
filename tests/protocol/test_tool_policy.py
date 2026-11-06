"""Tool metadata decides the model-visible surface.

The table in ``protocol.policy`` is the single source of truth for tiers. These
tests pin the three tiers, the default, and the discovery reply, without
starting a transport.
"""

from __future__ import annotations

import pytest

from somnus.protocol import policy
from somnus.protocol.help import TOPICS, render_help, topic_text
from somnus.protocol.tools import TOOL_SPECS, call_tool

CORE_TOOLS = ["observe", "find", "inspect", "call", "run", "help"]
DESKTOP_TOOLS = ["switch", "launch", "configure"]


def listed(capabilities: tuple[str, ...]) -> list[str]:
    return [spec["name"] for spec in policy.filter_tool_specs(TOOL_SPECS, capabilities)]


def test_every_advertised_tool_has_metadata():
    """A tool cannot be added without deciding its tier."""
    assert set(policy.TOOL_POLICY) == {spec["name"] for spec in TOOL_SPECS}


def test_the_core_tier_is_the_perception_action_loop_plus_discovery():
    # ``help`` stays in the smallest surface: without it a core-only host
    # cannot list what it is missing.
    assert listed(("core",)) == CORE_TOOLS


def test_desktop_adds_the_session_tools():
    assert listed(("core", "desktop")) == CORE_TOOLS + DESKTOP_TOOLS


def test_the_default_is_every_tier():
    assert policy.resolve_capabilities({}) == policy.DEFAULT_CAPABILITIES
    assert listed(policy.DEFAULT_CAPABILITIES) == [s["name"] for s in TOOL_SPECS]


def test_core_is_implied_by_an_optional_tier():
    assert policy.parse_capabilities("desktop") == ("core", "desktop")


def test_all_or_blank_means_every_tier():
    assert policy.parse_capabilities("all") == policy.DEFAULT_CAPABILITIES
    assert policy.parse_capabilities("   ") == policy.DEFAULT_CAPABILITIES


def test_parse_names_unknown_tiers_to_a_caller_that_asks():
    with pytest.raises(ValueError):
        policy.parse_capabilities("desktop,teleport")


def test_an_unknown_environment_value_falls_back_to_the_default():
    """Losing a tool because of a typo would be worse than ignoring the typo."""
    assert policy.resolve_capabilities(
        {policy.ENV_CAPABILITIES: "teleport"}
    ) == policy.DEFAULT_CAPABILITIES


def test_the_environment_selects_the_surface():
    environ = {policy.ENV_CAPABILITIES: "core"}
    assert policy.resolve_capabilities(environ) == ("core",)
    assert list(policy.tool_names(policy.resolve_capabilities(environ))) == CORE_TOOLS


def test_filtering_keeps_spec_objects_and_order():
    core = policy.filter_tool_specs(TOOL_SPECS, ("core",))
    assert core == [spec for spec in TOOL_SPECS if spec["name"] in CORE_TOOLS]


# ---------------------------------------------------------------------------
# help: discovery text
# ---------------------------------------------------------------------------


def test_help_without_a_topic_lists_tiers_tools_and_topics():
    text = render_help(None, ("core",))
    assert "enabled: core" in text
    assert "available: desktop -> switch launch configure" in text
    assert "tools: observe find inspect call run help" in text
    for name in TOPICS:
        assert name in text, name


def test_help_with_every_tier_has_nothing_to_enable():
    assert "available:" not in render_help(None, policy.DEFAULT_CAPABILITIES)


def test_a_topic_renders_the_document_verbatim():
    assert render_help("format", ("core",)) == topic_text("format")


def test_an_unknown_topic_lists_the_available_topics():
    text = render_help("teleport", ("core",))
    assert text.startswith('HELP unknown topic "teleport"')
    for name in TOPICS:
        assert name in text, name


def test_the_help_tool_needs_no_session_and_honours_the_tier():
    text = call_tool(None, "help", {"topic": "batching"}, capabilities=("core",))
    assert text == topic_text("batching")
    summary = call_tool(None, "help", None, capabilities=("core",))
    assert "SOMNUS_CAPABILITIES=desktop" in summary


def test_the_help_tool_refuses_a_non_string_topic():
    text = call_tool(None, "help", {"topic": 7}, capabilities=("core",))
    assert text.startswith("ERR INVALID_ARGUMENT"), text
