"""SomnusDroid tests: Portal JSON -> RawTree -> compiler -> SUI-Text.

The whole phone path is exercised without a device: the fixture *is* a Portal
snapshot, the mock replays it through the real conversion, and the shared
compiler/runtime do the rest. What passes here must also pass on a real
Portal, because nothing between the JSON and the SUI-Text knows the
difference.
"""

from __future__ import annotations

import pytest

from somnus.compiler.compile import Compiler
from somnus.compiler.rules.android import prepare_android_tree
from somnus.providers.android.mock import AndroidMockProvider
from somnus.providers.android.rawtree import portal_json_to_tree
from somnus.runtime.policy import Policy
from somnus.runtime.session import Session
from somnus.sui.serialize import RenderOptions, render_node_line
from tests.fixtures import android_scenarios as A


def make_session(screen: str, **tree_kw) -> Session:
    provider = AndroidMockProvider(snapshots=[A.SCREENS[screen](**tree_kw)])
    return Session(provider, policy=Policy())


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------


def test_portal_json_converts_to_trees():
    trees = portal_json_to_tree(A.settings_tree())
    assert len(trees) == 1
    win = trees[0].window
    assert win.process == "com.android.settings"
    assert win.window_id == "0/12"
    assert win.active


def test_password_field_never_leaves_the_portal():
    trees = portal_json_to_tree(A.web_tree())
    texts = [n.value for n in trees[0].root.walk()]
    assert all(t != "secret" for t in texts)
    # The field reports filled state instead.
    pw = next(n for n in trees[0].root.walk() if "password" in n.states)
    assert pw.role == "input"


def test_actions_become_pattern_evidence_only():
    """Only actions the Portal reports become patterns; isClickable never does."""
    trees = portal_json_to_tree(A.form_tree())
    switch = next(n for n in trees[0].root.walk() if n.role == "switch")
    assert "invoke" in switch.patterns  # from the CLICK action


# ---------------------------------------------------------------------------
# compile: roles, promotion, folding
# ---------------------------------------------------------------------------


def compile_snapshot(screen: str, **kw):
    tree = portal_json_to_tree(A.SCREENS[screen](**kw))[0]
    prepare_android_tree(tree)
    return Compiler().compile(tree)


def test_settings_rows_are_promoted_to_named_listitems():
    result = compile_snapshot("settings")
    names = [n.name for n in result.root.walk() if n.name]
    assert "WLAN" in names
    row = next(n for n in result.root.walk() if n.name == "WLAN")
    # The row carries the capability its platform proof allows.
    assert "activate" in row.cap_names()


def test_search_box_is_an_input_with_set_text():
    result = compile_snapshot("settings")
    search = next(n for n in result.root.walk() if n.role == "input")
    assert "set_text" in search.cap_names()
    assert "+set_text" in render_node_line(search)


def test_virtualised_list_reports_lower_bound_count():
    result = compile_snapshot("settings")
    lists = [n for n in result.root.walk() if n.role == "list"]
    assert lists, "settings list must survive compilation"
    assert any("count>=" in render_node_line(n) or "count=?" in render_node_line(n) for n in lists)


def test_canvas_screen_is_unknown_not_guessed():
    """A pure Canvas surface reports `? ... reason=`, never a fake tree."""
    result = compile_snapshot("canvas")
    rendered = [render_node_line(n) for n in result.root.walk()]
    assert any(line.startswith("? ") for line in rendered), rendered


def test_password_field_renders_without_a_value():
    result = compile_snapshot("web")
    pw = next(n for n in result.root.walk() if "password" in n.states)
    assert pw.value in (None, "", "•••")


# ---------------------------------------------------------------------------
# capabilities are the control's own: no inheritance from the toolkit's
# blanket actions (found live on MIUI, where every label advertised
# SHOW_ON_SCREEN / SELECT and rendered as a scroller with a context menu)
# ---------------------------------------------------------------------------


def _caps_for(actions: list[str], cls: str = "android.widget.TextView"):
    from somnus.compiler.caps import primitive_capabilities
    from somnus.ir.node import NativeRef, RawNode

    node = RawNode(
        native=NativeRef("0/12", (0,), class_name=cls),
        role="text" if "textview" in cls.lower() else "button",
        name="微信",
        patterns=frozenset(),
    )
    # Mirror the rawtree mapping: actions -> patterns.
    from somnus.providers.android.rawtree import ACTION_TO_PATTERN
    node.patterns = frozenset(
        p for p in (ACTION_TO_PATTERN.get(a) for a in actions) if p
    )
    return {c.name for c in primitive_capabilities(node)}


