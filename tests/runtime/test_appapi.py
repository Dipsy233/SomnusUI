"""App API channel tests (dev doc section 20).

A capability can reach the application's own interface instead of its controls.
Nothing about the LLM surface changes: the same `CALL ref.cap()` resolves to an
app-API step, and the structured result is reported back.
"""

from __future__ import annotations

from somnus.compiler.yaml_pack import load_pack_dict
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session


def _tree(process: str = "explorer.exe") -> RawTree:
    root = RawNode(
        native=NativeRef("w1", (0,), class_name="W"),
        role="window",
        name="w",
        children=[
            RawNode(
                native=NativeRef("w1", (1,), class_name="L"),
                role="list",
                name="Files",
            )
        ],
    )
    return RawTree(window=WindowInfo("w1", "w", process, 1), root=root)


_PACK = {
    "app": {"id": "fs", "match": {"process": "explorer.exe"}},
    "entities": {
        "folder": {
            "match": {"role": "list", "has_name": True},
            "key": "$self.name",
            "attrs": {"name": "$self.name"},
            "caps": {
                "list_files": {
                    "via": [
                        {"primitive": "app_api", "target": "list", "value": {"path": "$path"}}
                    ],
                    "params": {"path": {"type": "string", "required": True}},
                },
                "stat": {
                    "via": [
                        {
                            "primitive": "app_api",
                            "target": "stat",
                            "value": {"path": "$self.name"},
                        }
                    ],
                },
            },
        }
    },
}


def _session(process: str = "explorer.exe") -> Session:
    pack = load_pack_dict(dict(_PACK, app=dict(_PACK["app"], match={"process": process})))
    session = Session(MockProvider(fixtures=[_tree(process)]), pack=pack)
    session.observe()
    return session


def test_app_api_step_returns_structured_data(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("y")
    session = _session()
    ref = next(n.ref for n in session.result.root.walk() if n.entity == "folder")
    out = session.dispatch(f'CALL {ref}.list_files(path="{tmp_path}")')
    assert out.startswith("OK"), out
    assert "a.txt" in out and "b.txt" in out
    assert out.lstrip().splitlines()[1].startswith("=")


def test_app_api_without_a_handler_reports_not_supported():
    session = _session(process="unknownapp.exe")
    ref = next(n.ref for n in session.result.root.walk() if n.entity == "folder")
    out = session.dispatch(f'CALL {ref}.list_files(path=".")')
    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out


def test_app_api_refuses_an_unlisted_action():
    """A handler only runs the actions it declares."""
    from somnus.appapi import ShellApiHandler

    handler = ShellApiHandler()
    result = handler.invoke("delete_everything", {"path": "."})
    assert not result.ok and result.error == "ACTION_NOT_SUPPORTED"


def test_app_api_still_requires_a_valid_llm_argument(tmp_path):
    session = _session()
    ref = next(n.ref for n in session.result.root.walk() if n.entity == "folder")
    # The declared parameter is required, so omitting it fails before dispatch.
    out = session.dispatch(f"CALL {ref}.list_files()")
    assert out.startswith("ERR INVALID_ARGUMENT"), out
