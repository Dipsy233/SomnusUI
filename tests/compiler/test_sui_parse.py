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


def test_escaped_backslash_in_args():
    """A literal backslash is written ``\\\\``; the parser must collapse it.

    Regression: the parser used to handle only ``\\"`` and ``\\n``, so a
    Windows path could not be expressed at all - ``D:\\tmp\\notes.txt`` came out
    with a real newline where ``\\n`` was, and ``D:\\\\tmp`` doubled every
    separator. This is the case a Save-As dialog hits immediately.
    """
    action = parse_action('CALL q1.set_text("D:\\\\tmp\\\\notes.txt")')
    assert action.args["_0"] == "D:\\tmp\\notes.txt"


def test_escaped_backslash_before_n_stays_a_path():
    """``\\\\n`` is a backslash followed by ``n``, not a newline."""
    action = parse_action('CALL q1.set_text("notes\\\\new")')
    assert action.args["_0"] == "notes\\new"


def test_backslash_before_t_is_not_a_tab():
    """``\\t`` must survive verbatim: ``C:\\temp`` is a folder, not a tab."""
    action = parse_action('CALL q1.set_text("C:\\temp")')
    assert action.args["_0"] == "C:\\temp"


def test_newline_escape_is_a_literal_backslash_n():
    """``\\n`` stays two characters: a backslash and an ``n``.

    Multi-line text arrives through JSON (or a shell) as a *real* line feed,
    which the parser passes through untouched - see
    ``test_real_newline_passes_through``. Decoding ``\\n`` here would be a
    second escape pass over already-literal data, and it is what used to turn
    ``D:\\notes.txt`` into ``D:`` + line feed + ``otes.txt``.
    """
    action = parse_action('CALL q1.set_text("line one\\nline two")')
    assert action.args["_0"] == "line one\\nline two"


def test_real_newline_passes_through():
    """A genuine line feed inside the quotes is kept, not rejected."""
    action = parse_action('CALL q1.set_text("line one\nline two")')
    assert action.args["_0"] == "line one\nline two"


def test_path_with_backslash_n_survives():
    """The single most common argument: a Windows path containing ``\\n``.

    Regression: ``_unescape`` decoded ``\\n`` to a line feed, so every path
    with a segment starting with ``n`` (``D:\\notes.txt``, ``C:\\new\\a.txt``,
    ``D:\\runtime``) was silently corrupted. ``\\t`` and ``\\r`` were already
    exempt; ``\\n`` now is too.
    """
    for path in (
        "D:\\tmp\\notes.txt",
        "D:\\notes.txt",
        "C:\\Users\\new\\a.txt",
        "C:\\node\\x",
        "D:\\runtime\\bin",
    ):
        action = parse_action(f'CALL q1.set_text("{path}")')
        assert action.args["_0"] == path


def test_round_trip_through_serializer():
    """Parse(quote(s)) == s, for every string except ones with real newlines.

    Serializer output is for *reading*: a real newline is rendered as ``\\n``
    so a node's name stays on one line. That rendering is deliberately not
    re-parseable as an argument any more (``\\n`` is literal), so newline
    strings are excluded here by design rather than by oversight.
    """
    from somnus.sui.serialize import _quote

    for s in (
        "D:\\tmp\\notes.txt",
        "C:\\Users\\new",
        "C:\\node\\x",
        'he said "hi"',
        "back\\slash",
    ):
        assert parse_action(f"CALL q1.set_text({_quote(s, 1000)})").args["_0"] == s
