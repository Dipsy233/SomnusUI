"""Deterministic in-memory provider used for tests, CI and demos.

The mock provider is a first-class component, not a test stub:

* :mod:`somnus.providers.mock` can *record* a real provider's snapshots to JSON
  and *replay* them on any platform (dev doc section 14).
* It can execute a small scripted action model so actions produce real deltas
  during compiler / runtime tests without Windows.

Fixture format (JSON)::

    {
      "window": {"window_id": "w1", "title": "...", "process": "...", "pid": 1},
      "root": {<raw node>},
      "actions": [
        {"match": {"role": "button", "name": "Add"},
         "effect": [{"op": "set_attr", "ref_path": "0/1", "attr": "value", ...}]}
      ]
    }
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Iterable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.base import NullSubscription

# ---------------------------------------------------------------------------
# Serialization of raw trees
# ---------------------------------------------------------------------------


def raw_node_to_dict(node: RawNode, include_native: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = {
        "role": node.role,
        "name": node.name,
        "value": node.value,
        "description": node.description,
        "states": sorted(node.states),
        "patterns": sorted(node.patterns),
    }
    if node.truncated:
        data["truncated"] = True
    if node.virtual_count is not None:
        data["virtual_count"] = node.virtual_count
    if include_native:
        data["native"] = {
            "window_id": node.native.window_id,
            "runtime_id": list(node.native.runtime_id),
            "automation_id": node.native.automation_id,
            "class_name": node.native.class_name,
            "control_type": node.native.control_type,
            "path": list(node.native.path),
        }
    data["children"] = [raw_node_to_dict(c, include_native) for c in node.children]
    return data


def raw_node_from_dict(data: dict[str, Any], window_id: str) -> RawNode:
    native = data.get("native") or {}
    ref = NativeRef(
        window_id=native.get("window_id", window_id),
        runtime_id=tuple(native.get("runtime_id", ())),
        automation_id=native.get("automation_id"),
        class_name=native.get("class_name"),
        control_type=native.get("control_type"),
        path=tuple(native.get("path", ())),
    )
    return RawNode(
        native=ref,
        role=data.get("role", "unknown"),
        name=data.get("name"),
        value=data.get("value"),
        description=data.get("description"),
        states=frozenset(data.get("states", ())),
        patterns=frozenset(data.get("patterns", ())),
        truncated=bool(data.get("truncated")),
        virtual_count=data.get("virtual_count"),
        children=[raw_node_from_dict(c, window_id) for c in data.get("children", ())],
    )


def raw_tree_to_dict(tree: RawTree, include_native: bool = True) -> dict[str, Any]:
    return {
        "window": {
            "window_id": tree.window.window_id,
            "title": tree.window.title,
            "process": tree.window.process,
            "pid": tree.window.pid,
            "active": tree.window.active,
            "visible": tree.window.visible,
            "rect": list(tree.window.rect) if tree.window.rect else None,
        },
        "root": raw_node_to_dict(tree.root, include_native),
        "raw_revision": tree.raw_revision,
    }


def raw_tree_from_dict(data: dict[str, Any]) -> RawTree:
    win = data["window"]
    rect = win.get("rect")
    window = WindowInfo(
        window_id=win["window_id"],
        title=win.get("title", ""),
        process=win.get("process", ""),
        pid=win.get("pid", 0),
        active=bool(win.get("active")),
        visible=bool(win.get("visible", True)),
        rect=tuple(rect) if rect else None,
    )
    return RawTree(
        window=window,
        root=raw_node_from_dict(data["root"], window.window_id),
        raw_revision=data.get("raw_revision", 0),
    )


def save_tree(tree: RawTree, path: str | Path, include_native: bool = True) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw_tree_to_dict(tree, include_native), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def load_tree(path: str | Path) -> RawTree:
    return raw_tree_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Scripted mutation model
# ---------------------------------------------------------------------------

#: A mutation is applied to the raw tree after an action matches.
MUTATIONS: dict[str, list[dict[str, Any]]] = {
    # increment the numeric value of a node selected by role/name
    "increment": [
        {"op": "bump", "select": "self", "field": "value", "by": 1},
    ],
}


class MockProvider:
    """Replays fixtures and applies scripted, deterministic mutations."""

    name = "mock"

    def __init__(
        self,
        fixtures: Iterable[RawTree] | None = None,
        scripts: dict[str, list[dict[str, Any]]] | None = None,
        *,
        fixture_paths: Iterable[str | Path] | None = None,
        **_: Any,
    ) -> None:
        self._trees: dict[str, RawTree] = {}
        if fixtures:
            for tree in fixtures:
                self._trees[tree.window.window_id] = tree
        for p in fixture_paths or ():
            tree = load_tree(p)
            self._trees[tree.window.window_id] = tree
        #: capability name -> list of mutation dicts
        self.scripts = scripts or {}
        self._listeners: dict[str, list[Callable[[UiEvent], None]]] = {}
        self._rev = {wid: t.raw_revision for wid, t in self._trees.items()}
        self.executed: list[tuple[NativeRef, str, Any]] = []

    # -- provider API -------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        return [t.window for t in self._trees.values()]

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 12,
    ) -> RawTree:
        tree = self._trees.get(window_id)
        if tree is None:
            raise KeyError(window_id)
        if scope is None:
            return tree
        node = tree.root.find_native(scope)
        if node is None:
            raise KeyError(scope)
        return RawTree(window=tree.window, root=node, raw_revision=tree.raw_revision)

    def execute(
        self,
        ref: NativeRef,
        primitive: str,
        value: Any = None,
        timeout_s: float = 3.0,
    ) -> ExecResult:
        self.executed.append((ref, primitive, value))
        tree = self._trees.get(ref.window_id)
        if tree is None:
            return ExecResult(ok=False, error="TARGET_NOT_FOUND")
        node = tree.root.find_native(ref)
        if node is None:
            return ExecResult(ok=False, error="STALE_TARGET")

        # Apply scripted mutation keyed by primitive (and by capability alias).
        mutated = False
        for key in (primitive, f"{node.name}:{primitive}"):
            for mut in self.scripts.get(key, ()):
                self._apply(node, mut, value)
                mutated = True
        if primitive in ("set_text", "type_text") and value is not None and not mutated:
            if primitive == "type_text":
                # The keyboard fallback appends to whatever is already there,
                # unlike set_text which replaces the value.
                node.value = (node.value or "") + str(value)
            else:
                node.value = str(value)
            mutated = True
        if primitive in ("toggle",) and not mutated:
            if "checked" in node.states:
                node.states = frozenset(s for s in node.states if s != "checked")
            else:
                node.states = node.states | {"checked"}
            mutated = True

        if mutated:
            self._bump(ref.window_id)
        return ExecResult(ok=True)

    def subscribe(
        self, window_id: str, on_event: Callable[[UiEvent], None]
    ) -> NullSubscription:
        self._listeners.setdefault(window_id, []).append(on_event)
        return NullSubscription()

    def close(self) -> None:  # pragma: no cover - trivial
        self._trees.clear()

    # -- test helpers -------------------------------------------------------

    def set_tree(self, tree: RawTree) -> None:
        """Install/replace a tree and notify listeners (simulates a UI change)."""
        self._trees[tree.window.window_id] = tree
        self._rev[tree.window.window_id] = self._rev.get(tree.window.window_id, 0) + 1
        self._emit(tree.window.window_id, "structure")

    def mutate(self, window_id: str, helper: Callable[[RawTree], None]) -> None:
        tree = self._trees[window_id]
        helper(tree)
        self._bump(window_id)

    def _bump(self, window_id: str) -> None:
        self._rev[window_id] = self._rev.get(window_id, 0) + 1
        self._trees[window_id].raw_revision = self._rev[window_id]
        self._emit(window_id, "structure")

    def _emit(self, window_id: str, kind: str) -> None:
        for cb in self._listeners.get(window_id, ()):
            cb(UiEvent(window_id=window_id, kind=kind))

    def _apply(self, node: RawNode, mut: dict[str, Any], value: Any) -> None:
        op = mut.get("op")
        if op == "set_value":
            node.value = mut.get("to", value)
        elif op == "set_name":
            node.name = mut.get("to")
        elif op == "bump":
            current = _as_number(node.value)
            node.value = str((current or 0) + mut.get("by", 1))
        elif op == "add_state":
            node.states = node.states | {mut["state"]}
        elif op == "remove_state":
            node.states = frozenset(s for s in node.states if s != mut["state"])
        elif op == "add_child":
            child = raw_node_from_dict(mut["child"], node.native.window_id)
            node.children.append(child)
        elif op == "remove_child":
            idx = mut["index"]
            if 0 <= idx < len(node.children):
                node.children.pop(idx)
        elif op == "set_attr":
            setattr(node, mut["attr"], mut.get("to", value))
        elif op == "cart_add":
            # Demo-level mutation: find the cart text node and bump its count and
            # total, matching the restaurant pack's effects.
            price = mut.get("price")
            if price is None:
                price = _sibling_price(
                    self._trees[node.native.window_id].root, node
                )
            _cart_add(self._trees[node.native.window_id].root, float(price or 0.0))
        elif op == "open_screen":
            _open_screen(
                self._trees[node.native.window_id].root,
                mut.get("name", "cart"),
                mut.get("count_text", ""),
            )


def _as_number(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _cart_add(root: RawNode, price: float) -> None:
    """Simulate adding an item: bump the cart's count/total text node."""
    for node in root.walk():
        if node.native.automation_id == "cart_count" and node.name:
            m = _COUNT_RE.search(node.name)
            t = _TOTAL_RE.search(node.name)
            count = int(m.group(1)) + 1 if m else 1
            total = (float(t.group(1)) if t else 0.0) + price
            node.name = f"{count} items ${total:.2f}"
            return


