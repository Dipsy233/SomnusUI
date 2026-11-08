"""Control-flow benchmark: one round trip vs the best linear strategy.

Two tasks on the mock provider compare what an agent pays with and without
run-script control flow (EACH / TRY / IF):

* **Task A - add every product to the cart** (30 products): the linear agent
  must pay a full observation to learn the refs and then two batches (the
  20-statement batch cap); the control-flow agent keeps the cheap folded L0
  and runs one EACH loop.
* **Task B - delete, and confirm if a dialog appears**: the linear agent runs
  delete-then-confirm and, in the world where no dialog shows, spends a
  second call recovering from the failed AWAIT; the control-flow agent covers
  both world lines in one TRY/IF run.

Metrics per strategy: tool calls, total reply tokens (the text the model
would read) and the verified end state. Deterministic; no gate reads wall
time. Run from the repository root:

    python -m bench.run_control_flow
"""

from __future__ import annotations

from dataclasses import dataclass

from somnus.projection.budget import count_tokens
from somnus.providers.mock import MockProvider
from somnus.runtime.session import Session
from somnus.sui.parse import parse_script
from tests.fixtures import scenarios as S
from tests.fixtures.builders import demo_scripts, restaurant_demo_tree

PRODUCTS = 30


@dataclass
class StrategyResult:
    calls: int
    reply_tokens: int
    note: str


# --------------------------------------------------------------------- task A


def _restaurant_session() -> Session:
    catalog = [(f"Item {i:02d}", 1.0 + i) for i in range(PRODUCTS)]
    return Session(
        MockProvider(
            fixtures=[restaurant_demo_tree(catalog=catalog)],
            scripts=demo_scripts(),
        )
    )


def _add_refs(session: Session) -> list[str]:
    assert session.result is not None
    return [n.ref for n in session.result.root.walk() if "add" in n.cap_names()]


def task_a_linear() -> StrategyResult:
    """Best linear strategy: full observe, then batches under the 20 cap."""
    session = _restaurant_session()
    obs = session.observe(full=True)
    calls, tokens = 1, count_tokens(obs.text)
    refs = _add_refs(session)
    for chunk in (refs[:20], refs[20:]):
        script = "\n".join(f"CALL {ref}.add()" for ref in chunk)
        out = session.run_script(parse_script(script))
        assert out.startswith("RUN OK"), out
        calls += 1
        tokens += count_tokens(out)
    return StrategyResult(calls, tokens, "full observe + 2 batches")


def task_a_control_flow() -> StrategyResult:
    session = _restaurant_session()
    obs = session.observe()
    calls, tokens = 1, count_tokens(obs.text)
    out = session.run_script(
        parse_script(f'EACH BUTTON "Add" max={PRODUCTS}\nCALL $it.activate()\nEND')
    )
    assert out.startswith("RUN OK"), out
    assert f"count 0→{PRODUCTS}" in out, out
    calls += 1
    tokens += count_tokens(out)
    return StrategyResult(calls, tokens, "folded L0 + one EACH run")


# --------------------------------------------------------------------- task B


def _dialog_session(opens_dialog: bool) -> Session:
    scripts = S.scenario_scripts()
    if not opens_dialog:
        # The other world line: Delete just works, no confirmation appears.
        scripts = dict(scripts)
        scripts["删除:activate"] = []
    return Session(
        MockProvider(
            fixtures=[S.dialog_tree()], scripts=scripts, callbacks=S.callbacks()
        )
    )


def _delete_ref(session: Session) -> str:
    assert session.result is not None
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == "dialog_delete":
            return node.ref
    raise AssertionError("no delete button")


def task_b_linear(opens_dialog: bool) -> StrategyResult:
    session = _dialog_session(opens_dialog)
    obs = session.observe()
    calls, tokens = 1, count_tokens(obs.text)
    ref = _delete_ref(session)
    out = session.run_script(
        parse_script(f'CALL {ref}.activate()\nAWAIT BUTTON "确定" timeout=0\nCALL $it.activate()')
    )
    calls += 1
    tokens += count_tokens(out)
    note = "one run"
    if not opens_dialog:
        assert out.startswith("RUN PARTIAL"), out
        # The failed AWAIT tells the agent nothing about whether the delete
        # landed; recovering costs a second look.
        obs = session.observe()
        calls += 1
        tokens += count_tokens(obs.text)
        note = "one PARTIAL run + recovery observe"
    return StrategyResult(calls, tokens, note)


def task_b_control_flow(opens_dialog: bool) -> StrategyResult:
    session = _dialog_session(opens_dialog)
    obs = session.observe()
    calls, tokens = 1, count_tokens(obs.text)
    ref = _delete_ref(session)
    out = session.run_script(
        parse_script(
            f"CALL {ref}.activate()\n"
            'TRY AWAIT BUTTON "确定" timeout=0\n'
            "IF ok CALL $it.activate()"
        )
    )
    assert out.startswith("RUN OK"), out
    state = "ok ($it=" if opens_dialog else "skipped"
    assert state in out, out
    calls += 1
    tokens += count_tokens(out)
    return StrategyResult(calls, tokens, "one TRY/IF run, both world lines")


# --------------------------------------------------------------------- report


def _row(task: str, strategy: str, r: StrategyResult) -> str:
    return f"{task:<28} {strategy:<14} calls={r.calls}  reply_tokens={r.reply_tokens:<5} ({r.note})"


def main() -> None:
    a_lin, a_cf = task_a_linear(), task_a_control_flow()
    print(_row("A: add 30 products", "linear", a_lin))
    print(_row("A: add 30 products", "control-flow", a_cf))
    print(
        f"   -> control flow saves {a_lin.calls - a_cf.calls} call(s), "
        f"{a_lin.reply_tokens - a_cf.reply_tokens} reply tokens"
    )
    for world, opens in (("dialog appears", True), ("no dialog", False)):
        b_lin, b_cf = task_b_linear(opens), task_b_control_flow(opens)
        print(_row(f"B: delete, {world}", "linear", b_lin))
        print(_row(f"B: delete, {world}", "control-flow", b_cf))
        print(
            f"   -> control flow saves {b_lin.calls - b_cf.calls} call(s), "
            f"{b_lin.reply_tokens - b_cf.reply_tokens} reply tokens"
        )


if __name__ == "__main__":
    main()
