"""Lock the SUI-Text shape of each acceptance screen with a template.

Each case has two assertions on purpose:

* a template match (partial: refs, volatile counts and unnamed children may
  vary) that catches structural regressions without rewriting the file;
* one exact line (strict: refs included), because a template with wildcards can
  hide the regression it was meant to catch (`docs/archive/playwright-patterns.zh-CN.md`).

Templates live in ``tests/templates/`` and are test-only; nothing under
``somnus/`` imports the matcher.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.sui_template import assert_sui

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"

CASES = [
    (
        "store_session",
        "store_l0.sui",
        '    LIST l1 "商品" count=240 entity=item +find +240 more',
    ),
    (
        "form_session",
        "form_l0.sui",
        '    INPUT q4 "只读编号" value="REQ-0001" readonly +context_menu',
    ),
    (
        "chat_session",
        "chat_l0.sui",
        '    BUTTON b1 "发送" +context_menu',
    ),
    (
        "console_session",
        "console_l0.sui",
        '    INPUT q1 "输入" value="" +context_menu +set_text(text)',
    ),
    (
        "tree_session",
        "tree_l0.sui",
        '    TREE tr1 "目录树" count=1 +find',
    ),
]


@pytest.mark.parametrize("fixture_name,template,exact_line", CASES)
def test_observation_matches_its_template(request, fixture_name, template, exact_line):
    session = request.getfixturevalue(fixture_name)
    observed = session.observe().text
    assert_sui(observed, TEMPLATES / template)
    assert exact_line in observed.splitlines()


def test_a_single_node_template_hits_the_catalog_at_any_depth(store_session):
    """The fragment form: one top-level node may match anywhere in the tree."""
    assert_sui(store_session.observe().text, TEMPLATES / "store_catalog.sui")