def _open_screen(root: RawNode, name: str, count_text: str) -> None:
    """Add (or refresh) a named child screen, so an `open` shows up in the
    delta as ``+ SCREEN s? "<name>"`` (dev doc section 19.1).

    Idempotent: opening twice does not add the screen again.
    """
    for node in root.children:
        if node.name == name and node.role in ("group", "pane", "list"):
            return
    idx = len(root.children)
    screen = RawNode(
        native=NativeRef(root.native.window_id, (5000 + idx,), class_name="Demo"),
        role="group",
        name=name,
        children=[
            RawNode(
                native=NativeRef(root.native.window_id, (5100 + idx,), class_name="Demo"),
                role="text",
                name=count_text or "",
                patterns=frozenset({"text"}),
            ),
            RawNode(
                native=NativeRef(root.native.window_id, (5200 + idx,), class_name="Demo"),
                role="button",
                name="Close",
                patterns=frozenset({"invoke"}),
            ),
        ],
    )
    root.children.append(screen)


import re as _re

_COUNT_RE = re.compile(r"(\d+)\s*items?")
_TOTAL_RE = re.compile(r"\$?(\d+\.\d{2})")
count_re = _COUNT_RE
total_re = _TOTAL_RE



def _sibling_price(root: RawNode, add_button: RawNode) -> float:
    """Read the price text sibling of an Add button inside a product group.

    Uses the native path: siblings of ``<group>/2`` are ``<group>/0`` and
    ``<group>/1``. The Add button is the last child of the product group.
    """
    group_path = add_button.native.path[:-1]
    for node in root.walk():
        if node.native.path[: len(group_path)] != group_path:
            continue
        if node.role == "text" and node.name:
            m = total_re.search(node.name)
            if m:
                return float(m.group(1))
    return 0.0
