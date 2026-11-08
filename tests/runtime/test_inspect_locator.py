"""The strict locator line on INSPECT output.

The rule (from the ego native-snapshot requirement): a locator is printed only
when it matches exactly one node and that winner is the inspected node itself;
any doubt omits the line entirely. There is no "unstable" placeholder.
"""

from __future__ import annotations

from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.scenarios import make_provider


def _store_session() -> Session:
    session = Session(make_provider("store"))
    session.observe()
    return session


def _ref_of(session: Session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.ref
    raise AssertionError(f"no node with automation id {automation_id!r}")


def _locator_lines(out: str) -> list[str]:
    return [line.strip() for line in out.splitlines() if "locator" in line]


def test_a_uniquely_named_node_prints_its_recipe():
    session = _store_session()
    try:
        out = session.dispatch(f"INSPECT {_ref_of(session, 'store_checkout')}")
        assert _locator_lines(out) == ['locator role=button name="结算"'], out
    finally:
        session.close()


def test_an_ambiguous_name_is_omitted_entirely():
    """240 buttons share the name 加入: a recipe would be a tie, and a tie is
    not printed - there is no "unstable" placeholder to mislead the model."""
    session = _store_session()
    try:
        out = session.dispatch(f"INSPECT {_ref_of(session, 'store_item_0_add')}")
        assert _locator_lines(out) == [], out
    finally:
        session.close()


def test_a_unique_title_prints_and_synthetic_names_do_not():
    session = _store_session()
    try:
        # A product title is a real platform name: printable when unique.
        out = session.dispatch(
            f"INSPECT {_ref_of(session, 'store_item_0_title')}"
        )
        assert _locator_lines(out) == ['locator role=text name="Special Item 0"'], out
    finally:
        session.close()
