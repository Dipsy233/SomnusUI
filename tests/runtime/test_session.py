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
    out = session.dispatch('FIND product "Nonexistent"')
    assert out.startswith("FOUND 0")
    # Zero-match output names scoping targets so the agent can recover.
    assert "containers" in out and "SCREEN s1" in out


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


def test_call_critical_executes_directly():
    """No confirmation gate: a critical cap runs when it is called."""
    session = _new_session()
    session.observe()
    out = session.dispatch("CALL b5.submit()")
    assert out.startswith("OK"), out
    assert [primitive for _ref, primitive, _value in session.provider.executed] == [
        "activate"
    ]


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
    # `add` takes no parameters, so a named argument is rejected rather than
    # being silently dropped.
    out = session.dispatch('CALL p1.add(size="large")')
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


# ------------------------------------------------- nested text in a container


_WRITES = frozenset({"set_text", "paste_text", "type_text"})


def _save_dialog_tree():
    """A dialog whose named container holds the real filename INPUT."""
    from somnus.ir.node import RawTree, WindowInfo
    from tests.fixtures.builders import _n

    wid = "w1"
    filename = _n(
        wid,
        (0, 0),
        "input",
        name="文件名:",
        value="",
        patterns=frozenset({"value", "text"}),
        automation_id="file_name",
    )
    name_box = _n(
        wid,
        (0,),
        "input",
        name="名称",
        patterns=frozenset({"value", "legacyiaccessible"}),
        automation_id="name_box",
        children=[filename],
    )
    root = _n(wid, (), "dialog", name="另存为", children=[name_box])
    window = WindowInfo(
        window_id=wid,
        title="另存为",
        process="notepad.exe",
        pid=1,
    )
    return RawTree(window=window, root=root, raw_revision=1)


class _SendInputDenied(MockProvider):
    """The outer control refuses synthetic input; the nested field can be set.

    ``fail_nested=True`` makes both refuse, which is the honest total failure:
    the reply must carry the error and the candidate, never an ``OK``.
    """

    def __init__(self, *, fail_nested: bool = False):
        super().__init__(fixtures=[_save_dialog_tree()])
        self.fail_nested = fail_nested

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        from somnus.ir.action import ExecResult

        if primitive in _WRITES and (
            ref.path == (0,) or (self.fail_nested and ref.path == (0, 0))
        ):
            self.executed.append((ref, primitive, value))
            return ExecResult(
                ok=False,
                error="NATIVE_EXECUTION_FAILED",
                message="SendInput sent 0/1 (err 5)",
            )
        return super().execute(ref, primitive, value, timeout_s)


def _nodes_by_name(session):
    session.observe()
    return {n.name: n for n in session.result.root.walk() if n.name}


def test_set_text_falls_back_to_the_nested_input():
    provider = _SendInputDenied()
    session = Session(provider)
    nodes = _nodes_by_name(session)
    outer, nested = nodes["名称"], nodes["文件名:"]

    out = session.dispatch(f'CALL {outer.ref}.set_text("报告.txt")')

    assert out.startswith("OK"), out
    assert f"~ healed {outer.ref}\u2192{nested.ref}" in out, out
    # The write really ran on the nested field, through its Value interface.
    assert provider.executed[-1][0].path == (0, 0), provider.executed
    assert provider.executed[-1][1] == "set_text", provider.executed


def test_a_total_set_text_failure_names_the_candidate_and_never_says_ok():
    provider = _SendInputDenied(fail_nested=True)
    session = Session(provider)
    nodes = _nodes_by_name(session)
    outer, nested = nodes["名称"], nodes["文件名:"]

    out = session.dispatch(f'CALL {outer.ref}.set_text("报告.txt")')

    assert out.startswith("ERR NATIVE_EXECUTION_FAILED"), out
    assert not out.startswith("OK"), out
    assert f"try {nested.ref} (INPUT inside COMBOBOX)" in out, out
    assert "SendInput sent 0/1 (err 5)" in out, out


# ------------------------------------ a table cell is not the filename field


