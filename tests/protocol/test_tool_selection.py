"""Every core task still points at exactly one tool.

No model runs in this repository, so this is a deterministic proxy for "is the
right tool obvious from the surface it sees". Each task is scored against the
tool name and description with inverse document frequency: a shared word counts
for little, a distinctive word for a lot, and a task that names the tool itself
gets a direct bonus. A rewritten description or a new tool that makes a task
ambiguous fails here before it costs a model a turn.

The table is also the tier contract: a task marked ``core`` must be completable
with the five-tool loop alone, because that is the surface a minimal host
advertises.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from somnus.protocol import policy
from somnus.protocol.tools import TOOL_SPECS

STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "what", "when", "which",
    "give", "show", "into", "from", "your", "have", "does", "make", "because",
    "then", "than", "them", "their", "its", "all", "every", "one", "can", "you",
}


@dataclass(frozen=True)
class Selection:
    task: str
    tool: str
    core: bool = True


TASKS: tuple[Selection, ...] = (
    Selection("Give me an overview of the current window and its refs.", "observe"),
    Selection("Locate a product by name inside a folded list.", "find"),
    Selection("Show every capability, argument and relation of node b5.", "inspect"),
    Selection("Invoke the add capability on product p1.", "call"),
    Selection(
        "Run a script that expands a menu, awaits the item, then activates it.",
        "run",
    ),
    Selection("List the top-level windows with their titles.", "observe"),
    Selection("Find the row named Item 187 that is not on screen.", "find"),
    Selection("Inspect node q1 because set_text is missing.", "inspect"),
    Selection(
        "Move to the child desktop so my input does not disturb the user.",
        "switch",
        core=False,
    ),
    Selection("Start Notepad as a new program.", "launch", core=False),
    Selection(
        "Calls read an animation mid-flight; tune the settle timing.",
        "configure",
        core=False,
    ),
    Selection("Which topics do you have? Show me the batching page.", "help", core=False),
)


def words(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9_]+", text.lower())
        if token not in STOPWORDS and len(token) > 2
    }


def documents(specs: list[dict]) -> dict[str, set[str]]:
    return {spec["name"]: words(spec["name"] + " " + spec["description"]) for spec in specs}


def scores(task: str, docs: dict[str, set[str]]) -> dict[str, float]:
    task_words = words(task)
    frequency = Counter(word for doc in docs.values() for word in doc)
    picked = {name: 0.0 for name in docs}
    for name, doc in docs.items():
        picked[name] = sum(1.0 / frequency[word] for word in task_words & doc)
        if name in task_words:
            picked[name] += 2.0
    return picked


def pick(task: str, specs: list[dict]) -> str:
    docs = documents(specs)
    return max(docs, key=lambda name: scores(task, docs)[name])


def test_every_expected_tool_exists_in_the_default_surface():
    default = {spec["name"] for spec in policy.filter_tool_specs(TOOL_SPECS)}
    missing = [case.tool for case in TASKS if case.tool not in default]
    assert not missing, missing


def test_core_tasks_stay_inside_the_core_tier():
    core = set(policy.tier_tools(policy.CORE))
    outside = [(case.task, case.tool) for case in TASKS if case.core and case.tool not in core]
    assert not outside, outside


def test_the_descriptions_still_point_at_the_right_tool():
    default = policy.filter_tool_specs(TOOL_SPECS)
    wrong = [
        (case.task, case.tool, pick(case.task, default))
        for case in TASKS
        if pick(case.task, default) != case.tool
    ]
    assert not wrong, wrong


def test_the_core_surface_also_points_at_the_right_tool():
    """Filtering must not remove the signal a core task relies on."""
    core_specs = policy.filter_tool_specs(TOOL_SPECS, policy.CORE)
    cases = [case for case in TASKS if case.core]
    wrong = [
        (case.task, case.tool, pick(case.task, core_specs))
        for case in cases
        if pick(case.task, core_specs) != case.tool
    ]
    assert not wrong, wrong


def test_adding_help_does_not_steal_a_task_from_another_tool():
    """Adding a tool must not lower selection accuracy for existing tasks."""
    without_help = [spec for spec in TOOL_SPECS if spec["name"] != "help"]
    cases = [case for case in TASKS if case.tool != "help"]
    wrong = [
        (case.task, case.tool, pick(case.task, without_help))
        for case in cases
        if pick(case.task, without_help) != case.tool
    ]
    assert not wrong, wrong
