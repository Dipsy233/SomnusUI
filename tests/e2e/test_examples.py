"""The example hosts must stay importable and syntactically sound.

These scripts are host code (they drive a real model), so they are not executed
in CI - but a syntax or import regression should still fail the build.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize("name", ["agent.py", "check_contract.py", "eval_agent.py"])
def test_example_parses(name):
    path = EXAMPLES / name
    assert path.exists(), path
    ast.parse(path.read_text(encoding="utf-8"))


def test_agent_importable_without_model(monkeypatch):
    from examples.agent import LlmClient, openai_tools, run_agent  # noqa: F401

    from somnus.protocol import policy
    from somnus.protocol.tools import TOOL_SPECS

    monkeypatch.delenv("SOMNUS_CAPABILITIES", raising=False)
    tools = openai_tools()
    # The example host advertises the same surface as the MCP server: every
    # tool in every tier when nothing is selected.
    assert {t["function"]["name"] for t in tools} == {
        spec["name"] for spec in policy.filter_tool_specs(TOOL_SPECS)
    }


def test_agent_honours_the_capability_tier(monkeypatch):
    """The example is a host like any other: it lists only the active tiers."""
    from examples.agent import openai_tools

    monkeypatch.setenv("SOMNUS_CAPABILITIES", "core")
    assert {t["function"]["name"] for t in openai_tools()} == {
        "observe", "find", "inspect", "call", "run"
    }


def test_llm_client_headers_include_key_only_when_set():
    from examples.agent import LlmClient

    client = LlmClient("http://127.0.0.1:9999/v1", "m", api_key="secret")
    assert client._headers()["Authorization"] == "Bearer secret"

    client2 = LlmClient("http://127.0.0.1:9999/v1", "m", api_key="")
    client2.api_key = None
    assert "Authorization" not in client2._headers()
