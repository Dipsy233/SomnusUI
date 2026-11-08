"""Fingerprint identity under mutation: the ref-merge's correctness cases.

The registry merges refs across compilations by ``(window_id, fingerprint)``.
That merge is only as good as the fingerprint's stability, so these tests pin
the three cases that decide it:

* an element-unique automation id survives sibling deletion (position-free);
* an automation id a template repeats across parents keeps its copies distinct;
* an id duplicated among one parent's children is disambiguated by role index.
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.ir.node import RawNode, RawTree, WindowInfo
from tests.fixtures.builders import _BUTTON, _n, restaurant_demo_tree


def _fingerprint_by_aid(tree: RawTree) -> dict[str, str]:
    result = Compiler().compile(tree)
    out: dict[str, str] = {}
    for node in result.root.walk():
        source = result.sources.get(node.ref)
        aid = source.native.automation_id if source else None
        if aid:
            out[aid] = node.fingerprint
    return out


def _ref_by_aid(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.ref
    raise AssertionError(f"no node with automation id {automation_id!r}")


def test_sibling_deletion_keeps_survivors_fingerprints():
    """Deleting product_0 must not re-fingerprint product_1/product_2: their
    ids are element-unique, so position evidence is dropped."""
    tree = restaurant_demo_tree()
    before = _fingerprint_by_aid(tree)

    products = tree.root.children[0]  # the products list
    products.children.pop(0)  # delete the first row

    after = _fingerprint_by_aid(tree)
    assert after["product_1_add"] == before["product_1_add"]
    assert after["product_2_add"] == before["product_2_add"]


def test_template_repeated_ids_across_parents_stay_distinct():
    """Two rows reusing the same id are a *class*, not an element: their
    fingerprints must differ even though the id is identical."""
    wid = "w1"

    def row(idx: int) -> RawNode:
        return _n(
            wid,
            (1, idx),
            "group",
            automation_id=f"row_{idx}",
            children=[
                _n(
                    wid,
                    (1, idx, 0),
                    "text",
                    name=f"Row {idx}",
                    automation_id=f"row_{idx}_title",
                ),
                _n(
                    wid,
                    (1, idx, 1),
                    "button",
                    name="Add",
                    patterns=_BUTTON,
                    automation_id="row_add",  # the template's repeated id
                ),
            ],
        )

    tree = RawTree(
        window=WindowInfo(
            window_id=wid, title="T", process="p.exe", pid=1, active=True
        ),
        root=_n(
            wid,
            (0,),
            "pane",
            automation_id="root",
            children=[row(0), row(1)],
        ),
        raw_revision=1,
    )
    result = Compiler().compile(tree)
    button_fps = {
        node.fingerprint
        for node in result.root.walk()
        if node.role == "button"
    }
    # The two template buttons share the id but keep distinct fingerprints.
    assert len(button_fps) == 2, button_fps


def test_duplicated_ids_among_siblings_get_role_index():
    """The compiler already disambiguates same-parent duplicates with a role
    index; that must keep working under the position-free rule."""
    wid = "w1"
    tree = RawTree(
        window=WindowInfo(
            window_id=wid, title="T", process="p.exe", pid=1, active=True
        ),
        root=_n(
            wid,
            (0,),
            "pane",
            automation_id="root",
            children=[
                _n(
                    wid,
                    (0, i),
                    "button",
                    name="Add",
                    patterns=_BUTTON,
                    automation_id="dup_add",
                )
                for i in range(2)
            ],
        ),
        raw_revision=1,
    )
    result = Compiler().compile(tree)
    fingerprints = {
        node.fingerprint
        for node in result.root.walk()
        if node.role == "button"
    }
    assert len(fingerprints) == 2


def test_registry_keeps_refs_across_a_row_deletion():
    """End to end: observe, delete a row underneath the session, observe again
    - the surviving rows keep their refs, which is what makes the ref-merge
    usable across turns."""
    from somnus.providers.mock import MockProvider
    from somnus.runtime.session import Session

    tree = restaurant_demo_tree()
    provider = MockProvider(fixtures=[tree])
    session = Session(provider)
    try:
        session.observe()
        ref_before = _ref_by_aid(session, "product_1_add")

        products = tree.root.children[0]
        products.children.pop(0)  # the app deletes the first row
        session.observe()

        assert _ref_by_aid(session, "product_1_add") == ref_before
    finally:
        session.close()
