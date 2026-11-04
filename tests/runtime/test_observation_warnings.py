"""P2-3: a refused raise or an empty shell must not look like an empty app.

A covered self-drawn window publishes an empty tree; a raise the Win32
foreground lock refused is the reason. The observation says so instead of
handing the agent "this window has no controls".
"""

from __future__ import annotations

import copy

from somnus.ir.node import RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import _n


def _tree(children, title="App"):
    return RawTree(
        window=WindowInfo(
            window_id="w1", title=title, process="demo.exe", pid=1, active=True
        ),
        root=_n("w1", (), "window", title, children=list(children)),
    )


class _ForegroundProvider(MockProvider):
    """Replays one tree and reports the raise result like the real provider."""

    def __init__(self, tree, *, foreground_error=None):
        super().__init__(fixtures=[tree])
        self.last_foreground_error: str | None = None
        self._foreground_error = foreground_error

    def snapshot_viewport(self, window_id, expanded_paths=None):
        self.viewport_meta = {}
        self.last_viewport_partial = True
        self.last_foreground_error = self._foreground_error
        return copy.deepcopy(self._trees[window_id])


def _observe(provider):
    session = Session(provider)
    return session.observe()


def _button(path, name):
    return _n("w1", path, "button", name, patterns=frozenset({"invoke"}))


def test_a_refused_raise_is_reported_even_when_the_tree_has_content():
    painter = _button((0,), "发送")
    obs = _observe(
        _ForegroundProvider(_tree([painter]), foreground_error="FOREGROUND_DENIED")
    )
    assert (
        "~ foreground denied (FOREGROUND_DENIED); tree may be empty for covered windows"
        in obs.text
    ), obs.text
    # The warning is a header line: the tree still renders below it.
    assert obs.text.index("~ foreground denied") < obs.text.index("BUTTON")


def test_a_titled_window_with_no_children_is_not_silently_empty():
    obs = _observe(_ForegroundProvider(_tree([], title="QQ")))
    assert "~ tree looks empty; window may be covered or app not rendering" in obs.text, (
        obs.text
    )


def test_a_small_window_is_never_reported_as_empty():
    """A dialog with two buttons is small, not empty: the check is strict."""
    dialog = [_button((0,), "确定"), _button((1,), "取消")]
    obs = _observe(_ForegroundProvider(_tree(dialog, title="确认删除")))
    assert "~ tree looks empty" not in obs.text
    assert "~ foreground denied" not in obs.text


def test_a_normal_observation_gains_no_warning_lines():
    normal = _button((0,), "提交")
    obs = _observe(_ForegroundProvider(_tree([normal], title="表单")))
    assert "~ foreground denied" not in obs.text
    assert "~ tree looks empty" not in obs.text


def test_a_denied_raise_on_an_empty_tree_says_both_facts():
    obs = _observe(
        _ForegroundProvider(_tree([], title="QQ"), foreground_error="FOREGROUND_DENIED")
    )
    assert "~ foreground denied (FOREGROUND_DENIED)" in obs.text
    assert "~ tree looks empty" in obs.text
