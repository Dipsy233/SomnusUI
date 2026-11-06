"""Re-resolvable locators: pure recipes over a compiled tree.

The locator is the second relocation strategy, so every test here is a pure
function call - no session, no provider, no desktop. The point of the recipe is
that it answers with *every* match and refuses to pick one on a tie.
"""

from __future__ import annotations

from somnus.compiler.compile import Compiler
from somnus.ir.node import RawTree, WindowInfo
from somnus.runtime import locator
from tests.fixtures.builders import _n

_BUTTON = frozenset({"invoke"})
_INPUT = frozenset({"value"})


def _compile(children):
    wid = "w1"
    root = _n(wid, (), "pane", name="面板", children=children)
    return Compiler().compile(
        RawTree(
            window=WindowInfo(
                window_id=wid, title="窗口", process="demo.exe", pid=1, active=True
            ),
            root=root,
        )
    )


def _tree(layout: str = "standard"):
    wid = "w1"
    if layout == "standard":
        children = [
            _n(wid, (0,), "group", "数字键盘", children=[
                _n(wid, (0, 0), "button", "七", patterns=_BUTTON,
                   automation_id="seven"),
                _n(wid, (0, 1), "button", "八", patterns=_BUTTON,
                   automation_id="eight"),
            ]),
            _n(wid, (1,), "input", "备注", patterns=_INPUT, automation_id="note"),
        ]
    else:
        children = [
            _n(wid, (0,), "group", "数字键盘", children=[
                _n(wid, (0, 0), "button", "七", patterns=_BUTTON,
                   automation_id="seven_copy"),
            ]),
            _n(wid, (1,), "group", "数字键盘", children=[
                _n(wid, (1, 0), "button", "七", patterns=_BUTTON,
                   automation_id="seven_two"),
            ]),
        ]
    return _compile(children)


def test_an_exact_name_matches_after_whitespace_and_case_normalization():
    result = _tree()
    found = locator.match(result.root, locator.Locator(name="  七 "))
    assert found.unique
    assert found.ref == "b1"
    assert locator.match(result.root, locator.Locator(name="x")).found == 0


def test_role_and_entity_filters_narrow_the_recipe():
    result = _tree()
    found = locator.match(result.root, locator.Locator(role="button", name="八"))
    assert found.ref == "b2"
    assert locator.match(result.root, locator.Locator(role="input", name="八")).found == 0


def test_a_regex_name_is_an_opt_in_widening():
    result = _tree()
    found = locator.match(result.root, locator.Locator(role="button", name_re="^七$"))
    assert found.unique and found.ref == "b1"


def test_a_capability_is_the_tie_breaker():
    result = _tree("duplicate")
    plain = locator.match(result.root, locator.Locator(role="button", name="七"))
    assert plain.found == 2
    assert plain.ref is None
    capped = locator.match(
        result.root, locator.Locator(role="button", name="七", capability="activate")
    )
    assert capped.found == 2  # both are invokable; capability alone cannot split them


def test_a_capability_that_only_one_candidate_satisfies_is_unique():
    wid = "w1"
    result = _compile(
        [
            _n(wid, (0,), "group", "数字键盘", children=[
                _n(wid, (0, 0), "button", "七", patterns=_BUTTON,
                   automation_id="seven"),
            ]),
            _n(wid, (1,), "group", "数字键盘", children=[
                # No `invoke`: the compiler offers expand/collapse, not activate.
                _n(wid, (1, 0), "button", "七",
                   patterns=frozenset({"expandcollapse"}),
                   automation_id="seven_collapsed"),
            ]),
        ]
    )
    capped = locator.match(
        result.root, locator.Locator(role="button", name="七", capability="activate")
    )
    assert capped.unique, capped.matches


def test_states_and_scope_narrow_the_search():
    wid = "w1"
    result = _compile(
        [
            _n(wid, (0,), "group", "左", children=[
                _n(wid, (0, 0), "button", "确定", patterns=_BUTTON,
                   automation_id="ok_left"),
            ]),
            _n(wid, (1,), "group", "右", children=[
                _n(wid, (1, 0), "button", "确定", patterns=_BUTTON,
                   states=frozenset({"disabled"}), automation_id="ok_right"),
            ]),
        ]
    )
    assert locator.match(result.root, locator.Locator(name="确定")).found == 2
    assert locator.match(
        result.root, locator.Locator(name="确定", states=frozenset({"disabled"}))
    ).ref == "b2"
    scope = next(n for n in result.root.walk() if n.name == "左")
    assert locator.match(
        result.root, locator.Locator(name="确定", scope=scope.ref)
    ).ref == "b1"
    assert locator.match(
        result.root, locator.Locator(name="确定", scope="nope")
    ).found == 0


def test_index_selects_one_candidate_in_document_order():
    result = _tree("duplicate")
    first = locator.match(result.root, locator.Locator(role="button", name="七", index=0))
    last = locator.match(result.root, locator.Locator(role="button", name="七", index=-1))
    assert first.unique and last.unique
    assert first.ref != last.ref


def test_a_tie_returns_candidates_instead_of_guessing():
    result = _tree("duplicate")
    found = locator.match(result.root, locator.Locator(role="button", name="七"))
    assert found.found == 2 and found.ref is None
    lines = found.candidate_lines()
    assert len(lines) == 2
    assert all(line.startswith(("b", "g")) for line in lines)


def test_an_ambiguity_hint_names_the_lookalikes_of_a_ref():
    from somnus.ir.node import SemanticNode

    root = SemanticNode(ref="w1", role="window", fingerprint="window")
    for ref in ("b1", "b2"):
        root.children.append(
            SemanticNode(
                ref=ref, role="button", name="七", fingerprint="same-shape"
            )
        )
    other = locator.ambiguous_candidates(root, "b2")
    assert len(other) == 1, other
    assert "b1" in other[0] and "七" in other[0]


def test_a_record_builds_a_recipe_and_describes_it():
    class Record:
        role = "button"
        name = "保存"
        entity = None

    recipe = locator.from_record(Record(), capability="activate")
    rendered = recipe.describe()
    assert "role=button" in rendered
    assert 'name="保存"' in rendered
    assert "cap=activate" in rendered
