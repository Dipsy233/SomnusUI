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


def test_agent_importable_without_model():
    from examples.agent import LlmClient, openai_tools, run_agent  # noqa: F401

    tools = openai_tools()
    # ``switch`` moves the agent between the two Windows sessions (and opens or
    # closes the child one); it is part of the tool surface, not an extra.
    assert {t["function"]["name"] for t in tools} == {
        "observe",
        "find",
        "inspect",
        "call",
        "switch",
    }


def test_llm_client_headers_include_key_only_when_set():
    from examples.agent import LlmClient

    client = LlmClient("http://127.0.0.1:9999/v1", "m", api_key="secret")
    assert client._headers()["Authorization"] == "Bearer secret"

    client2 = LlmClient("http://127.0.0.1:9999/v1", "m", api_key="")
    client2.api_key = None
    assert "Authorization" not in client2._headers()
