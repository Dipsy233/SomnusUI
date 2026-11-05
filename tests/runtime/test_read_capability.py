"""`read` returns the text, not a bare OK.

Before this change the executor answered `read` with ``ExecResult(ok=True)``,
so `CALL q1.read()` produced only `OK rev=N`. Now it reads the platform's
readable surface (Text pattern first because it takes a length cap, then the
Value property, then a text label's accessible Name), caps what crosses COM,
and returns `value="…" len=N [truncated=true]` which the session echoes.

Pure fakes: no COM, no desktop.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.action import Action, ExecResult
from somnus.providers.mock import MockProvider
from somnus.providers.windows.executor import (
    _READ_TEXT_CAP,
    _read_payload,
    run_primitive,
)
from somnus.runtime.session import Session
from tests.fixtures.builders import notepad_tree, restaurant_demo_tree


class _ValueOnly:
    """An element whose only readable surface is the Value property."""

    def __init__(self, value: str) -> None:
        self._value = value

    def GetCurrentPattern(self, _pid):
        raise KeyError(_pid)

    def GetCurrentPropertyValue(self, prop):
        if prop != 30045:
            raise KeyError(prop)
        return self._value

    @property
    def CurrentName(self):
        raise AttributeError("no name")


class _TextOnly:
    """An element with a Text pattern (and a length-aware GetText)."""

    def __init__(self, text: str) -> None:
        self._text = text

    def GetCurrentPattern(self, pid):
        if pid != 10014:
            raise KeyError(pid)
        return _FakeTextPattern(self._text)

    def GetCurrentPropertyValue(self, _prop):
        raise KeyError("no value")

    @property
    def CurrentName(self):
        raise AttributeError("no name")


class _FakeTextPattern:
    def __init__(self, text: str) -> None:
        self.DocumentRange = _FakeRange(text)


class _FakeRange:
    def __init__(self, text: str) -> None:
        self._text = text

    def GetText(self, cap: int) -> str:
        return self._text[:cap]


class _Unreadable:
    def GetCurrentPattern(self, _pid):
        raise KeyError(_pid)

    def GetCurrentPropertyValue(self, _prop):
        raise KeyError(_prop)

    @property
    def CurrentName(self):
        raise AttributeError("no name")


class _NameOnly:
    def __init__(self, name: str) -> None:
        self._name = name

    def GetCurrentPattern(self, _pid):
        raise KeyError(_pid)

    def GetCurrentPropertyValue(self, _prop):
        raise KeyError(_prop)

    @property
    def CurrentName(self):
        return self._name


def test_a_short_value_is_returned_with_its_length():
    assert _read_payload(_ValueOnly("hello")) == 'value="hello" len=5'


def test_an_empty_value_is_a_readable_answer_not_a_failure():
    assert _read_payload(_ValueOnly("")) == 'value="" len=0'


def test_newlines_tabs_quotes_and_backslashes_are_escaped():
    payload = _read_payload(_ValueOnly('a\nb\tc"d\\e'))
    assert "\\n" in payload and "\\t" in payload
    assert '\\"d' in payload and "\\\\e" in payload
    assert "\n" not in payload and "\t" not in payload


def test_a_long_value_is_previewed_and_marked_truncated():
    payload = _read_payload(_ValueOnly("x" * 300))
    assert 'value="' in payload and "len=300" in payload
    assert "truncated=true" in payload and "INSPECT" in payload
    # 200-character preview, ending in an ellipsis.
    assert "x" * 199 + "…" in payload


def test_the_text_pattern_is_read_with_a_hard_cap():
    text = "a" * (_READ_TEXT_CAP + 1)
    payload = _read_payload(_TextOnly(text))
    assert f"len>={_READ_TEXT_CAP + 1}" in payload
    assert "truncated=true" in payload


def test_an_unreadable_element_returns_none():
    assert _read_payload(_Unreadable()) is None


def test_a_text_label_falls_back_to_its_accessibility_name():
    assert _read_payload(_NameOnly("标签")) == 'value="标签" len=2'


def test_run_primitive_read_carries_the_payload():
    result = run_primitive(_ValueOnly("hello"), "read", None, SimpleNamespace())
    assert result.ok
    assert result.message == 'value="hello" len=5'


def test_run_primitive_read_reports_unsupported_when_unreadable():
    result = run_primitive(_Unreadable(), "read", None, SimpleNamespace())
    assert not result.ok and result.unsupported
    assert result.error == "ACTION_NOT_SUPPORTED"


# ---------------------------------------------------------------------------
# session wiring on the replay provider
# ---------------------------------------------------------------------------


class _ReadProvider(MockProvider):
    """The mock has no COM surface; this double supplies the executor's answer."""

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        if primitive == "read":
            return ExecResult(ok=True, message='value="hello" len=5')
        return super().execute(ref, primitive, value, timeout_s)


