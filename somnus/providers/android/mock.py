"""Mock Android provider: replays recorded Portal snapshots.

Same role as :class:`somnus.providers.mock.MockProvider` for Windows: a
first-class component, not a test stub. It

* replays a recorded Portal ``snapshot`` JSON (one file per screen),
* executes a small scripted action model so calls produce real deltas,
* runs the *real* ``rawtree`` conversion and the real compiler pipeline, so a
  fixture that passes here is evidence about the phone path too.

Fixture format (a recorded Portal reply, possibly several screens)::

    [
      {"display": 0, "rev": 1, "windows": [ {"id": 12, "pkg": "...", ...} ]},
      ...
    ]

``actions`` on a fixture maps ``<node key>:<primitive>`` to mutations, the
same convention the Windows mock uses with names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from somnus.ir.action import ExecResult, UiEvent
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.android.rawtree import portal_json_to_tree, portal_json_to_windows
from somnus.providers.base import NullSubscription


def load_portal_fixture(path: str | Path) -> list[dict[str, Any]]:
    """One fixture file -> list of Portal snapshot dicts (one per screen)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        return [data]
    return list(data)


class AndroidMockProvider:
    """Replays recorded Portal snapshots with scripted mutations."""

    name = "android"

    #: A replay cannot change on its own.
    is_deterministic = True

    #: Mirrors the real provider: a replay stands in for the phone's screen,
    #: so the session must follow whichever window the fixture marks active.
    follows_active_window = True

    def __init__(
        self,
        snapshots: list[dict[str, Any]] | None = None,
        *,
        snapshot_paths: list[str | Path] | None = None,
        scripts: dict[str, list[dict[str, Any]]] | None = None,
        callbacks: dict[str, Callable[[RawNode, RawNode], None]] | None = None,
        **_: Any,
    ) -> None:
        self.trees: dict[str, RawTree] = {}
        self._rev: dict[str, int] = {}
        self._listeners: dict[str, list[Callable[[UiEvent], None]]] = {}
        self.scripts = scripts or {}
        self.callbacks = callbacks or {}
        self.executed: list[tuple[NativeRef, str, Any]] = []
        self.launchable: list[dict[str, str]] = []
        self.globals: list[str] = []
        self.pages: list[str] = []
        for snap in snapshots or ():
            self._install(snap)
        for path in snapshot_paths or ():
            for snap in load_portal_fixture(path):
                self._install(snap)

    def _install(self, snap: dict[str, Any]) -> None:
        for tree in portal_json_to_tree(snap):
            self.trees[tree.window.window_id] = tree
            self._rev[tree.window.window_id] = int(snap.get("rev") or 1)

    # -- provider API --------------------------------------------------------

    def list_windows(self) -> list[WindowInfo]:
        return [t.window for t in self.trees.values()]

    def peek(self, window_id: str) -> WindowInfo | None:
        tree = self.trees.get(window_id)
        return tree.window if tree else None

    def snapshot(
        self,
        window_id: str,
        scope: NativeRef | None = None,
        max_depth: int = 20,
    ) -> RawTree:
        tree = self.trees.get(window_id)
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
        tree = self.trees.get(ref.window_id)
        if tree is None:
            return ExecResult(ok=False, error="TARGET_NOT_FOUND")
        node = tree.root.find_native(ref)
        if node is None:
            return ExecResult(ok=False, error="STALE_TARGET")

        # Scripted mutation, keyed `<node key>:<primitive>` or
        # `<node name>:<primitive>` (the Windows mock's convention, kept so
        # scenario fixtures and their scripts port unchanged).
        mutated = False
        keys = [f"{_key(ref)}:{primitive}"]
        if node.name:
            keys.append(f"{node.name}:{primitive}")
        for key in keys:
            for mut in self.scripts.get(key, ()):
                self._apply(node, tree, mut, value)
                mutated = True

        # Default behaviour for unscripted primitives, so a fixture needs no
        # script for the trivial cases.
        if not mutated:
            if primitive in ("set_text", "paste_text", "type_text") and value is not None:
                node.value = str(value)
                mutated = True
            elif primitive == "toggle" and node.role in ("switch", "checkbox"):
                if "checked" in node.states:
                    node.states = node.states - {"checked"}
                    node.states = frozenset(node.states | {"unchecked"})
                else:
                    node.states = frozenset(node.states - {"unchecked"} | {"checked"})
                mutated = True
            elif primitive == "scroll_into_view":
                node.states = frozenset(node.states - {"offscreen"})
                mutated = True
            elif primitive == "scroll" and "scroll" in node.patterns:
                # A virtualised list moves its window: the fixture's rows are
                # rotated (the Portal would realise different views) so a
                # scroll-find genuinely has to walk the list.
                direction = value if isinstance(value, str) else "down"
                if node.children:
                    if direction == "down":
                        node.children.append(node.children.pop(0))
                    elif direction == "up":
                        node.children.insert(0, node.children.pop())
                mutated = True
        if mutated:
            self._bump(ref.window_id)
        return ExecResult(ok=True)
    def subscribe(
        self, window_id: str, on_event: Callable[[UiEvent], None]
    ) -> NullSubscription:
        self._listeners.setdefault(window_id, []).append(on_event)
        return NullSubscription()

    def close(self) -> None:
        self.trees.clear()

    # -- Android-specific surface --------------------------------------------

    def global_action(self, name: str) -> ExecResult:
        self.globals.append(name)
        return ExecResult(ok=True)

    def page(self, direction: str) -> ExecResult:
        if direction not in ("next", "prev"):
            return ExecResult(
                ok=False, error="ACTION_NOT_SUPPORTED", message=direction
            )
        self.pages.append(direction)
        return ExecResult(ok=True)

    def launch_app(self, package: str) -> ExecResult:
        if any(a.get("package") == package for a in self.launchable):
            return ExecResult(ok=True)
        return ExecResult(ok=False, error="APP_NOT_FOUND", message=package)

    def list_apps(self) -> list[dict[str, str]]:
        return list(self.launchable)

    def set_clipboard(self, text: str) -> bool:
        return True

    # -- test helpers ---------------------------------------------------------

    def set_snapshot(self, snap: dict[str, Any]) -> None:
        """Replace the whole screen state (simulates a UI change)."""
        self._install(snap)
        for tree in portal_json_to_tree(snap):
            self._bump(tree.window.window_id)

    def _bump(self, window_id: str) -> None:
        self._rev[window_id] = self._rev.get(window_id, 0) + 1
        self.trees[window_id].raw_revision = self._rev[window_id]
        for cb in self._listeners.get(window_id, ()):
            cb(UiEvent(window_id=window_id, kind="structure"))

    def _apply(
        self, node: RawNode, tree: RawTree, mut: dict[str, Any], value: Any
    ) -> None:
        op = mut.get("op")
        root = tree.root
        if op == "set_value":
            node.value = mut.get("to", value)
        elif op == "add_state":
            node.states = frozenset(node.states | {mut["state"]})
        elif op == "remove_state":
            node.states = frozenset(node.states - {mut["state"]})
        elif op == "add_child":
            node.children.append(
                RawNode(native=NativeRef(window_id=tree.window.window_id, runtime_id=(hash(node) % 100000,)))
            )
        elif op == "callback":
            fn = self.callbacks.get(mut.get("fn", ""))
            if fn is not None:
                fn(node, root)


def _key(ref: NativeRef) -> str:
    return ".".join(str(p) for p in ref.runtime_id)
