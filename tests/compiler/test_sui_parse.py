"""SUI-Text serialization + parsing tests."""

from __future__ import annotations

import pytest

from somnus.errors import SomnusError
from somnus.sui.parse import parse_action, parse_find, parse_inspect


def test_parse_call_no_args():
    action = parse_action("CALL p1.add()")
    assert action.kind == "call"
    assert action.ref == "p1"
    assert action.capability == "add"
    assert action.args == {}


def test_parse_call_with_named_args():
    action = parse_action('CALL p1.customize(size="large", qty=2)')
    assert action.args == {"size": "large", "qty": 2}


def test_parse_call_positional_arg():
    action = parse_action('CALL q1.set_text("Big Mac")')
    assert action.args == {"_0": "Big Mac"}


def test_parse_find():
    action = parse_find('FIND product "Big Mac"')
    assert action.kind == "find"
    assert action.query_type == "product"
    assert action.query == "Big Mac"


def test_parse_find_with_scope():
    action = parse_action('FIND product "Big Mac" in l1')
    assert action.scope == "l1"


def test_parse_inspect():
    action = parse_inspect("INSPECT l1 depth=1")
    assert action.ref == "l1"
    assert action.depth == 1


def test_parse_inspect_range():
    action = parse_action("INSPECT t1 range=0:200")
    assert action.range == (0, 200)


def test_forbidden_primitive_verb():
    with pytest.raises(SomnusError) as exc:
        parse_action("click(e123)")
    assert "PARSE_ERROR" in str(exc.value)


def test_forbidden_uia_verb():
    with pytest.raises(SomnusError):
        parse_action("CALL p1.click()")


def test_garbage_rejected():
    with pytest.raises(SomnusError):
        parse_action("do the thing")


def test_parse_unknown():
    action = parse_action('UNKNOWN "cart is a canvas"')
    assert action.kind == "unknown"


def test_parse_observe():
    action = parse_action("OBSERVE w2")
    assert action.kind == "observe"
    assert action.window == "w2"


def test_escaped_quotes_in_args():
    action = parse_action('CALL q1.set_text("say \\"hi\\"")')
    assert action.args["_0"] == 'say "hi"'
