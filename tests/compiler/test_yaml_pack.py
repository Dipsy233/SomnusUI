"""YAML App Pack tests (dev doc section 15, v0.2).

Exercises the loader, the attribute/selector expressions and the two real-app
packs (Notepad, Explorer) against recorded fixtures so they run on any platform.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from somnus.compiler.compile import Compiler
from somnus.compiler.pack_schema import get_pack, select_pack
from somnus.compiler.yaml_pack import load_pack_dict
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

yaml = pytest.importorskip("yaml")

PACK_DIR = Path(__file__).resolve().parents[2] / "somnus" / "packs"


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


def test_builtin_yaml_packs_registered():
    assert get_pack("notepad") is not None
    assert get_pack("explorer") is not None
    assert select_pack("notepad.exe").id == "notepad"
    assert select_pack("explorer.exe").id == "explorer"


def test_load_pack_dict_minimal():
    pack = load_pack_dict(
        {
            "app": {"id": "demo", "match": {"process": "demo.exe"}},
            "entities": {
                "thing": {
                    "match": {"role": "group", "has_name": True},
                    "key": "$self.name",
                    "attrs": {"name": "$self.name"},
                    "caps": {
                        "poke": {
                            "via": ["$self.activate"],
                            "effects": ["thing.count += 1"],
                            "risk": "normal",
                        }
                    },
                }
            },
        }
    )
    assert pack.id == "demo"
    assert pack.match_process == ("demo.exe",)
    thing = pack.entities[0]
    assert thing.name == "thing"
    assert thing.caps["poke"].via == ["$self.activate"]
    assert thing.caps["poke"].effects == ["thing.count += 1"]


def test_missing_app_id_fails_loud():
    from somnus.compiler.yaml_pack import PackLoadError

    with pytest.raises(PackLoadError):
        load_pack_dict({"entities": {}})


def test_yaml_files_parse():
    for path in sorted(PACK_DIR.glob("*.pack.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["app"]["id"], path
        assert data["app"]["match"]["process"], path


# ---------------------------------------------------------------------------
# descriptor / attr expressions
# ---------------------------------------------------------------------------


def _node(role, name=None, value=None, automation_id=None, patterns=(), children=()):
    return RawNode(
        native=NativeRef("w1", (hash((role, name)) & 0xFFFF,), automation_id=automation_id),
        role=role,
        name=name,
        value=value,
        patterns=frozenset(patterns),
        children=list(children),
    )


def test_descendant_attr_expression():
    # A product group whose price lives in a descendant text node.
    price_text = _node("text", name="$5.99")
    group = _node("group", children=[_node("text", name="Big Mac"), price_text])
    pack = load_pack_dict(
        {
            "app": {"id": "t", "match": {"process": "t.exe"}},
            "entities": {
                "item": {
                    "match": {"role": "group"},
                    "attrs": {
                        "price": {
                            "from": "descendant(role=text, name=/\\d+\\.\\d{2}/)",
                            "regex": "(\\d+\\.\\d{2})",
                            "cast": "number",
                        }
                    },
                }
            },
        }
    )
    root = _node("window", children=[group])
    tree = RawTree(window=WindowInfo("w1", "t", "t.exe", 1), root=root)
    result = Compiler().compile(tree, pack=pack)
    items = [n for n in result.root.walk() if n.entity == "item"]
    assert items and items[0].attrs["price"] == 5.99


def test_has_patterns_match_rule():
    from somnus.compiler.pack_schema import MatchRule

    rule = MatchRule(role="text", has_patterns=("value", "text"))
    assert rule.matches(_node("text", patterns=("value", "text")))
    assert not rule.matches(_node("text", patterns=("value",)))


# ---------------------------------------------------------------------------
# Notepad pack (recorded fixture)
# ---------------------------------------------------------------------------


def _notepad_tree(text: str = "") -> RawTree:
    """A trimmed copy of the real Windows 11 Notepad tree."""
    editor = _node(
        "document",
        name="文本编辑器",
        value=text,
        automation_id="editor",
        patterns=("value", "text", "scroll"),
    )
    status = _node(
        "text", name="行 1，\n列 1", patterns=("text",)
    )
    root = _node(
        "window",
        name="无标题 - Notepad",
        automation_id="notepad_window",
        children=[editor, status],
    )
    return RawTree(
        window=WindowInfo("w1", "无标题 - Notepad", "notepad.exe", 1, active=True),
        root=root,
    )


def test_notepad_pack_matches_document():
    result = Compiler().compile(_notepad_tree(), pack=get_pack("notepad"))
    docs = [n for n in result.root.walk() if n.entity == "document"]
    assert len(docs) == 1
    assert "set_text" in docs[0].cap_names()


def test_notepad_set_text_positional_argument():
    """``CALL tx1.set_text("…")`` binds the positional arg to the named param."""
    from somnus.providers.mock import MockProvider
    from somnus.runtime.session import Session
    from somnus.sui.parse import parse_action
    from somnus.runtime.resolver import Resolver

    tree = _notepad_tree()
    result = Compiler().compile(tree, pack=get_pack("notepad"))
    resolver = Resolver(result)
    doc_ref = next(n.ref for n in result.root.walk() if n.entity == "document")
    resolution = resolver.resolve(parse_action(f'CALL {doc_ref}.set_text("hi")'))
    assert resolution.steps[0].primitive == "set_text"
    assert resolution.steps[0].value == "hi"


def test_notepad_named_argument_also_works():
    from somnus.runtime.resolver import Resolver
    from somnus.sui.parse import parse_action

    result = Compiler().compile(_notepad_tree(), pack=get_pack("notepad"))
    resolver = Resolver(result)
    doc_ref = next(n.ref for n in result.root.walk() if n.entity == "document")
    resolution = resolver.resolve(
        parse_action(f'CALL {doc_ref}.set_text(text="named")')
    )
    assert resolution.steps[0].value == "named"


# ---------------------------------------------------------------------------
# Explorer pack
# ---------------------------------------------------------------------------


def _explorer_tree() -> RawTree:
    """A trimmed copy of the real Explorer items view."""
    def row(name: str) -> RawNode:
        return _node(
            "listitem",
            name=name,
            patterns=("invoke", "selectionitem", "value"),
            children=[_node("edit", name="名称", patterns=("value",))],
        )

    items = _node(
        "list",
        name="项目视图",
        automation_id="items_view",
        patterns=("selection", "scroll", "value"),
        children=[row(".git"), row("apps"), row("bench")],
    )
    root = _node(
        "window",
        name="E:\\0down\\SomnusUI",
        children=[items],
    )
    return RawTree(
        window=WindowInfo("w1", "E:\\0down\\SomnusUI", "explorer.exe", 1, active=True),
        root=root,
    )


def test_explorer_pack_matches_files_and_folder():
    result = Compiler().compile(_explorer_tree(), pack=get_pack("explorer"))
    files = [n for n in result.root.walk() if n.entity == "file"]
    names = {n.name for n in files}
    assert {".git", "apps", "bench"} <= names
    # A file is selectable and activatable through native patterns.
    assert "select" in files[0].cap_names()
    assert "activate" in files[0].cap_names()

    folders = [n for n in result.root.walk() if n.entity == "folder"]
    assert folders and "scroll" in folders[0].cap_names()


def test_explorer_dir_change_keeps_window_ref():
    """Navigating does not invalidate the window ref (title = path changes)."""
    from somnus.providers.mock import MockProvider
    from somnus.runtime.session import Session

    tree = _explorer_tree()
    session = Session(MockProvider(fixtures=[tree]), pack_id="explorer")
    session.observe()
    window_ref = session.result.root.ref

    # Simulate navigating to another folder: the title changes.
    tree2 = _explorer_tree()
    tree2.window.title = "C:\\Windows"
    tree2.root.name = "C:\\Windows"
    session.provider.set_tree(tree2)
    session._recompile()
    assert session.result.root.ref == window_ref


# ---------------------------------------------------------------------------
# keyboard fallback (dev doc section 12.2)
# ---------------------------------------------------------------------------

_KEYBOARD_PACK = {
    "app": {"id": "kb", "match": {"process": "kb.exe"}},
    "entities": {
        "editor": {
            "match": {"role": "text"},
            "key": "$self.automation_id",
            "caps": {
                "set_text": {
                    "params": {"text": {"type": "string", "required": True}},
                    "via": [
                        {
                            "target": "$self",
                            "primitive": "set_text",
                            "value": "$text",
                            "when_has": "value",
                        },
                        {
                            "target": "$self",
                            "primitive": "type_text",
                            "value": "$text",
                            "when_missing": "value",
                        },
                    ],
                }
            },
        }
    },
}


def _editor_tree(*, has_value: bool) -> RawTree:
    patterns = ("value", "text") if has_value else ("text",)
    editor = _node("text", name="editor", patterns=patterns)
    root = _node("window", name="w", children=[editor])
    return RawTree(window=WindowInfo("w1", "w", "kb.exe", 1), root=root)


def test_keyboard_fallback_selected_only_without_value_pattern():
    pack = load_pack_dict(_KEYBOARD_PACK)

    native = Compiler().compile(_editor_tree(has_value=True), pack=pack)
    cap = next(n for n in native.root.walk() if n.entity == "editor").get_cap("set_text")
    assert [s.primitive for s in cap.via] == ["set_text"]

    fallback = Compiler().compile(_editor_tree(has_value=False), pack=pack)
    cap = next(
        n for n in fallback.root.walk() if n.entity == "editor"
    ).get_cap("set_text")
    assert [s.primitive for s in cap.via] == ["type_text"]


def test_keyboard_fallback_executes_through_the_resolver():
    """A capability whose only step is type_text still runs end to end."""
    from somnus.providers.mock import MockProvider
    from somnus.runtime.resolver import Resolver
    from somnus.runtime.session import Session
    from somnus.sui.parse import parse_action

    pack = load_pack_dict(_KEYBOARD_PACK)
    tree = _editor_tree(has_value=False)
    session = Session(MockProvider(fixtures=[tree]), pack=pack)
    session.observe()

    editor = next(n for n in session.result.root.walk() if n.entity == "editor")
    out = session.dispatch(f'CALL {editor.ref}.set_text("hello")')
    assert out.startswith("OK"), out

    # The mock appends for type_text, proving the fallback step ran.
    session.observe()
    editor = next(n for n in session.result.root.walk() if n.entity == "editor")
    assert editor.value and "hello" in editor.value


def test_type_text_primitive_is_not_a_default_capability():
    """The LLM must never see type_text as a callable capability."""
    pack = load_pack_dict(_KEYBOARD_PACK)
    result = Compiler().compile(_editor_tree(has_value=False), pack=pack)
    for node in result.root.walk():
        assert "type_text" not in node.cap_names()
