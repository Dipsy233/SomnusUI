"""The protocol layer: the four tools and the two transports that carry them.

This is the surface every MCP client sees, so a defect here is a defect in the
product's headline feature rather than in an internal helper. Every tool is
exercised through `call_tool` (the single function both transports call), the
JSON-RPC server is exercised method by method including its error replies, and
the MCP server's tool listing is checked against the specs it publishes.

Runs anywhere: the session is backed by the mock provider.
"""

from __future__ import annotations

import io
import json

import pytest

from somnus.providers.mock import MockProvider
from somnus.protocol.jsonrpc import JsonRpcServer
from somnus.protocol.tools import TOOL_SPECS, call_tool
from somnus.runtime.session import Session
from tests.fixtures.builders import demo_scripts, notepad_tree, restaurant_demo_tree


@pytest.fixture
def session():
    provider = MockProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts()
    )
    return Session(provider)


# ---------------------------------------------------------------------------
# tool specs: the contract a client reads before calling anything
# ---------------------------------------------------------------------------


def test_the_nine_tools_are_advertised():
    # The five-tool perception/action loop, plus ``help`` (loads a topic page
    # on demand), ``switch`` (moves the agent between the two Windows
    # sessions), ``launch`` (starts a program) and ``configure`` (tunes call
    # timing). Tier filtering decides which of them a client actually sees.
    names = {spec["name"] for spec in TOOL_SPECS}
    assert names == {
        "observe",
        "find",
        "inspect",
        "call",
        "run",
        "help",
        "switch",
        "launch",
        "configure",
    }


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda s: s["name"])
def test_every_tool_has_a_description_and_a_schema(spec):
    assert spec["description"].strip()
    assert spec["inputSchema"]["type"] == "object"
    # The description must not hand a model the vocabulary of the layer it is
    # meant to sit above. "no pixels" is a promise, not a leak, so the check is
    # for the identifier-shaped terms rather than the words themselves.
    lowered = spec["description"].lower()
    for leak in ("automationid", "automation id", "hwnd", "native_window_handle"):
        assert leak not in lowered, (spec["name"], leak)


def test_only_find_and_call_require_an_argument():
    required = {
        spec["name"]: spec["inputSchema"].get("required", []) for spec in TOOL_SPECS
    }
    assert required["observe"] == []
    assert required["find"] == ["query"]
    assert required["inspect"] == ["ref"]
    assert required["call"] == ["action"]
    assert required["run"] == ["script"]
    assert required["launch"] == ["command"]
    assert required["help"] == []


def test_no_tool_advertises_a_confirmation_switch():
    """This build has no confirmation gate, so no tool asks for consent.

    ``!`` on a capability is metadata, not a permission request: every tool
    executes what it is given.
    """
    for spec in TOOL_SPECS:
        assert "confirmed" not in spec["inputSchema"].get("properties", {}), spec["name"]


# ---------------------------------------------------------------------------
# call_tool: each tool reaches the session
# ---------------------------------------------------------------------------


def test_observe_returns_the_semantic_overview(session):
    text = call_tool(session, "observe")
    assert 'WINDOW w1 "Restaurant Demo"' in text
    assert "PRODUCT" in text


def test_observe_windows_only_lists_windows(session):
    text = call_tool(session, "observe", {"windows_only": True})
    assert "WINDOW w1" in text
    # A window-list answer carries no control tree.
    assert "PRODUCT" not in text


def test_find_accepts_the_find_syntax_verbatim(session):
    text = call_tool(session, "find", {"query": 'FIND product "Big Mac"'})
    assert text.startswith("FOUND 1"), text
    assert "Big Mac" in text


def test_find_accepts_a_bare_query(session):
    text = call_tool(session, "find", {"query": "Big Mac"})
    assert "Big Mac" in text


def test_inspect_reports_capability_parameters(session):
    call_tool(session, "observe")
    text = call_tool(session, "inspect", {"ref": "p1"})
    assert "p1" in text
    assert "Big Mac" in text


def test_inspect_rejects_a_bad_ref_without_raising(session):
    call_tool(session, "observe")
    text = call_tool(session, "inspect", {"ref": "zz99"})
    assert text.startswith("ERR"), text


def test_inspect_accepts_depth_and_a_text_range(session):
    call_tool(session, "observe")
    text = call_tool(session, "inspect", {"ref": "p1", "depth": 2, "start": 0, "end": 10})
    assert not text.startswith("ERR"), text


def test_call_invokes_a_capability(session):
    call_tool(session, "observe")
    text = call_tool(session, "call", {"action": "CALL p1.add()"})
    assert text.startswith("OK"), text
    assert "count" in text