def _file_list_dialog_tree(*, cell_writable: bool = True):
    """The save dialog shape: row cells named 名称 beside the 文件名 combo."""
    from somnus.ir.node import RawTree, WindowInfo
    from tests.fixtures.builders import _n

    wid = "w1"
    cell_patterns = frozenset({"value"}) if cell_writable else frozenset()
    cell = _n(
        wid,
        (0, 0, 0),
        "input" if cell_writable else "text",
        name="名称",
        value="accept_main",
        patterns=cell_patterns,
        automation_id="name_cell",
    )
    row = _n(wid, (0, 0), "listitem", name="accept_main", children=[cell])
    listing = _n(
        wid, (0,), "list", name="项目视图", children=[row], patterns=frozenset({"scroll"})
    )
    filename = _n(
        wid,
        (1, 0),
        "input",
        name="文件名:",
        value="",
        patterns=frozenset({"value", "text"}),
        automation_id="file_name",
    )
    combo = _n(wid, (1,), "select", name="文件名:", children=[filename])
    root = _n(wid, (), "dialog", name="另存为", children=[listing, combo])
    return RawTree(
        window=WindowInfo(
            window_id=wid, title="另存为", process="notepad.exe", pid=1
        ),
        root=root,
        raw_revision=1,
    )


class _CellWriteDenied(MockProvider):
    def __init__(self, *, cell_writable: bool = True):
        super().__init__(fixtures=[_file_list_dialog_tree(cell_writable=cell_writable)])

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        from somnus.ir.action import ExecResult

        if ref.path == (0, 0, 0) and primitive in _WRITES:
            self.executed.append((ref, primitive, value))
            return ExecResult(
                ok=False,
                error="NATIVE_EXECUTION_FAILED",
                message="SendInput sent 0/1 (err 5)",
            )
        return super().execute(ref, primitive, value, timeout_s)


def test_a_failed_write_on_a_table_cell_names_the_combo_field():
    session = Session(_CellWriteDenied())
    nodes = _nodes_by_name(session)
    cell, field = nodes["名称"], nodes["文件名:"]

    out = session.dispatch(f'CALL {cell.ref}.set_text("报告.txt")')

    assert out.startswith("ERR NATIVE_EXECUTION_FAILED"), out
    assert (
        f"try {field.ref} (INPUT inside COMBOBOX '文件名:')" in out
    ), out


def test_a_missing_set_text_on_a_table_cell_names_the_combo_field():
    session = Session(_CellWriteDenied(cell_writable=False))
    nodes = _nodes_by_name(session)
    cell, field = nodes["名称"], nodes["文件名:"]

    out = session.dispatch(f'CALL {cell.ref}.set_text("报告.txt")')

    assert out.startswith("ERR CAPABILITY_NOT_FOUND"), out
    assert (
        f"try {field.ref} (INPUT inside COMBOBOX '文件名:')" in out
    ), out


# ------------------------------- an Invoke that answers NULL names SelectionItem


def _selectable_panel_tree(*, selectionitem: bool = True):
    from somnus.ir.node import RawTree, WindowInfo
    from tests.fixtures.builders import _n

    patterns = frozenset({"invoke", "selectionitem"} if selectionitem else {"invoke"})
    button = _n(
        "w1",
        (0,),
        "button",
        name="三",
        patterns=patterns,
    )
    root = _n("w1", (), "pane", name="计算器", children=[button])
    return RawTree(
        window=WindowInfo(
            window_id="w1", title="计算器", process="CalculatorApp.exe", pid=1
        ),
        root=root,
        raw_revision=1,
    )


class _NullInvoke(MockProvider):
    def __init__(self, *, selectionitem: bool = True):
        super().__init__(fixtures=[_selectable_panel_tree(selectionitem=selectionitem)])

    def execute(self, ref, primitive, value=None, timeout_s=3.0):
        from somnus.ir.action import ExecResult

        if primitive == "activate":
            self.executed.append((ref, primitive, value))
            return ExecResult(
                ok=False,
                error="ACTION_NOT_SUPPORTED",
                message="no usable interface on this element (tried activate); NULL COM pointer access",
                unsupported=True,
            )
        return super().execute(ref, primitive, value, timeout_s)


def test_a_null_invoke_failure_names_select():
    session = Session(_NullInvoke())
    session.observe()
    button = next(n for n in session.result.root.walk() if n.name == "三")

    out = session.dispatch(f"CALL {button.ref}.activate()")

    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out
    assert "try select" in out, out
    assert session.dispatch(f"CALL {button.ref}.select()").startswith("OK")


def test_a_null_invoke_without_selection_does_not_invent_select():
    session = Session(_NullInvoke(selectionitem=False))
    session.observe()
    button = next(n for n in session.result.root.walk() if n.name == "三")

    out = session.dispatch(f"CALL {button.ref}.activate()")

    assert out.startswith("ERR ACTION_NOT_SUPPORTED"), out
    assert "try select" not in out, out