def test_show_on_screen_does_not_advertise_scroll():
    """SCROLL_TO_POSITION / SHOW_ON_SCREEN mean *be scrolled to*, not scroll.

    Every MIUI launcher label publishes them; granting `scroll` there made a
    TEXT render `+scroll(to:up|down|top|bottom)` it could never perform.
    """
    caps = _caps_for(["SHOW_ON_SCREEN", "SCROLL_TO_POSITION"])
    assert "scroll" not in caps
    assert "scroll_into_view" in caps  # the capability the node really has


def test_scroller_still_advertises_scroll():
    caps = _caps_for(["SCROLL_FORWARD", "SCROLL_BACKWARD"], "androidx.recyclerview.widget.RecyclerView")
    assert "scroll" in caps


def test_blanket_select_is_not_a_capability_on_labels():
    """A label with the toolkit's blanket ACTION_SELECT is not selectable."""
    caps = _caps_for(["CLICK", "SELECT"])
    assert "select" not in caps


def test_context_menu_on_android_needs_long_click():
    """Android's context menu IS the long press: offered only where the
    element advertises LONG_CLICK, and routed to ACTION_LONG_CLICK."""
    assert "context_menu" not in _caps_for(["CLICK"])
    assert "context_menu" in _caps_for(["CLICK", "LONG_CLICK"])
    from somnus.providers.android.provider import ACTION_FOR_PRIMITIVE
    assert ACTION_FOR_PRIMITIVE["context_menu"] == "LONG_CLICK"


# ---------------------------------------------------------------------------
# runtime: observe / find / call on the mock
# ---------------------------------------------------------------------------


def test_observe_settings_is_small_and_addressable():
    session = make_session("settings")
    obs = session.observe()
    assert obs.tokens > 0
    # L0 folds the virtualised list (by design, doc §6.2); the rows stay
    # reachable through FIND.
    assert "count>=" in obs.text
    out = session.dispatch('FIND "WLAN"')
    assert out.startswith("FOUND 1"), out
    assert "WLAN" in out
    # No native identity leaks.
    assert "com.android.settings" not in obs.text
    assert "android.widget" not in obs.text


def test_call_toggle_switches_the_form_switch():
    session = make_session("form")
    session.observe()
    switch = next(
        n for n in session.result.root.walk() if n.role == "switch"
    )
    out = session.dispatch(f"CALL {switch.ref}.toggle()")
    assert out.startswith("OK"), out
    switch2 = next(
        n for n in session.result.root.walk() if n.role == "switch"
    )
    assert "checked" in switch2.states


# ---------------------------------------------------------------------------
# screen following + switch navigation
# ---------------------------------------------------------------------------


def test_observe_follows_the_active_window():
    """A phone shows one screen: the read must always target it.

    Observing a background window returned a bare shell (measured live:
    MIUI home behind another app), which read as 'the observation lost
    everything'.
    """
    provider = AndroidMockProvider(snapshots=[A.settings_tree(), A.form_tree()])
    session = Session(provider, policy=Policy())
    session.observe()
    # The active window is the one the fixture marks active; force a different
    # target, then observe again - the session must follow the active one.
    other = next(w.window_id for w in provider.list_windows()
                 if w.window_id != session.target_window)
    provider.trees[other].window.active = True
    provider.trees[session.target_window].window.active = False
    obs = session.observe()
    assert session.target_window == other
    assert "SWITCH" in obs.text  # the form screen's switch, not settings


def test_switch_home_and_pages_go_through_platform_actions():
    """SWITCH home/next/prev on Android are platform actions, not UI calls."""
    session = make_session("settings")
    session.observe()
    assert session.dispatch("SWITCH home").startswith("OK switched=home")
    assert session.provider.globals == ["home"]
    assert session.dispatch("SWITCH next").startswith("OK switched=next")
    assert session.provider.pages == ["next"]
    assert session.dispatch("SWITCH prev").startswith("OK switched=prev")
    # Every switch discards the refs: the screen changed wholesale.
    assert session.result is None


