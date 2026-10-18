"""Scenario Suite fixtures as pytest fixtures.

These wrap `tests/fixtures/scenarios.py` so a test can ask for a scenario by
name and get a ready session bound to the right mock scripts. The point of the
split is that every scenario test in this directory reads as "drive this screen
and check the answer", never as provider/plumbing setup.
"""

from __future__ import annotations

import pytest

from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from tests.fixtures import scenarios as S


def _provider(tree, scripts=None, callbacks=True) -> MockProvider:
    return MockProvider(
        fixtures=[tree],
        scripts=scripts if scripts is not None else S.scenario_scripts(),
        callbacks=S.callbacks() if callbacks else None,
    )


def _session(tree, *, scripts=None, callbacks=True, **policy_kw) -> Session:
    policy = Policy(**policy_kw) if policy_kw else None
    return Session(_provider(tree, scripts, callbacks), policy=policy)


@pytest.fixture
def store_session():
    return _session(S.store_tree())


@pytest.fixture
def form_session():
    return _session(S.form_tree())


@pytest.fixture
def chat_session():
    return _session(S.chat_tree(), scripts=S.chat_scripts())


@pytest.fixture
def console_session():
    return _session(S.console_tree(), scripts=S.console_scripts())


@pytest.fixture
def tree_session():
    return _session(S.tree_tree())


@pytest.fixture
def native_session():
    return _session(S.native_tree())


@pytest.fixture
def menu_session():
    return _session(S.menu_tree())


@pytest.fixture
def dialog_session():
    return _session(S.dialog_tree())


@pytest.fixture
def make_session():
    """Build a session for any scenario, with policy knobs for the routing tests."""

    def build(tree, *, scripts=None, callbacks=True, **policy_kw):
        return _session(tree, scripts=scripts, callbacks=callbacks, **policy_kw)

    return build
