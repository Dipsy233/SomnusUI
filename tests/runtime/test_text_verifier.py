"""The delivery verifier the synthetic write channels use.

``paste_text`` and ``type_text`` need to know whether what they sent actually
arrived. The verifier reads the one surface that reports it - the Value property
when it is live, the Text pattern when it is not - and these tests pin that
choice, because getting it wrong turns "the write landed" into a fixed timeout
(measured on QQ's ProseMirror editor: reading only the dead Value property cost
1.89 s for three characters and hit the outer timeout at ten).

Pure logic, no live window: the elements are fakes shaped like the real
provider's answers.
"""

from __future__ import annotations

from somnus.providers.windows.executor import _text_landed_verifier


class _FakeTextPattern:
    """Stands in for IUIAutomationTextPattern's document range."""

    def __init__(self, store: dict) -> None:
        self._store = store
        self.DocumentRange = self

    def GetText(self, _cap):
        return self._store["text"]


class _FakeElement:
    """An element shaped like a real self-drawn editor.

    Measured live on QQ's ProseMirror editor and reproduced here:

    * the Value property answers **successfully with an empty string** - it does
      not raise, and it never changes no matter what is typed;
    * the Text pattern's document range reports the live content.

    ``value_raises`` switches to the other provider shape (a Value read that
    raises), so both "dead property" and "absent property" are covered.
    """

    def __init__(self, *, text: str = "", value: str = "", value_raises: bool = False):
        self._store = {"text": text}
        self._value = value
        self._value_raises = value_raises
        self.pattern_calls = 0

    def GetCurrentPropertyValue(self, prop):
        if prop != 30045:
            raise KeyError(prop)
        if self._value_raises:
            raise OSError("no such property")
        return self._value

    def GetCurrentPattern(self, pid):
        self.pattern_calls += 1
        if pid != 10014:  # UIA_TextPatternId
            raise OSError(f"no pattern {pid}")
        return _FakeTextPattern(self._store)

    def type(self, chars: str) -> None:
        self._store["text"] += chars


def test_the_verifier_reads_the_text_pattern_when_value_is_dead():
    """回归：Value 返回空串不算"能读"，必须改读 Text pattern。

    实测 QQ 的 ProseMirror 编辑器：``GetCurrentPropertyValue(30045)`` 成功返回
    ``''``（不是抛错），且打字后永远是 ``''``；真正的内容在 Text pattern 里。

    旧实现只看 Value，于是 ``len('') - 0 >= count`` 恒为 False，每个字符都
    跑满重试预算并在其间狂发 SHIFT——实测 3 字符 1.89s、6 字符 3.41s、
    10 字符直接被外层超时打断。这个测试钉住"信号修好了"，而不是"超时调大了"。
    """
    el = _FakeElement(text="base", value="")
    verify = _text_landed_verifier(el, "abc")
    assert verify is not None, "a Text pattern is available, so a verifier must exist"

    # Nothing typed yet: the baseline is the existing content, so one more
    # character has not arrived.
    assert verify(1) is False

    # Type one character: the Text pattern moves, and the verifier must see it
    # *immediately* rather than waiting out its retry budget.
    el.type("a")
    assert verify(1) is True

    el.type("bc")
    assert verify(3) is True


def test_the_verifier_still_prefers_a_live_value_property():
    """Value 真的能读时仍然用它，不去碰 Text pattern。

    普通 Edit 控件走 Value，读起来更便宜，也没有 Text pattern 的额外开销。
    """
    el = _FakeElement(text="ignored", value="base")
    verify = _text_landed_verifier(el, "xy")
    assert verify is not None
    assert el.pattern_calls == 0, "a live Value property must not fall back to Text"

    assert verify(1) is False
    el._value = "basex"
    assert verify(1) is True


def test_the_verifier_is_absent_when_neither_surface_works():
    """两个面都读不到时返回 None（=没有可用校验器），而不是一个永远 False 的。

    返回一个恒 False 的校验器，等于让发送方在每个字符上白等重试预算。
    诚实的"没有信号"应当让调用退回到"定速投递并如实说明"。
    """
    el = _FakeElement(value_raises=True)
    el.GetCurrentPattern = lambda pid: (_ for _ in ()).throw(OSError("no text pattern"))
    assert _text_landed_verifier(el, "abc") is None


def test_an_empty_but_readable_value_still_yields_a_verifier():
    """Value 能读但为空、且也没有 Text pattern 时，仍给出校验器（如实报"没到"）。

    这比返回 None 更好：调用方至少知道"目标没变"，而不是把不可观测
    当成"已经到达"。
    """
    el = _FakeElement(value="")
    el.GetCurrentPattern = lambda pid: (_ for _ in ()).throw(OSError("no text pattern"))
    verify = _text_landed_verifier(el, "abc")
    assert verify is not None
    assert verify(1) is False
