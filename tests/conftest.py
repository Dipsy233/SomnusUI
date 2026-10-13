"""Shared pytest fixtures."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from somnus.providers.mock import MockProvider  # noqa: E402
from somnus.runtime.session import Session  # noqa: E402
from tests.fixtures.builders import demo_scripts, notepad_tree, restaurant_demo_tree  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="rewrite golden SUI-Text fixtures",
    )


@pytest.fixture
def demo_tree():
    return restaurant_demo_tree()


@pytest.fixture
def notepad():
    return notepad_tree()


@pytest.fixture
def mock_provider(demo_tree):
    return MockProvider(fixtures=[demo_tree], scripts=demo_scripts())


@pytest.fixture
def session(mock_provider):
    return Session(mock_provider)
