"""CDP channel tests.

Two layers are covered separately, because they fail for different reasons:

* the pure parts - engine matching, AX-node conversion, editability detection -
  always run, so a regression there is caught without a browser;
* the live parts need a Chromium with a debug port and are skipped otherwise,
  so the suite stays green on a machine with no browser open.

The live test is the one that matters for the reason this module exists: it
writes into a ``contenteditable`` (the shape an Electron app's editor has) and
checks that the page's *own* input handler ran, which is what distinguishes a
real text insertion from a DOM poke.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from somnus.ir.action import ExecResult
from somnus.providers.cdp import (
    ROLE_MAP,
    SUPPORTED_PRIMITIVES,
    CdpBridge,
    _ax_is_editable,
    _ax_match_score,
    _engine_family,
    ax_node_to_raw,
    candidate_ports,
    configured_ports,
    discover_targets,
)

FIXTURE_PAGE = Path(__file__).resolve().parents[1] / "fixtures" / "cdp_test_page.html"

#: The port the live tests point at. Set ``SOMNUS_CDP_PORTS`` to whatever the
#: test browser was launched with; the defaults are probed either way, so an
#: unset variable simply means "look where a browser usually is".
LIVE_PORT = int(os.environ.get("SOMNUS_CDP_TEST_PORT", "9333"))


def _ax(node_id, role, name="", *, backend=0, children=(), props=()):
    node = {
        "nodeId": str(node_id),
        "role": {"value": role},
        "name": {"value": name},
        "childIds": [str(c) for c in children],
    }
    if backend:
        node["backendDOMNodeId"] = backend
    if props:
        node["properties"] = [{"name": k, "value": {"value": v}} for k, v in props]
    return node


# ---------------------------------------------------------------------------
# role mapping and AX conversion (pure)
# ---------------------------------------------------------------------------


def test_cdp_roles_map_to_portable_roles():
    assert ROLE_MAP["button"] == "button"
    assert ROLE_MAP["statictext"] == "text"
    assert ROLE_MAP["inlinetextbox"] == "text"
    assert ROLE_MAP["textbox"] == "input"
    assert ROLE_MAP["combobox"] == "select"
    assert ROLE_MAP["checkbox"] == "checkbox"
    assert ROLE_MAP["rootwebarea"] == "group"
    assert ROLE_MAP["option"] == "listitem"


def test_ax_tree_converts_with_patterns_and_states():
    """The web tree becomes the same RawNode shape a native provider produces,
    which is what lets the compiler and runtime stay platform free."""
    root = _ax(1, "RootWebArea", "测试页", backend=100, children=[2, 3])
    nodes = {
        "1": root,
        "2": _ax(2, "textbox", "消息输入框", backend=15,
                 props=[("editable", "richtext")]),
        "3": _ax(3, "button", "发送", backend=19),
    }
    raw = ax_node_to_raw(root, nodes, window_id="w1")
    assert raw.role == "group"
    assert raw.name == "测试页"
    by_backend = {n.native.runtime_id[0]: n for n in raw.walk() if n.native.runtime_id}
    assert by_backend[15].role == "input"
    # The textbox advertises the web counterpart of a Value pattern, which is
    # what makes the existing capability derivation grant set_text.
    assert "value" in by_backend[15].patterns
    assert by_backend[19].role == "button"
    assert "invoke" in by_backend[19].patterns


def test_inline_text_boxes_are_folded_into_their_parent():
    """InlineTextBox nodes are rendering fragments: walking them would repeat
    every paragraph once per fragment."""
    root = _ax(1, "RootWebArea", children=[2])
    nodes = {
        "1": root,
        "2": _ax(2, "StaticText", "hello", children=[3, 4]),
        "3": _ax(3, "InlineTextBox", "he"),
        "4": _ax(4, "InlineTextBox", "llo"),
    }
    raw = ax_node_to_raw(root, nodes, window_id="w1")
    text_node = next(n for n in raw.walk() if n.role == "text")
    assert text_node.children == []


# ---------------------------------------------------------------------------
# engine matching: the process a UIA window belongs to vs the page's browser name
# ---------------------------------------------------------------------------


def test_engine_family_matches_a_process_to_its_browser_product():
    """A UIA window names a process (``msedge.exe``) while a page names the
    engine product (``Edg``); only normalising both ties them together."""
    assert _engine_family("msedge.exe") == "chromium"
    assert _engine_family("Edg") == "chromium"
    assert _engine_family("Chrome") == "chromium"
    assert _engine_family("chrome.exe") == "chromium"


def test_engine_family_refuses_an_unknown_process():
    """Only Chromium engines can be driven this way. Returning None is what
    keeps the channel from being offered to an application it cannot serve -
    and, more importantly, from matching the wrong one."""
    assert _engine_family("QQ.exe") is None
    assert _engine_family("notepad.exe") is None
    assert _engine_family("") is None
    # A substring match would wrongly accept this; the comparison is on the
    # product word, not on whether the text contains a known name.
    assert _engine_family("edgeviewer.exe") is None


def test_bridge_finds_no_target_for_an_unknown_process():
    bridge = CdpBridge()
    assert bridge.target_for_process("QQ.exe") is None


# ---------------------------------------------------------------------------
# AX editability / matching (pure)
# ---------------------------------------------------------------------------


def test_editability_follows_the_browser_not_the_role_alone():
    assert _ax_is_editable(_ax(1, "textbox", props=[("editable", "richtext")]))
    assert _ax_is_editable(_ax(1, "textbox", props=[("editable", "plaintext")]))
    # A textbox role is the browser's own word for an editable region, even
    # when the editable property is not spelled out.
    assert _ax_is_editable(_ax(1, "textbox"))
    assert not _ax_is_editable(_ax(1, "button"))
    assert not _ax_is_editable(_ax(1, "StaticText"))


def test_match_score_prefers_class_then_name_then_shape():
    plain = _ax(1, "textbox")
    named = _ax(1, "textbox", "消息输入框")
    classed = _ax(1, "textbox", props=[("className", "ProseMirror ExEditor-qq")])
    focused = _ax(1, "textbox", props=[("focused", True)])
    assert _ax_match_score(classed, "ProseMirror", "") > _ax_match_score(named, "", "消息输入框")
    assert _ax_match_score(named, "", "消息输入框") > _ax_match_score(focused, "", "")
    assert _ax_match_score(named, "", "消息输入框") > _ax_match_score(plain, "", "")


def test_set_text_is_an_advertised_primitive():
    assert "set_text" in SUPPORTED_PRIMITIVES


def test_ports_are_configurable_and_defaults_are_kept(monkeypatch):
    """An application on a non-default debug port must be reachable without a
    code change, and the defaults must stay in the probe list."""
    from somnus.providers.cdp import PORTS_ENV, configured_ports

    monkeypatch.setenv(PORTS_ENV, "9333, 9444 , garbage")
    ports = configured_ports()
    assert 9333 in ports and 9444 in ports
    assert ports[:3] == (9222, 9223, 9224)
    # A malformed entry is ignored rather than crashing a text write.
    assert all(isinstance(p, int) for p in ports)

    monkeypatch.delenv(PORTS_ENV, raising=False)
    assert configured_ports() == (9222, 9223, 9224)


def test_probe_does_not_pay_two_timeouts_per_closed_port():
    """A probe runs on every text write, so a closed port must cost one
    timeout, not two.

    Regression: the port was probed for ``/json/version`` and then
    ``/json/list``, and a closed port costs the full timeout each time. With
    the conventional ports that was ~5s of latency on every write to an
    application that - in the common case - is not debuggable at all (measured
    live). A single request per port is what keeps the miss cheap.
    """
    import somnus.providers.cdp as cdp

    calls: list[str] = []
    real_http = cdp._http_json
    real_ports = cdp.candidate_ports

    def counting(port, path, timeout=cdp._PROBE_TIMEOUT):
        calls.append((port, path))
        return real_http(port, path, timeout)

    # Isolate from any real browser on this machine: the point is how many
    # requests a *single* closed port costs.
    cdp._http_json = counting
    cdp.candidate_ports = lambda ports: list(ports)
    try:
        cdp.discover_targets((1,))
    finally:
        cdp._http_json = real_http
        cdp.candidate_ports = real_ports
    assert calls == [(1, "/json/list")], calls


# ---------------------------------------------------------------------------
# live: a real Chromium must accept a programmatic write
# ---------------------------------------------------------------------------


def _live_target():
    """A reachable page other than about:blank, or skip the test."""
    try:
        # Probe the configured ports as well as the defaults, so a browser on a
        # non-standard port is found the same way the provider would find it.
        targets = discover_targets(configured_ports() + (LIVE_PORT,))
    except Exception as exc:  # noqa: BLE001 - treated as "no browser"
        pytest.skip(f"CDP probe failed: {exc}")
    page = next((t for t in targets if "cdp_test_page" in t.url), None)
    if page is None:
        pytest.skip(
            "no live CDP test page; launch one with "
            f"--remote-debugging-port={LIVE_PORT} on {FIXTURE_PAGE}"
        )
    return page


def test_live_write_into_a_contenteditable_runs_the_pages_own_handler():
    """The reason this channel exists, end to end.

    A ``contenteditable`` has no ``.value`` and UIA cannot write into it, so
    this is the only programmatic route. The check is not merely "the text is
    there": the page's own ``input`` listener must have run, which is what a
    real text insertion does and what assigning ``textContent`` does not.
    """
    page = _live_target()
    bridge = CdpBridge(ports=(LIVE_PORT,))
    bridge.discover(refresh=True)
    conn = bridge.connection(page)
    def evaluate(expr):
        res = conn.send(
            "Runtime.evaluate", {"expression": expr, "returnByValue": True}
        )
        return (res.get("result") or {}).get("value")

    text = "不依赖键盘写入自绘 UI 12345"
    evaluate("window.__clear && window.__clear()")
    time.sleep(0.2)

    result = bridge.set_text("msedge", text)
    assert result.ok, result
    time.sleep(0.3)

    # The editor holds the text...
    assert evaluate("window.__readEditor()") == text
    # ...and the page's input handler saw it, proving the real pipeline ran.
    mirror = evaluate('document.getElementById("mirror").textContent')
    assert mirror == json.dumps(text, ensure_ascii=False)

    # A second write replaces rather than appends (focus + select happened).
    assert bridge.set_text("msedge", "REPLACED").ok
    time.sleep(0.3)
    assert evaluate("window.__readEditor()") == "REPLACED"

    bridge.close()
