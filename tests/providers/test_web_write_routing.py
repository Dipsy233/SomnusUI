"""The web-write channel's routing rules.

Two faults are possible here and both are silent, which is why they are tested
with fakes rather than only live:

* routing a browser's own chrome (the address bar) through the page lookup
  would write into the page's editor instead of the field that was named;
* routing a non-debuggable application through the channel would cost latency
  on every write and could reach the wrong application's page.
"""

from __future__ import annotations

import pytest

from somnus.providers.windows.uia import _CT_DOCUMENT, _CT_WINDOW, UiaProvider


class _FakeWalker:
    """A UIA tree walker over a fixed ancestor chain."""

    def __init__(self, chain):
        self._by_child = chain

    def GetParentElement(self, el):
        return self._by_child.get(id(el))


class _FakeElement:
    def __init__(self, control_type, parent=None):
        self.CurrentControlType = control_type
        self._parent = parent


def _provider_with_chain(chain):
    """A provider whose walker follows ``chain`` (leaf first)."""
    provider = UiaProvider.__new__(UiaProvider)  # no COM initialisation
    mapping = {}
    elements = chain
    for i, el in enumerate(elements):
        if i + 1 < len(elements):
            mapping[id(el)] = elements[i + 1]
    provider._auto = type("Auto", (), {"ControlViewWalker": _FakeWalker(mapping)})()
    return provider


def test_element_inside_a_document_is_web_content():
    """Real nesting is Edit -> ... -> Document -> ... -> Window, so the walk
    meets the page boundary before the window and stops there."""
    window = _FakeElement(_CT_WINDOW)
    doc = _FakeElement(_CT_DOCUMENT, parent=window)
    field = _FakeElement(50004, parent=doc)  # an Edit inside the page
    provider = _provider_with_chain([field, doc, window])
    assert provider._is_web_content(field) is True


def test_element_before_any_document_is_not_web_content():
    """A browser's own chrome sits under the window but *outside* the page, so
    the walk reaches the window first and stops."""
    window = _FakeElement(_CT_WINDOW)
    omnibox = _FakeElement(50004, parent=window)
    provider = _provider_with_chain([omnibox, window])
    assert provider._is_web_content(omnibox) is False


def test_missing_parents_are_not_web_content():
    """A dead pointer must fail closed: keeping the native route is safe,
    guessing "web content" is not."""
    lone = _FakeElement(50004)
    provider = _provider_with_chain([lone])
    assert provider._is_web_content(lone) is False


def test_no_write_channel_is_attempted_for_a_non_web_element():
    """`_try_web_set_text` must not reach the browser at all for chrome."""
    provider = UiaProvider.__new__(UiaProvider)
    provider._cdp = False  # bridge unavailable
    provider._process_by_window = {"w1": "msedge.exe"}
    window = _FakeElement(_CT_WINDOW)
    omnibox = _FakeElement(50004, parent=window)
    provider._auto = type(
        "Auto", (), {"ControlViewWalker": _FakeWalker({id(omnibox): window})}
    )()
    assert provider._try_web_set_text(_ref(), omnibox, "x") is None


def _ref():
    from somnus.ir.node import NativeRef

    return NativeRef(window_id="w1", runtime_id=(1,))


def test_bridge_absence_is_not_an_error():
    """No CDP support installed must not raise: the native routes still work."""
    provider = UiaProvider.__new__(UiaProvider)
    provider._cdp = False
    assert provider._cdp_bridge() is None
