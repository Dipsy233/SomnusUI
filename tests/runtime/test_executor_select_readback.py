"""P2-1: `select` reads the element back and declines to lie.

``SelectionItemPattern.Select`` can return normally and have no effect - the
shape WPF's duplicated drop-down rows produce (the ghost copy accepts the call
and nothing changes). The executor therefore reads ``CurrentIsSelected`` back
and reports the route *unsupported* when it did not take, so the declared chain
(select -> invoke -> any other UIA-reachable gesture) can escalate. It must
never report a successful selection it cannot see.
"""

from __future__ import annotations

from types import SimpleNamespace

from somnus.providers.windows.executor import run_primitive

UIA = SimpleNamespace(
    UIA_SelectionItemPatternId=10010,
    IUIAutomationSelectionItemPattern=object(),
)


class _Element:
    """Shaped like a real UIA element: GetCurrentPattern -> IUnknown -> QI."""

    def __init__(self, pattern, *, returns_none: bool = False) -> None:
        self._pattern = pattern
        self._returns_none = returns_none

    def GetCurrentPattern(self, _pid):
        return None if self._returns_none else self._pattern


class _SelectionItem:
    def __init__(self, selected: bool, *, readable: bool = True) -> None:
        self._selected = selected
        self._readable = readable
        self.select_calls = 0

    def Select(self) -> None:
        self.select_calls += 1

    def QueryInterface(self, _interface):
        return self

    @property
    def CurrentIsSelected(self) -> bool:
        if not self._readable:
            raise AttributeError("this provider does not expose the property")
        return self._selected


def test_a_selection_that_took_is_reported_ok():
    pattern = _SelectionItem(True)
    result = run_primitive(_Element(pattern), "select", None, UIA)
    assert result.ok
    assert pattern.select_calls == 1


def test_a_selection_that_did_not_take_is_reported_unsupported():
    """The escalation signal, not a hard failure: the chain may try Invoke next."""
    pattern = _SelectionItem(False)
    result = run_primitive(_Element(pattern), "select", None, UIA)
    assert not result.ok
    assert result.unsupported
    assert result.error == "ACTION_NOT_SUPPORTED"
    assert "did not become selected" in (result.message or "")


def test_an_unreadable_selection_state_is_not_turned_into_a_failure():
    """Unobservable is not the same as "did not work"."""
    pattern = _SelectionItem(False, readable=False)
    result = run_primitive(_Element(pattern), "select", None, UIA)
    assert result.ok


def test_a_missing_selection_item_pattern_is_unsupported():
    result = run_primitive(_Element(None, returns_none=True), "select", None, UIA)
    assert not result.ok
    assert result.unsupported