def test_call_reports_a_parse_error_as_a_line(session):
    text = call_tool(session, "call", {"action": "CALL p1.add("})
    assert text.startswith("ERR PARSE_ERROR"), text


def test_call_reports_a_missing_capability_as_a_line(session):
    call_tool(session, "observe")
    text = call_tool(session, "call", {"action": "CALL p1.nope()"})
    assert text.startswith("ERR"), text
    assert "nope" in text


def test_an_unknown_tool_names_itself_and_does_not_raise(session):
    text = call_tool(session, "teleport", {})
    assert text.startswith("ERR PARSE_ERROR"), text
    assert "teleport" in text


def test_a_null_arguments_object_is_tolerated(session):
    # A client may send `"arguments": null`; that must mean "no arguments",
    # not a crash.
    text = call_tool(session, "observe", None)
    assert "WINDOW w1" in text


def test_a_100kb_write_reply_stays_bounded():
    """The write echo and delta share one 80-character value limit.

    Without it the reply carried the full 100 KB twice: once as the delta's
    old/new value and once as the `~ ref text=` echo.
    """
    from somnus.projection.budget import estimate_tokens

    session = Session(MockProvider(fixtures=[notepad_tree("")]))
    text = call_tool(session, "observe")
    ref = next(
        line.split()[1] for line in text.splitlines() if "INPUT" in line
    )
    big = "A" * 100_000
    out = call_tool(session, "call", {"action": f'CALL {ref}.set_text("{big}")'})
    assert out.startswith("OK"), out
    assert "…" in out
    assert "len=100000" in out
    assert len(out) < 400, len(out)
    assert estimate_tokens(out) < 100, estimate_tokens(out)
    session.close()


# ---------------------------------------------------------------------------
# JsonRpcServer: methods, ids and error replies
# ---------------------------------------------------------------------------


def _server(session, capabilities=("core", "desktop")) -> JsonRpcServer:
    # Explicit tiers keep the test independent of the caller's environment.
    return JsonRpcServer(session, capabilities=capabilities)


def test_tools_list_matches_the_specs(session):
    reply = _server(session).handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert reply["id"] == 1
    assert [t["name"] for t in reply["result"]["tools"]] == [
        spec["name"] for spec in TOOL_SPECS
    ]


def test_tools_call_wraps_the_text_in_a_content_block(session):
    reply = _server(session).handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "observe", "arguments": {}},
        }
    )
    block = reply["result"]["content"][0]
    assert block["type"] == "text"
    assert "WINDOW w1" in block["text"]


def test_tools_call_without_arguments_is_allowed(session):
    reply = _server(session).handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "observe"},
        }
    )
    assert "result" in reply


def test_sui_method_runs_a_raw_command(session):
    reply = _server(session).handle(
        {"jsonrpc": "2.0", "id": 4, "method": "sui", "params": {"text": "OBSERVE"}}
    )
    assert "WINDOW w1" in reply["result"]["text"]


def test_an_unknown_method_is_a_jsonrpc_error_not_a_crash(session):
    reply = _server(session).handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/explode"}
    )
    assert reply["error"]["code"] == -32601
    assert "tools/explode" in reply["error"]["message"]


def test_a_missing_id_is_echoed_as_null(session):
    reply = _server(session).handle({"jsonrpc": "2.0", "method": "tools/list"})
    assert reply["id"] is None


def test_serve_reads_line_delimited_json_and_writes_one_reply_per_line(session):
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
        + "\n"  # a blank line is skipped, not answered
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "sui",
                      "params": {"text": "OBSERVE"}}) + "\n"
    )
    stdout = io.StringIO()
    _server(session).serve(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["id"] for line in lines] == [1, 2]


def test_serve_answers_malformed_json_with_a_parse_error(session):
    stdin = io.StringIO("{not json}\n")
    stdout = io.StringIO()
    _server(session).serve(stdin=stdin, stdout=stdout)
    reply = json.loads(stdout.getvalue().strip())
    assert reply["error"]["code"] == -32700


# ---------------------------------------------------------------------------
# the MCP server advertises the same tools the JSON-RPC transport does
# ---------------------------------------------------------------------------


def test_make_mcp_server_lists_the_same_tools(session):
    mcp = pytest.importorskip("mcp")  # the optional dependency

    from somnus.protocol.mcp_server import _make_list_tools, make_mcp_server

    make_mcp_server(session, capabilities=("core", "desktop"))
    # mcp 2.x registers handlers as constructor callbacks taking (ctx, params);
    # call the list_tools handler directly - the closest check that does not
    # start a transport.
    import asyncio

    listed = asyncio.run(_make_list_tools(session, ("core", "desktop"))(None, None))
    tools = listed.tools
    assert [t.name for t in tools] == [spec["name"] for spec in TOOL_SPECS]


