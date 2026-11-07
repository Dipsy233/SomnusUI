"""Build a mock session per screen, run the loop, assert on runtime state.

The assertion side never looks at the transcript: it walks the session's final
compiled frame, finds nodes by automation_id (the stable handle, same idea as
``resolve_anchors``) and compares the application state the loop left behind.
A run succeeds only if every assertion passes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASKS_DIR = Path(__file__).parent


@dataclass(frozen=True)
class Task:
    id: str
    screen: str
    instruction: str
    assertions: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AssertionResult:
    passed: bool
    detail: str


def load_tasks(path: Path | None = None) -> list[Task]:
    path = path or TASKS_DIR / "tasks-mock-v1.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Task(
            id=t["id"],
            screen=t["screen"],
            instruction=t["instruction"],
            assertions=tuple(t["assertions"]),
        )
        for t in data["tasks"]
    ]


def build_session(screen: str):
    """A mock session for one screen, wired the same way the scenario tests are."""
    from somnus.runtime.session import Session
    from tests.fixtures.scenarios import make_provider

    return Session(make_provider(screen))


# ---------------------------------------------------------------------------
# assertions
# ---------------------------------------------------------------------------


def _node_by_automation(session, automation_id: str):
    if session.result is None:
        return None  # the model never observed anything
    for node in session.result.root.walk():
        source = session.result.sources.get(node.ref)
        if source is not None and source.native.automation_id == automation_id:
            return node
    return None


def evaluate_assertion(session, assertion: dict[str, Any]) -> AssertionResult:
    if "window_title_contains" in assertion:
        want = assertion["window_title_contains"]
        hit = any(want in (w.title or "") for w in session.windows)
        return AssertionResult(
            hit, f"window title ⊇ {want!r}" if hit else f"no window title ⊇ {want!r}"
        )
    node = _node_by_automation(session, assertion["aid"])
    if node is None:
        found = bool(assertion.get("not_exists"))
        return AssertionResult(
            found, f"aid={assertion['aid']} not in final frame"
        )

    def fail(msg: str) -> AssertionResult:
        return AssertionResult(False, f"aid={assertion['aid']} {msg}")

    if "text_equals" in assertion:
        actual = node.name or ""
        want = assertion["text_equals"]
        return (
            AssertionResult(True, f"text == {want!r}")
            if actual == want
            else fail(f"text == {actual!r}, want {want!r}")
        )
    if "text_contains" in assertion:
        actual = node.name or ""
        want = assertion["text_contains"]
        return (
            AssertionResult(True, f"text ⊇ {want!r}")
            if want in actual
            else fail(f"text == {actual!r}, want ⊇ {want!r}")
        )
    if "value_equals" in assertion:
        actual = node.value or ""
        want = assertion["value_equals"]
        return (
            AssertionResult(True, f"value == {want!r}")
            if actual == want
            else fail(f"value == {actual!r}, want {want!r}")
        )
    if "value_contains" in assertion:
        actual = node.value or ""
        want = assertion["value_contains"]
        return (
            AssertionResult(True, f"value ⊇ {want!r}")
            if want in actual
            else fail(f"value == {actual!r}, want ⊇ {want!r}")
        )
    if "children_contain" in assertion:
        names = [c.name or "" for c in node.children]
        missing = [w for w in assertion["children_contain"] if w not in names]
        return (
            AssertionResult(True, "children contain all wanted")
            if not missing
            else fail(f"children={names}, missing {missing}")
        )
    if "children_contain_sub" in assertion:
        names = [c.name or "" for c in node.children]
        missing = [
            w for w in assertion["children_contain_sub"] if not any(w in n for n in names)
        ]
        return (
            AssertionResult(True, "children contain all wanted substrings")
            if not missing
            else fail(f"children={names}, missing substring {missing}")
        )
    if "children_not_contain" in assertion:
        names = [c.name or "" for c in node.children]
        present = [w for w in assertion["children_not_contain"] if w in names]
        return (
            AssertionResult(True, "children contain none of the banned")
            if not present
            else fail(f"children contain {present}")
        )
    if "children_count" in assertion:
        want = assertion["children_count"]
        actual = len(node.children)
        return (
            AssertionResult(True, f"children == {want}")
            if actual == want
            else fail(f"children == {actual}, want {want}")
        )
    if "children_count_min" in assertion:
        want = assertion["children_count_min"]
        actual = len(node.children)
        return (
            AssertionResult(True, f"children >= {want}")
            if actual >= want
            else fail(f"children == {actual}, want >= {want}")
        )
    if "exists" in assertion:
        return (
            AssertionResult(True, "exists")
            if assertion["exists"]
            else fail("exists but must not")
        )
    raise ValueError(f"unknown assertion kinds: {sorted(assertion)}")


def evaluate(session, task: Task) -> tuple[bool, list[AssertionResult]]:
    results = [evaluate_assertion(session, a) for a in task.assertions]
    return all(r.passed for r in results), results


# ---------------------------------------------------------------------------
# one benchmark run
# ---------------------------------------------------------------------------


def run_task(
    task: Task,
    surface,
    dispatch_fn,
    client,
    *,
    max_steps: int = 12,
):
    """Run one task on one surface; returns the RunResult with success filled."""
    from bench.loop.agent import run_loop

    session = build_session(task.screen)
    try:
        tools = list(surface.openai_tools)
        if surface.full_openai_tools is not None:
            # Staged exposure: start with the initial subset; the first
            # `help` call flips the tool list to the full set for every
            # later step.
            state = {"expanded": False}

            def tools_provider():
                if state["expanded"]:
                    return list(surface.full_openai_tools)
                return list(surface.openai_tools)

            base_dispatch = dispatch_fn

            def dispatch_fn(session, name, args):  # noqa: F811 - wrapper
                result = base_dispatch(session, name, args)
                if name == "help":
                    state["expanded"] = True
                return result

            tools = tools_provider
        result = run_loop(
            session,
            task.id,
            task.instruction,
            scheme_id=surface.scheme_id,
            tools=tools,
            system_prompt=surface.system_prompt,
            dispatch_fn=dispatch_fn,
            client=client,
            max_steps=max_steps,
        )
        ok, assertion_results = evaluate(session, task)
        result.success = ok
        result.transcript.append(
            {
                "assertions": [
                    {"aid": a["aid"], "passed": r.passed, "detail": r.detail}
                    for a, r in zip(task.assertions, assertion_results)
                ]
            }
        )
        return result
    finally:
        session.close()
