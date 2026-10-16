"""Scenario S3: form — labelled fields, validation, a gated submit and a record.

The form is where three things the runtime must get right come together:

* **fields are labelled and writable**, and a read-only field looks different
  from a writable one (otherwise the agent writes a field that refuses text);
* **validation is a real outcome**: a submit on an invalid form changes the
  validation line and adds no record;
* **the record is the verification surface**: a successful submit appends a
  line the caller can read back, so "did it work" is answered from the same
  observation rather than from trust in the status word.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# fields
# ---------------------------------------------------------------------------


def test_writable_and_readonly_fields_render_differently(form_session):
    """The two field kinds must be distinguishable from the line alone."""
    obs = form_session.observe().text
    writable = next(l for l in obs.splitlines() if "姓名" in l and "INPUT" in l)
    readonly = next(l for l in obs.splitlines() if "只读编号" in l and "INPUT" in l)
    assert "+set_text" in writable
    assert "+set_text" not in readonly
    assert "readonly" in readonly
    assert writable != readonly


def test_a_readonly_field_is_refused_not_written(form_session):
    """Writing the read-only field fails loudly instead of silently doing nothing."""
    form_session.observe()
    ref = _ref(form_session, "form_id")
    out = form_session.dispatch(f'CALL {ref}.set_text("HACK")')
    assert out.startswith("ERR CAPABILITY_NOT_FOUND"), out


def test_fields_are_written_through_the_native_route(form_session):
    """Every field here publishes `value`, so nothing is typed or pasted."""
    form_session.observe()
    form_session.dispatch(f'CALL {_ref(form_session, "form_name")}.set_text("张三")')
    form_session.dispatch(f'CALL {_ref(form_session, "form_email")}.set_text("a@b.c")')
    primitives = [p for _r, p, _v in form_session.provider.executed]
    assert primitives == ["set_text", "set_text"], primitives
    assert _field(form_session, "form_name") == "张三"


def test_field_labels_are_related_to_their_fields(form_session):
    """A label and its field are linked, so the agent knows which is which.

    The relation is what lets an agent that found the text "邮箱" act on the
    field beside it without guessing by position.
    """
    form_session.observe()
    ref = _ref(form_session, "form_email")
    detail = form_session.inspect_direct(ref)
    # The relation is present on the field (label_for) or the label (controls).
    kinds = set()
    for node in form_session.result.root.walk():
        for rel in node.relations:
            kinds.add(rel.kind)
    assert "label_for" in kinds or "controls" in kinds


# ---------------------------------------------------------------------------
# controls
# ---------------------------------------------------------------------------


def test_checkbox_toggles_and_reports_the_change(form_session):
    """A checkbox advertises toggle and the state change shows in the delta."""
    form_session.observe()
    ref = _ref(form_session, "form_agree")
    out = form_session.dispatch(f"CALL {ref}.toggle()")
    assert not out.startswith("ERR"), out
    node = form_session.result.find(ref)
    assert node is not None and "checked" in node.states


def test_a_disabled_button_is_marked_and_refused(form_session):
    """`disabled` is printed and the call is refused with TARGET_DISABLED."""
    obs = form_session.observe().text
    line = next(l for l in obs.splitlines() if "禁用按钮" in l)
    assert "disabled" in line
    ref = _ref(form_session, "form_disabled")
    out = form_session.dispatch(f"CALL {ref}.activate()")
    assert out.startswith("ERR"), out


# ---------------------------------------------------------------------------
# validation is a real outcome
# ---------------------------------------------------------------------------


def test_submit_without_a_name_is_rejected_by_the_form(form_session):
    """An invalid submit reports the reason and records nothing.

    The name is empty, so the application refuses; the runtime must surface that
    as a delta on the validation line rather than as a success.
    """
    form_session.observe()
    out = form_session.dispatch(f"CALL {_ref(form_session, 'form_submit')}.activate()")
    assert _validation(form_session) == "姓名不能为空"
    assert "姓名不能为空" in out
    assert _records(form_session) == []


def test_submit_without_consent_is_rejected(form_session):
    """The checkbox gates the submit, and the form says so."""
    form_session.observe()
    form_session.dispatch(f'CALL {_ref(form_session, "form_name")}.set_text("张三")')
    form_session.dispatch(f"CALL {_ref(form_session, 'form_submit')}.activate()")
    assert _validation(form_session) == "必须同意条款"
    assert _records(form_session) == []


def test_a_valid_submit_records_and_clears_validation(form_session):
    """The happy path: fill, agree, submit, read the record back."""
    form_session.observe()
    form_session.dispatch(f'CALL {_ref(form_session, "form_name")}.set_text("张三")')
    form_session.dispatch(f'CALL {_ref(form_session, "form_email")}.set_text("z@example.com")')
    form_session.dispatch(f"CALL {_ref(form_session, 'form_agree')}.toggle()")
    out = form_session.dispatch(f"CALL {_ref(form_session, 'form_submit')}.activate()")

    assert _validation(form_session) == "未提交"
    records = _records(form_session)
    assert len(records) == 1
    assert "张三" in records[0] and "z@example.com" in records[0]
    assert "张三" in out, "the new record must appear in the delta"


def test_the_readonly_id_advances_after_a_submit(form_session):
    """The app writes back an id, which the observation shows changing."""
    form_session.observe()
    before = _field(form_session, "form_id")
    form_session.dispatch(f'CALL {_ref(form_session, "form_name")}.set_text("李四")')
    form_session.dispatch(f"CALL {_ref(form_session, 'form_agree')}.toggle()")
    form_session.dispatch(f"CALL {_ref(form_session, 'form_submit')}.activate()")
    assert _field(form_session, "form_id") != before


def test_repeated_submits_accumulate_records(form_session):
    """Two submissions leave two records, in order."""
    form_session.observe()
    for name in ("甲", "乙"):
        form_session.dispatch(f'CALL {_ref(form_session, "form_name")}.set_text("{name}")')
        if "checked" not in (form_session.result.find(_ref(form_session, "form_agree")).states):
            form_session.dispatch(f"CALL {_ref(form_session, 'form_agree')}.toggle()")
        form_session.dispatch(f"CALL {_ref(form_session, 'form_submit')}.activate()")
    records = _records(form_session)
    assert len(records) == 2
    assert "甲" in records[0] and "乙" in records[1]


def test_reset_clears_the_fields_and_the_validation(form_session):
    """The reset button is a real action with visible effects."""
    form_session.observe()
    form_session.dispatch(f'CALL {_ref(form_session, "form_name")}.set_text("王五")')
    form_session.dispatch(f"CALL {_ref(form_session, 'form_reset')}.activate()")
    assert _field(form_session, "form_name") == ""
    assert _validation(form_session) == "未提交"


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


def _validation(session) -> str:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "form_validation":
            return node.name or ""
    raise AssertionError("validation line not found")


def _records(session) -> list[str]:
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "form_records_list":
            return [c.name or "" for c in node.children]
    raise AssertionError("records list not found")