def test_mcp_lists_only_the_core_tier(session):
    pytest.importorskip("mcp")
    import asyncio

    from somnus.protocol.mcp_server import _make_list_tools

    listed = asyncio.run(_make_list_tools(session, ("core",))(None, None))
    assert [tool.name for tool in listed.tools] == [
        "observe", "find", "inspect", "call", "run", "help"
    ]


def test_jsonrpc_tools_list_follows_the_selected_tier(session):
    reply = _server(session, capabilities=("core",)).handle(
        {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}
    )
    assert [tool["name"] for tool in reply["result"]["tools"]] == [
        "observe", "find", "inspect", "call", "run", "help"
    ]


def test_build_session_honours_the_persistent_opt_ins(tmp_path):
    """The MCP entry point must read the settings file, not just the flags.

    A MCP client launches this process itself, so a command-line flag can never
    reach it - the settings file is the only way a user's clipboard/keyboard
    opt-in applies. Here that round trip is checked through the session's
    policy.
    """
    from somnus.protocol.mcp_server import build_session

    config = tmp_path / "config.toml"
    config.write_text(
        "allow_clipboard = true\nallow_keyboard = true\n", encoding="utf-8"
    )

    session = build_session(provider="mock", config=str(config))
    try:
        assert session.policy.allow_clipboard is True
        assert session.policy.allow_keyboard is True
    finally:
        session.close()


# ---------------------------------------------------------------------------
# idle recycle (SOMNUS_IDLE_TIMEOUT_S)
# ---------------------------------------------------------------------------


def test_idle_timeout_defaults_to_off(monkeypatch):
    from somnus.protocol.mcp_server import resolve_idle_timeout_s

    monkeypatch.delenv("SOMNUS_IDLE_TIMEOUT_S", raising=False)
    assert resolve_idle_timeout_s() == 0.0


def test_idle_timeout_parses_and_falls_back_to_off(monkeypatch):
    from somnus.protocol.mcp_server import resolve_idle_timeout_s

    monkeypatch.setenv("SOMNUS_IDLE_TIMEOUT_S", "0.05")
    assert resolve_idle_timeout_s() == 0.05
    # A malformed or non-positive value means "off": a server that refuses to
    # start over an ops knob helps nobody.
    for bad in ("bogus", "-3", "0"):
        monkeypatch.setenv("SOMNUS_IDLE_TIMEOUT_S", bad)
        assert resolve_idle_timeout_s() == 0.0, bad


def test_the_idle_timer_swaps_in_a_fresh_provider(session):
    import asyncio

    from somnus.protocol.mcp_server import _RecyclableSession

    fresh = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    recycler = _RecyclableSession(session, lambda: fresh, 0.01)
    original = session.provider

    async def run():
        recycler.arm()
        await asyncio.sleep(0.05)

    asyncio.run(run())
    # Same Session object - settler, policy and trace sink survive - but the
    # provider was swapped, so refs from before the gap are gone.
    assert recycler.current() is session
    assert session.provider is fresh
    assert session.provider is not original


def test_the_call_handler_disarms_and_rearms_the_timer(session):
    pytest.importorskip("mcp")
    import asyncio

    from mcp.types import CallToolRequestParams

    from somnus.protocol.mcp_server import _RecyclableSession, _make_call_tool

    fresh = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    recycler = _RecyclableSession(session, lambda: fresh, 0.01)
    handler = _make_call_tool(recycler, ("core", "desktop"))
    params = CallToolRequestParams(name="observe", arguments={})

    async def run():
        result = await handler(None, params)
        # The handler re-armed on the way out, so the idle gap after it still
        # recycles the provider.
        await asyncio.sleep(0.05)
        return result

    result = asyncio.run(run())
    assert "Restaurant Demo" in result.content[0].text
    assert session.provider is fresh


def test_the_call_handler_survives_a_timeout_of_zero(session):
    pytest.importorskip("mcp")
    import asyncio

    from mcp.types import CallToolRequestParams

    from somnus.protocol.mcp_server import _RecyclableSession, _make_call_tool

    fresh = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    recycler = _RecyclableSession(session, lambda: fresh, 0.0)
    handler = _make_call_tool(recycler, ("core", "desktop"))
    params = CallToolRequestParams(name="observe", arguments={})
    result = asyncio.run(handler(None, params))
    assert "Restaurant Demo" in result.content[0].text
    assert session.provider is not fresh  # nothing fired; no swap happened
