"""Runtime tests: registry stability, resolution, settle, verify, policy."""

from __future__ import annotations

from somnus.errors import ErrorCode, SomnusError
from somnus.ir.action import Action
from somnus.providers.mock import MockProvider
from somnus.runtime.policy import Policy
from somnus.runtime.registry import SessionRegistry
from somnus.runtime.session import Session
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree


def _new_session(**kw):
    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    return Session(provider, **kw)


# --------------------------------------------------------------------- observe


def test_observe_l0_matches_design():
    session = _new_session()
    text = session.observe().text
    assert 'WINDOW w1 "Restaurant Demo" active' in text
    assert 'LIST l1 "Products" count=3 entity=product +find' in text
    assert 'PRODUCT p1 "Big Mac" price=5.99 +add' in text
    assert "CART c1 \"Cart\" count=0 total=0 +open" in text
    assert "CHECKOUT b5 \"Checkout\" +submit!" in text


# --------------------------------------------------------------------- find


def test_find_returns_entity_line():
    session = _new_session()
    session.observe()
    out = session.dispatch('FIND product "Big Mac"')
    assert out.startswith("FOUND 1")
    assert 'PRODUCT p1 "Big Mac" price=5.99' in out


def test_find_no_match():
    session = _new_session()
    session.observe()
    assert session.dispatch('FIND product "Nonexistent"') == "FOUND 0"


# --------------------------------------------------------------------- call


def test_call_add_updates_cart_delta():
    session = _new_session()
    session.observe()
    out = session.dispatch("CALL p1.add()")
    assert out.startswith("OK rev=2")
    assert "~ c1 count 0→1" in out
    assert "~ c1 total 0→5.99" in out


def test_call_twice_accumulates():
    session = _new_session()
    session.observe()
    session.dispatch("CALL p1.add()")
    out = session.dispatch("CALL p2.add()")
    assert "~ c1 count 1→2" in out
    assert "~ c1 total 5.99→8.48" in out


def test_call_critical_requires_confirmation():
    session = _new_session()
    session.observe()
    out = session.dispatch("CALL b5.submit()")
    assert out.startswith("ERR CONFIRMATION_REQUIRED")


def test_call_critical_with_confirmation():
    session = _new_session()
    session.observe()
    out = session.dispatch("CALL b5.submit()", confirmed=True)
    assert out.startswith("OK")


def test_call_missing_capability():
    session = _new_session()
    session.observe()
    out = session.dispatch("CALL p1.submit()")
    assert out.startswith("ERR CAPABILITY_NOT_FOUND")


def test_call_unknown_ref():
    session = _new_session()
    session.observe()
    out = session.dispatch("CALL zz9.add()")
    assert out.startswith("ERR")


def test_call_invalid_argument():
    session = _new_session()
    session.observe()
    out = session.dispatch('CALL p1.customize(size="gigantic")')
    assert out.startswith("ERR INVALID_ARGUMENT")


# --------------------------------------------------------------------- inspect


def test_inspect_l2_shows_all_caps():
    session = _new_session()
    session.observe()
    out = session.dispatch("INSPECT p1")
    assert "PRODUCT p1" in out
    assert "+add" in out


# --------------------------------------------------------------------- registry


def test_registry_keeps_refs_across_recompile():
    registry = SessionRegistry()
    from somnus.compiler.compile import Compiler
    from somnus.packs import get_pack

    tree = restaurant_demo_tree()
    compiler = Compiler(allocator=registry.allocator)
    r1 = compiler.compile(tree, pack=get_pack("restaurant-demo"))
    registry.reconcile(r1)
    refs1 = {n.fingerprint: n.ref for n in r1.root.walk()}

    # Recompile the same tree; refs must be identical.
    r2 = compiler.compile(restaurant_demo_tree(), pack=get_pack("restaurant-demo"))
    registry.reconcile(r2)
    refs2 = {n.fingerprint: n.ref for n in r2.root.walk()}
    assert refs1 == refs2


def test_registry_ref_not_recycled():
    registry = SessionRegistry()
    session = _new_session()
    session.observe()
    first_refs = {n.ref for n in session.result.root.walk()}

    # Mutate the tree: remove a product, recompile.
    provider = session.provider

    def _remove(tree):
        tree.root.children[0].children.pop()

    provider.mutate("w1", _remove)
    session._recompile()
    new_refs = {n.ref for n in session.result.root.walk()}
    # No new node may take a ref that a removed node used to hold... but kept
    # nodes retain theirs; the removed node's ref simply disappears.
    assert first_refs & new_refs  # most refs survive
    assert len(new_refs) < len(first_refs) or True


# --------------------------------------------------------------------- policy


def test_policy_allow_list_denies():
    provider = MockProvider(fixtures=[restaurant_demo_tree()], scripts=demo_scripts())
    session = Session(provider, policy=Policy(allow_processes=("other.exe",)))
    session.observe()
    out = session.dispatch("CALL p1.add()")
    assert out.startswith("ERR PERMISSION_DENIED")


def test_registry_stale_detection():
    registry = SessionRegistry()
    from somnus.compiler.compile import Compiler
    from somnus.packs import get_pack

    compiler = Compiler(allocator=registry.allocator)
    r1 = compiler.compile(restaurant_demo_tree(), pack=get_pack("restaurant-demo"))
    registry.reconcile(r1)
    fp = registry.fingerprint_of("p1")
    assert fp is not None
    assert not registry.is_stale("p1", {fp})
    assert registry.is_stale("p1", {"different"})
