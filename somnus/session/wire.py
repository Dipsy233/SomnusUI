"""Serialize the provider's own types for the cross-session pipe.

The peer exposes the *provider* interface, not the tool interface. That choice
is what makes a session switch cheap: :class:`~somnus.runtime.session.Session`
already takes an ``AccessibilityProvider``, so the far session is just another
provider - one whose windows happen to live on the other side of the session
boundary. The compiler, projection, registry, resolver and policy all stay here
and never learn that anything is remote.

Everything crossing the pipe is a dataclass from :mod:`somnus.ir.node`, so the
encoding is a direct field mapping. Two conversions are not identities and are
worth naming:

* ``states`` / ``patterns`` are ``frozenset[str]``; JSON has no sets, so they
  travel as sorted lists and are rebuilt as frozensets.
* ``runtime_id`` / ``path`` are tuples of ints; JSON turns them into lists, so
  they are rebuilt as tuples. Leaving them as lists would break ``NativeRef``
  equality, which is how the session resolves a ref back to a native element.
"""

from __future__ import annotations

from typing import Any

from somnus.ir.action import ExecResult
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

# ---------------------------------------------------------------------------
# NativeRef
# ---------------------------------------------------------------------------


def native_ref_to_dict(ref: NativeRef) -> dict[str, Any]:
    return {
        "window_id": ref.window_id,
        "runtime_id": list(ref.runtime_id),
        "automation_id": ref.automation_id,
        "class_name": ref.class_name,
        "control_type": ref.control_type,
        "path": list(ref.path),
    }


def native_ref_from_dict(data: dict[str, Any]) -> NativeRef:
    return NativeRef(
        window_id=data["window_id"],
        runtime_id=tuple(data.get("runtime_id") or ()),
        automation_id=data.get("automation_id"),
        class_name=data.get("class_name"),
        control_type=data.get("control_type"),
        path=tuple(data.get("path") or ()),
    )


# ---------------------------------------------------------------------------
# WindowInfo
# ---------------------------------------------------------------------------


def window_to_dict(window: WindowInfo) -> dict[str, Any]:
    return {
        "window_id": window.window_id,
        "title": window.title,
        "process": window.process,
        "pid": window.pid,
        "active": window.active,
        "visible": window.visible,
        "rect": list(window.rect) if window.rect else None,
        "hwnd": window.hwnd,
    }


def window_from_dict(data: dict[str, Any]) -> WindowInfo:
    rect = data.get("rect")
    return WindowInfo(
        window_id=data["window_id"],
        title=data["title"],
        process=data["process"],
        pid=data["pid"],
        active=data.get("active", False),
        visible=data.get("visible", True),
        rect=tuple(rect) if rect else None,
        hwnd=int(data.get("hwnd") or 0),
    )


# ---------------------------------------------------------------------------
# RawNode / RawTree
# ---------------------------------------------------------------------------


def raw_node_to_dict(node: RawNode) -> dict[str, Any]:
    return {
        "native": native_ref_to_dict(node.native),
        "role": node.role,
        "name": node.name,
        "value": node.value,
        "description": node.description,
        "states": sorted(node.states),
        "patterns": sorted(node.patterns),
        "truncated": node.truncated,
        "virtual_count": node.virtual_count,
        "children": [raw_node_to_dict(child) for child in node.children],
    }


def raw_node_from_dict(data: dict[str, Any]) -> RawNode:
    return RawNode(
        native=native_ref_from_dict(data["native"]),
        role=data["role"],
        name=data.get("name"),
        value=data.get("value"),
        description=data.get("description"),
        states=frozenset(data.get("states") or ()),
        patterns=frozenset(data.get("patterns") or ()),
        children=[raw_node_from_dict(child) for child in data.get("children") or ()],
        truncated=data.get("truncated", False),
        virtual_count=data.get("virtual_count"),
    )


def raw_tree_to_dict(tree: RawTree) -> dict[str, Any]:
    return {
        "window": window_to_dict(tree.window),
        "root": raw_node_to_dict(tree.root),
        "raw_revision": tree.raw_revision,
    }


def raw_tree_from_dict(data: dict[str, Any]) -> RawTree:
    return RawTree(
        window=window_from_dict(data["window"]),
        root=raw_node_from_dict(data["root"]),
        raw_revision=data.get("raw_revision", 0),
    )


# ---------------------------------------------------------------------------
# ExecResult
# ---------------------------------------------------------------------------


def exec_result_to_dict(result: ExecResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "error": result.error,
        "message": result.message,
        "unsupported": getattr(result, "unsupported", False),
        "undetermined": getattr(result, "undetermined", False),
    }


def exec_result_from_dict(data: dict[str, Any]) -> ExecResult:
    """Rebuild an ExecResult, keeping only the fields the class declares.

    Constructed through ``__init__`` with keyword filtering rather than
    ``ExecResult(**data)``: a newer peer may send a field this build's
    ExecResult does not have, and an unexpected keyword would turn a working
    call into a TypeError on the agent's side.
    """
    import dataclasses

    known = {f.name for f in dataclasses.fields(ExecResult)}
    return ExecResult(**{k: v for k, v in data.items() if k in known})