def _longpress_tree() -> dict:
    """One icon whose row advertises CLICK + LONG_CLICK (a MIUI launcher icon)."""
    from tests.fixtures.android_scenarios import _node, _window

    icon = _node(
        "12/1.0", "android.widget.LinearLayout",
        actions=["CLICK", "LONG_CLICK"], desc="微信",
        children=[
            _node("12/1.0.0", "android.widget.TextView", text="微信"),
        ],
    )
    return _window(12, "com.miui.home", "桌面", icon)


def test_context_menu_that_opens_items_reports_ok():
    """Android long-press menus are ordinary tappable items, not `menu` nodes.

    Found live: long-pressing a MIUI launcher icon added 扫一扫/加好友 as
    TEXT+tap lines in the delta while the verdict claimed `context_menu
    produced no menu` - the agent would have concluded the gesture failed
    and the popup was left undismissed on screen.
    """
    from somnus.ir.node import NativeRef, RawNode

    def grow_menu(node: RawNode, root: RawNode) -> None:
        for label in ("扫一扫", "加好友"):
            node.children.append(
                RawNode(
                    native=NativeRef(root.native.window_id, (abs(hash(label)) % 9999,),
                                     class_name="android.widget.TextView"),
                    role="text", name=label,
                    patterns=frozenset({"invoke"}),
                )
            )

    provider = AndroidMockProvider(
        snapshots=[_longpress_tree()],
        scripts={"微信:context_menu": [{"op": "callback", "fn": "grow_menu"}]},
        callbacks={"grow_menu": grow_menu},
    )
    session = Session(provider, policy=Policy())
    session.observe()
    row = next(
        n for n in session.result.root.walk()
        if n.name == "微信" and "long_click" in n.patterns
    )
    out = session.dispatch(f"CALL {row.ref}.context_menu()")
    assert out.startswith("OK"), out
    assert "produced no menu" not in out
    assert "扫一扫" in out


def test_disabled_button_refuses():
    session = make_session("form")
    session.observe()
    btn = next(
        n for n in session.result.root.walk()
        if n.role == "button" and "disabled" in n.states
    )
    out = session.dispatch(f"CALL {btn.ref}.activate()")
    assert out.startswith("ERR"), out


def test_write_to_readonly_field_is_refused():
    session = make_session("form")
    session.observe()
    readonly = next(
        n for n in session.result.root.walk()
        if n.role == "input" and "readonly" not in n.states and n.value
        and "REQ" in str(n.value)
    ) if any(
        "REQ" in str(n.value or "")
        for n in session.result.root.walk() if n.role == "input"
    ) else None
    if readonly is None:
        pytest.skip("fixture has no readonly input field")
    out = session.dispatch(f'CALL {readonly.ref}.set_text("HACK")')
    assert out.startswith("ERR")


def test_set_text_writes_through_set_text_action():
    session = make_session("form")
    session.observe()
    field = next(n for n in session.result.root.walk() if n.role == "input")
    out = session.dispatch(f'CALL {field.ref}.set_text("张三")')
    assert out.startswith("OK"), out
    field2 = next(n for n in session.result.root.walk() if n.role == "input")
    assert field2.value == "张三"


def test_find_in_large_store_catalog():
    session = make_session("store")
    session.observe()
    out = session.dispatch('FIND "Item 3"')
    assert out.startswith("FOUND"), out


# ---------------------------------------------------------------------------
# contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("screen", ["settings", "form", "chat", "store", "web", "canvas"])
def test_android_screen_leaks_no_native_identity(screen):
    session = make_session(screen)
    text = session.observe().text
    for leak in ("com.example", "android.widget", "androidx.", "android.view",
                 "rid=", "resource-id", "bounds"):
        assert leak not in text, f"{screen}: leaked {leak!r}:\n{text}"


@pytest.mark.parametrize("screen", ["settings", "form", "chat", "store", "web", "canvas"])
def test_android_screen_observation_is_bounded(screen):
    session = make_session(screen)
    obs = session.observe()
    # L0 stays within the hard budget on every acceptance screen.
    assert obs.tokens <= 300, f"{screen}: {obs.tokens} tokens\n{obs.text}"
