"""Scenario S6: native — real Win32 controls, hosted in WPF.

Every control on this screen maps a real HWND. That is the shape an application
built from Win32 controls (rather than a WPF/XAML one) presents, and it is worth
its own screen because the two toolkits answer UIA differently: WPF reports a
painted control, a native EDIT reports a window with a handle.

The controls also publish a Value pattern, so the ordinary write route is what
runs. The screen is therefore the plain case on purpose - it exists to prove the
runtime reaches a real native control at all, and to pin the one honest refusal
(``readonly``) that comes with it. The synthetic-input screens are the chat one.
"""

from __future__ import annotations


def test_native_controls_are_writable_inputs(native_session):
    obs = native_session.observe().text
    for automation_id, label in (
        ("native_edit", "单行输入"),
        ("native_multiline", "多行输入"),
        ("native_rich", "富文本输入"),
    ):
        line = next(l for l in obs.splitlines() if label in l and "INPUT" in l)
        assert "+set_text" in line, (automation_id, line)


def test_a_readonly_native_control_offers_no_write(native_session):
    """It maps a handle, but the platform refuses the write - say so.

    A readonly field is still an INPUT with a value; the absence of ``set_text``
    is what tells a model not to try. Granting it on the strength of the role
    would promise a write that can only fail."""
    native_session.observe()
    ref = _ref(native_session, "native_readonly")
    node = native_session.result.find(ref)
    assert "readonly" in node.states
    assert "set_text" not in node.cap_names(), node.cap_names()


def test_writing_a_native_control_uses_the_value_route(native_session):
    """A real native control is written through its own interface, no synthesis."""
    native_session.observe()
    out = native_session.dispatch(f'CALL {_ref(native_session, "native_edit")}.set_text("hello")')
    assert not out.startswith("ERR"), out
    primitives = [p for _r, p, _v in native_session.provider.executed]
    assert primitives == ["set_text"], primitives
    assert _field(native_session, "native_edit") == "hello"


def test_the_multiline_control_accepts_a_replacement(native_session):
    """A second write replaces rather than appends - the field is not a log."""
    native_session.observe()
    ref = _ref(native_session, "native_multiline")
    native_session.dispatch(f'CALL {ref}.set_text("first")')
    native_session.dispatch(f'CALL {ref}.set_text("second")')
    assert _field(native_session, "native_multiline") == "second"


def test_a_native_control_reports_its_handle_but_the_model_never_sees_it(native_session):
    """The handle is what the runtime uses; it must not reach the LLM text.

    The screen carries real handles so the write path is real, but a handle in
    the observation would be a leak - it is the exact native identity the
    semantic layer exists to hide."""
    native_session.observe()
    obs = native_session.observe().text
    for node in native_session.result.root.walk():
        source = native_session.result.sources.get(node.ref)
        if source is None:
            continue
        handle = source.native.native_window_handle
        if handle:
            assert hex(handle) not in obs, hex(handle)
            assert str(handle) not in obs, str(handle)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ref(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.ref
    raise AssertionError(f"no node {automation_id!r}")


def _field(session, automation_id: str) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node.value or ""
    raise AssertionError(f"no field {automation_id!r}")
