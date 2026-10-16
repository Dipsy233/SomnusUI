"""YAML App Pack tests (dev doc section 15, v0.2).

Exercises the loader, the attribute/selector expressions and the two real-app
packs (Notepad, Explorer) against recorded fixtures so they run on any platform.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from somnus.compiler.compile import Compiler
from somnus.compiler.pack_schema import get_pack
from somnus.compiler.yaml_pack import load_pack_dict
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo

yaml = pytest.importorskip("yaml")

PACK_DIR = Path(__file__).resolve().parents[2] / "somnus" / "packs"


# ---------------------------------------------------------------------------
# loader
# ---------------------------------------------------------------------------


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
    """A capability whose only step is type_text runs end to end - but only
    when the host has authorised synthetic input, which is off by default."""
    from somnus.providers.mock import MockProvider
    from somnus.runtime.policy import Policy
    from somnus.runtime.resolver import Resolver
    from somnus.runtime.session import Session
    from somnus.sui.parse import parse_action

    pack = load_pack_dict(_KEYBOARD_PACK)
    tree = _editor_tree(has_value=False)
    session = Session(
        MockProvider(fixtures=[tree]), pack=pack, policy=Policy(allow_keyboard=True)
    )
    session.observe()

    editor = next(n for n in session.result.root.walk() if n.entity == "editor")
    out = session.dispatch(f'CALL {editor.ref}.set_text("hello")')
    assert out.startswith("OK"), out

    # The mock appends for type_text, proving the fallback step ran.
    session.observe()
    editor = next(n for n in session.result.root.walk() if n.entity == "editor")
    assert editor.value and "hello" in editor.value


def test_set_text_echoes_the_value_written():
    """A write reports the exact text it stored, so the agent can confirm it.

    The delta only shows a `value` change when the value differed and a routed
    write may not be readable back through UIA, so without this echo "wrote the
    same text" and "wrote nothing" look identical.
    """
    from somnus.providers.mock import MockProvider
    from somnus.runtime.policy import Policy
    from somnus.runtime.session import Session

    pack = load_pack_dict(_KEYBOARD_PACK)
    session = Session(
        MockProvider(fixtures=[_editor_tree(has_value=True)]),
        pack=pack,
        policy=Policy(allow_keyboard=True),
    )
    session.observe()
    editor = next(n for n in session.result.root.walk() if n.entity == "editor")

    out = session.dispatch(f'CALL {editor.ref}.set_text("hello")')
    assert f'~ {editor.ref} text="hello"' in out, out

    # Writing the same value again leaves the delta empty, but the echo stays:
    # the agent can still tell the write happened, and what it stored.
    again = session.dispatch(f'CALL {editor.ref}.set_text("hello")')
    assert f'~ {editor.ref} text="hello"' in again, again


def test_set_text_echo_shows_a_literal_colon_argument():
    """`text:\"x\"` is data, not a named argument, and the echo says so.

    A JSON-style colon is the mistake this makes visible: the write stored the
    whole literal, and the quoted echo shows it instead of hiding it.
    """
    from somnus.providers.mock import MockProvider
    from somnus.runtime.policy import Policy
    from somnus.runtime.session import Session

    pack = load_pack_dict(_KEYBOARD_PACK)
    session = Session(
        MockProvider(fixtures=[_editor_tree(has_value=True)]),
        pack=pack,
        policy=Policy(allow_keyboard=True),
    )
    session.observe()
    editor = next(n for n in session.result.root.walk() if n.entity == "editor")

    out = session.dispatch(f'CALL {editor.ref}.set_text(text:"foo")')
    assert r'~ ' + editor.ref + r' text="text:\"foo\""' in out, out


def test_keyboard_fallback_is_refused_when_the_policy_disables_it():
    """Without authorisation, a capability that can only type is refused.

    Typing is the one route that reaches a control no native pattern reaches,
    and it costs focus stealing, a race with the target's message pump, and an
    unverifiable result. The channel is on by default in this build, so the test
    disables it explicitly to exercise the refusal path."""
    from somnus.providers.mock import MockProvider
    from somnus.runtime.policy import Policy
    from somnus.runtime.session import Session

    pack = load_pack_dict(_KEYBOARD_PACK)
    tree = _editor_tree(has_value=False)
    session = Session(
        MockProvider(fixtures=[tree]),
        pack=pack,
        policy=Policy(allow_keyboard=False, allow_clipboard=False),
    )
    session.observe()

    editor = next(n for n in session.result.root.walk() if n.entity == "editor")
    out = session.dispatch(f'CALL {editor.ref}.set_text("hello")')
    assert out.startswith("ERR"), out
    assert "keyboard" in out.lower(), out


def test_type_text_primitive_is_not_a_default_capability():
    """The LLM must never see type_text as a callable capability."""
    pack = load_pack_dict(_KEYBOARD_PACK)
    result = Compiler().compile(_editor_tree(has_value=False), pack=pack)
    for node in result.root.walk():
        assert "type_text" not in node.cap_names()


# ---------------------------------------------------------------------------
# pack honesty: a capability must correspond to something the app can do
# ---------------------------------------------------------------------------


def test_pack_with_two_capabilities_sharing_one_via_is_rejected():
    """Two names for the same call mean one of them is fabricated.

    This is exactly how `+customize` appeared on the demo's products: it reused
    the Add button's invoke, so `CALL p1.customize(...)` reported OK while
    actually adding a product and dropping the argument.
    """
    from somnus.compiler.yaml_pack import validate_pack

    pack = load_pack_dict(
        {
            "app": {"id": "x", "match": {"process": "x.exe"}},
            "entities": {
                "product": {
                    "match": {"role": "group"},
                    "caps": {
                        "add": {"via": ["$add_btn.activate"]},
                        "customize": {"via": ["$add_btn.activate"]},
                    },
                }
            },
        }
    )
    problems = validate_pack(pack)
    assert any("share one via sequence" in p for p in problems)


def test_pack_with_a_dropped_parameter_is_rejected():
    """A parameter no step forwards would be silently discarded."""
    from somnus.compiler.yaml_pack import validate_pack

    pack = load_pack_dict(
        {
            "app": {"id": "x", "match": {"process": "x.exe"}},
            "entities": {
                "product": {
                    "match": {"role": "group"},
                    "caps": {
                        "add": {
                            "via": ["$add_btn.activate"],
                            "params": {"size": {"type": "enum", "enum": ["small"]}},
                        }
                    },
                }
            },
        }
    )
    problems = validate_pack(pack)
    assert any("never forwarded" in p for p in problems)


def test_honest_packs_validate_clean():
    """Distinct capabilities, each fully forwarded, validate clean."""
    from somnus.compiler.yaml_pack import validate_pack

    pack = load_pack_dict(
        {
            "app": {"id": "ok", "match": {"process": "ok.exe"}},
            "entities": {
                "item": {
                    "match": {"role": "group"},
                    "caps": {
                        "add": {"via": ["$add_btn.activate"]},
                        "remove": {"via": ["$remove_btn.activate"]},
                    },
                }
            },
        }
    )
    assert validate_pack(pack) == []


def test_set_text_parameters_are_not_flagged():
    """set_text consumes its argument positionally, so that is not a problem."""
    from somnus.compiler.yaml_pack import validate_pack

    pack = load_pack_dict(
        {
            "app": {"id": "x", "match": {"process": "x.exe"}},
            "entities": {
                "field": {
                    "match": {"role": "input"},
                    "caps": {
                        "set_text": {
                            "via": ["$self.set_text"],
                            "params": {"text": {"type": "string", "required": True}},
                        }
                    },
                }
            },
        }
    )
    assert validate_pack(pack) == []


def test_demo_products_advertise_only_what_the_ui_has():
    """The acceptance app has one button per product; `add` is the whole surface."""
    pack = get_pack("restaurant-demo")
    if pack is None:  # pragma: no cover - the demo app is optional
        pytest.skip("restaurant-demo pack not registered")
    product = pack.find_entity_def("product")
    assert set(product.caps) == {"add"}
