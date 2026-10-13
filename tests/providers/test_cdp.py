"""CDP provider tests.

The AX-tree conversion and role mapping are pure and always testable. The live
browser tests are marked ``cdp`` and skipped unless a debug port is reachable,
so the suite stays green on a machine with no browser open.
"""

from __future__ import annotations

import pytest

from somnus.ir.node import NativeRef
from somnus.providers.cdp import (
    ROLE_MAP,
    ax_node_to_raw,
    discover_targets,
)


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
# role mapping
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


# ---------------------------------------------------------------------------
# AX -> RawNode
# ---------------------------------------------------------------------------


def test_ax_tree_converts_with_patterns_and_states():
    root = _ax(1, "RootWebArea", "测试页", backend=100, children=[2, 3, 4])
    nodes = {
        "1": root,
        "2": _ax(2, "button", "登录", backend=101),
        "3": _ax(3, "checkbox", "记住我", backend=102, props=[("checked", "false")]),
        "4": _ax(4, "textbox", "用户名", backend=103, props=[("required", True)]),
    }
    raw = ax_node_to_raw(root, nodes, window_id="w1")

    assert raw.role == "group" and raw.name == "测试页"
    by_name = {c.name: c for c in raw.children}
    assert by_name["登录"].role == "button" and "invoke" in by_name["登录"].patterns
    assert by_name["记住我"].role == "checkbox"
    assert "unchecked" in by_name["记住我"].states
    assert "toggle" in by_name["记住我"].patterns
    assert by_name["用户名"].role == "input"
    assert "value" in by_name["用户名"].patterns
    assert "required" in by_name["用户名"].states


def test_inline_text_boxes_are_folded():
    """CDP emits one InlineTextBox per rendering run; they add no semantics."""
    root = _ax(1, "paragraph", "", children=[2, 3])
    nodes = {
        "1": root,
        "2": _ax(2, "InlineTextBox", "第一段"),
        "3": _ax(3, "InlineTextBox", "第二段"),
    }
    raw = ax_node_to_raw(root, nodes, window_id="w1")
    assert raw.children == []


def test_ax_node_carries_backend_id_for_actions():
    node = _ax(1, "button", "Go", backend=555)
    raw = ax_node_to_raw(node, {"1": node}, window_id="w1")
    assert raw.native.runtime_id == (555,)


def test_checked_tristate_maps_to_state():
    for cdp_value, expected in (("true", "checked"), ("false", "unchecked"), ("mixed", "mixed")):
        node = _ax(1, "checkbox", "x", props=[("checked", cdp_value)])
        raw = ax_node_to_raw(node, {"1": node}, window_id="w1")
        assert expected in raw.states


def test_disabled_and_expanded_states():
    node = _ax(
        1, "button", "x",
        props=[("disabled", True), ("expanded", True), ("focused", True)],
    )
    raw = ax_node_to_raw(node, {"1": node}, window_id="w1")
    assert {"disabled", "expanded", "focused"} <= set(raw.states)


# ---------------------------------------------------------------------------
# live browser (optional)
# ---------------------------------------------------------------------------

_cdp_targets = discover_targets()


@pytest.mark.skipif(not _cdp_targets, reason="no browser debug port reachable")
def test_live_cdp_produces_a_semantic_tree():
    """End-to-end against whatever browser page is available."""
    from somnus.providers.cdp import CdpProvider
    from somnus.runtime.session import Session

    provider = CdpProvider()
    session = Session(provider)
    observation = session.observe()
    assert observation.text.startswith("@rev")
    assert "WINDOW" in observation.text
    provider.close()
