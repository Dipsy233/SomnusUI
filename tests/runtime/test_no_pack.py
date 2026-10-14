"""The ``--no-pack`` switch.

A pack is an optimisation, not a requirement, so there must be a supported way
to say "compile this window generically" - without it, driving an application
that has no pack means reaching into the session from outside.
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.ir.node import NativeRef, RawNode, RawTree, WindowInfo
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from tests.fixtures.builders import notepad_tree, restaurant_demo_tree


def _session(*, allow_packs: bool) -> Session:
    return Session(
        MockProvider(fixtures=[notepad_tree()]),
        allow_packs=allow_packs,
    )


def test_no_pack_drops_pack_selection():
    """With allow_packs=False the session never resolves a pack, even for a
    process that has one."""
    session = _session(allow_packs=False)
    session.observe()
    assert session._current_pack() is None


def test_packs_are_used_by_default():
    """The default is unchanged: a window whose process has a pack gets it."""
    session = Session(
        MockProvider(fixtures=[restaurant_demo_tree()]),
        allow_packs=True,
    )
    session.observe()
    pack = session._current_pack()
    assert pack is not None and pack.id == "restaurant-demo"


def test_no_pack_still_compiles_controls():
    """Generic compilation is what makes the switch useful: the same window
    still yields operable nodes, just without the pack's entity vocabulary."""
    tree = notepad_tree()
    with_pack = Compiler().compile(tree, pack=None)
    roles = {n.role for n in with_pack.root.walk()}
    assert "input" in roles or "button" in roles
    assert any("set_text" in n.cap_names() for n in with_pack.root.walk())


def test_no_pack_has_no_entity_vocabulary():
    """The one thing it costs: `PRODUCT`-style names are gone."""
    tree = notepad_tree()
    result = Compiler().compile(tree, pack=None)
    assert not [n for n in result.root.walk() if n.entity == "document"]