def _input_ref(session) -> str:
    return next(
        n.ref for n in session.result.root.walk()
        if n.role == "input" and "read" in n.cap_names()
    )


def test_observe_shows_the_value_and_read_returns_it():
    session = Session(_ReadProvider(fixtures=[notepad_tree(text="hello")]))
    observed = session.observe().text
    line = next(l for l in observed.splitlines() if "Text Editor" in l)
    assert 'value="hello"' in line, line

    out = session.dispatch(f"CALL {_input_ref(session)}.read()")
    assert out.startswith("OK"), out
    assert '~' in out and 'value="hello" len=5' in out, out


def test_read_is_not_offered_on_a_button():
    session = Session(MockProvider(fixtures=[restaurant_demo_tree()]))
    session.observe()
    button = next(n for n in session.result.root.walk() if n.role == "button")
    assert "read" not in button.cap_names()
    with pytest.raises(SomnusError) as err:
        session.call(Action(kind="call", ref=button.ref, capability="read"))
    assert err.value.code == ErrorCode.CAPABILITY_NOT_FOUND


# ---------------------------------------------------------------------------
# observe + read on a value that carries a real line break
# ---------------------------------------------------------------------------


class _RoundTripProvider(MockProvider):
    """set_text mutates the tree; read answers with the executor's payload."""

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        if primitive == "read":
            tree = self._trees.get(ref.window_id)
            node = tree.root.find_native(ref) if tree is not None else None
            if node is None:
                return ExecResult(ok=False, error="STALE_TARGET")
            payload = _read_payload(_ValueOnly(node.value or ""))
            return ExecResult(ok=True, message=payload) if payload else ExecResult(
                ok=False, error="ACTION_NOT_SUPPORTED", unsupported=True
            )
        return super().execute(ref, primitive, value, timeout_s)


def _decode_preview(value: str) -> str:
    """Undo the display escapes added by ``escape_text``."""
    return (
        value.replace("\\\\", "\x00")
        .replace('\\"', '"')
        .replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\x00", "\\")
    )


def test_set_text_then_observe_and_read_keep_real_line_breaks_on_one_line():
    """A CR/CRLF value must not split the node line, and read() must restore it."""
    session = Session(_RoundTripProvider(fixtures=[notepad_tree(text="seed")]))
    session.observe()
    ref = _input_ref(session)

    session.call(
        Action(
            kind="call",
            ref=ref,
            capability="set_text",
            args={"_0": "L1\r\nL2"},
            raw="CALL editor.set_text(<CRLF>)",
        )
    )
    observed = session.observe().text
    lines = [line for line in observed.splitlines() if "Text Editor" in line]
    assert len(lines) == 1, observed
    assert "\r" not in observed and "\t" not in observed, repr(observed)
    assert 'value="L1\\r\\nL2"' in lines[0], lines[0]

    out = session.dispatch(f"CALL {ref}.read()")
    assert 'value="L1\\r\\nL2"' in out and "len=6" in out, out
    raw = out.split('value="', 1)[1].split('"', 1)[0]
    assert _decode_preview(raw) == "L1\r\nL2"
