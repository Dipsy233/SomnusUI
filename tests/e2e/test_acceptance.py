"""End-to-end acceptance test (mock provider).

Reproduces dev doc section 19.1 exactly: the LLM-facing transcript for
"order a Big Mac and fries, then open the cart". Runs anywhere; the Windows
E2E variant (``-m windows``) drives the real WPF app.
"""

from __future__ import annotations

import pytest

from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree


@pytest.fixture
def session():
    provider = MockProvider(
        fixtures=[restaurant_demo_tree()], scripts=demo_scripts()
    )
    return Session(provider)


def test_acceptance_transcript(session):
    observe = session.observe().text
    assert 'WINDOW w1 "Restaurant Demo" active' in observe
    assert 'LIST l1 "Products" count=3 entity=product +find' in observe
    assert 'CART c1 "Cart" count=0 total=0 +open' in observe
    assert 'CHECKOUT b5 "Checkout" +submit!' in observe

    found = session.dispatch('FIND product "Big Mac"')
    assert found.startswith("FOUND 1")
    assert 'PRODUCT p1 "Big Mac" price=5.99 +add' in found

    add1 = session.dispatch("CALL p1.add()")
    assert add1.startswith("OK rev=2")
    assert "~ c1 count 0→1" in add1
    assert "~ c1 total 0→5.99" in add1

    found2 = session.dispatch('FIND product "Fries"')
    assert found2.startswith("FOUND 1")
    add2 = session.dispatch("CALL p2.add()")
    assert add2.startswith("OK rev=3")
    assert "~ c1 count 1→2" in add2
    assert "~ c1 total 5.99→8.48" in add2

    open_cart = session.dispatch("CALL c1.open()")
    assert open_cart.startswith("OK")


def test_acceptance_repeatable_100x():
    """100 fresh runs must all succeed (dev doc section 19.1)."""
    for _ in range(100):
        provider = MockProvider(
            fixtures=[restaurant_demo_tree()], scripts=demo_scripts()
        )
        session = Session(provider)
        session.observe()
        assert session.dispatch("CALL p1.add()").startswith("OK")
        assert session.dispatch("CALL p2.add()").startswith("OK")


def test_never_calls_submit_unprompted(session):
    """The acceptance run must never invoke the critical checkout."""
    session.observe()
    session.dispatch("CALL p1.add()")
    session.dispatch("CALL p2.add()")
    session.dispatch("CALL c1.open()")
    primitives = [entry["primitive"] for entry in session.audit_log()]
    # Only activate (from add/open) may appear; submit is gated.
    assert all(p not in ("submit",) for p in primitives)
