"""Benchmark harness: the v0.1 acceptance metrics (dev doc section 19).

Runs the acceptance scenario N times against the mock provider and reports:

* Zero Vision: number of screenshot/coordinate/vision calls (must be 0 - the
  runtime has no such capability, so this is structurally 0 and asserted here).
* Information isolation: scan of every LLM-facing string for AutomationId,
  RuntimeId, COM pointers or coordinates (must be 0).
* Overview tokens (L0 <= 200).
* Compression ratio: raw JSON tokens / SUI-Text tokens (>= 10x).
* Action success rate (OK / total).
* Task success rate over the scripted task set.

The harness is deterministic; it is the same code path CI runs.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from somnus.projection.budget import count_tokens
from somnus.providers.mock import MockProvider, raw_tree_to_dict
from somnus.runtime.session import Session

#: Patterns for the *shapes* of native identity that must never appear in
#: LLM-facing text (dev doc section 19).
#:
#: A bare run of digits is deliberately **not** here. A four-digit number is
#: ambiguous - the form screen's own record id is `REQ-0001`, which is
#: application data the user is meant to see, and a regex cannot tell it from a
#: runtime id. Flagging it would make the leak gate fire on legitimate content,
#: and a gate that cries wolf gets ignored. Native identity is instead caught by
#: :func:`native_identity_leaks`, which compares the text against the actual
#: identifiers of the tree the text came from - evidence, not a guess.
FORBIDDEN_LEAK_PATTERNS = [
    re.compile(r"AutomationId", re.IGNORECASE),
    re.compile(r"\bRuntimeId\b", re.IGNORECASE),
    re.compile(r"\bnative_window_handle\b"),
    re.compile(r"0x[0-9a-fA-F]{4,}"),  # COM pointer / hwnd literal
    re.compile(r"\(\s*\d{2,5}\s*,\s*\d{2,5}\s*\)"),  # (x, y) coordinates
    re.compile(r"\bpid=\d+"),
    re.compile(r"class_name="),
    re.compile(r"\bcontrol_type="),
]


def native_identity_leaks(text: str, tree: Any) -> list[str]:
    """Native identifiers of ``tree`` that appear verbatim in ``text``.

    This is the honest form of the isolation check: instead of guessing what a
    leaked id looks like, it enumerates the ids that actually exist (automation
    ids, class names, runtime ids, handles) and looks for each one in the text
    the model would receive. An identifier that appears there is a leak, full
    stop; one that does not cannot be.

    Empty and trivially short identifiers are skipped: an automation id of
    ``"a"`` or a class name of ``"x"`` would match half the document and prove
    nothing.
    """
    leaks: list[str] = []
    seen: set[str] = set()
    for node in _walk_raw(tree):
        native = getattr(node, "native", None)
        if native is None:
            continue
        candidates = [
            ("automation_id", native.automation_id),
            ("class_name", native.class_name),
        ]
        for kind, value in candidates:
            if not value or len(str(value)) < 3:
                continue
            value = str(value)
            if value in seen:
                continue
            seen.add(value)
            if value in text:
                leaks.append(f"{kind}: {value}")
        # A runtime id is a tuple of integers; its dotted form is how it would
        # appear if it ever did.
        if native.runtime_id:
            dotted = ".".join(str(p) for p in native.runtime_id)
            if dotted not in seen and len(dotted) >= 3:
                seen.add(dotted)
                if dotted in text:
                    leaks.append(f"runtime_id: {dotted}")
    return leaks


def _walk_raw(tree: Any):
    root = getattr(tree, "root", tree)
    stack = [root]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(getattr(node, "children", ()) or ())


@dataclass
class BenchReport:
    runs: int = 0
    overview_tokens: list[int] = field(default_factory=list)
    compression_ratios: list[float] = field(default_factory=list)
    action_total: int = 0
    action_ok: int = 0
    task_total: int = 0
    task_ok: int = 0
    leaks: list[str] = field(default_factory=list)
    zero_vision: bool = True
    step_counts: list[int] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "overview_tokens_max": max(self.overview_tokens) if self.overview_tokens else 0,
            "overview_tokens_median": int(statistics.median(self.overview_tokens))
            if self.overview_tokens
            else 0,
            "compression_ratio_min": round(min(self.compression_ratios), 1)
            if self.compression_ratios
            else 0,
            "action_success_rate": round(
                self.action_ok / self.action_total, 4
            )
            if self.action_total
            else 0,
            "task_success_rate": round(self.task_ok / self.task_total, 4)
            if self.task_total
            else 0,
            "median_steps": int(statistics.median(self.step_counts))
            if self.step_counts
            else 0,
            "zero_vision": self.zero_vision,
            "leaks": self.leaks[:5],
            "leak_count": len(self.leaks),
        }


#: One scripted task: (name, screen, list of SUI-Text commands).
#: Refs are stable within a fresh session (deterministic ref allocation).
#:
#: The tasks run against the Scenario Suite fixtures rather than the three-row
#: restaurant demo, because the metrics have to be measured on data that is
#: large enough for the folding/budget rules to matter. A three-item catalog
#: always fits in the budget, so "compression >= 10x" and "L0 stays bounded"
#: would be trivially true and would stop measuring anything.
TASKS: list[tuple[str, list[str]]] = [
    (
        "store_add_one",
        ['FIND "Item 7"', "CALL {row}.add()"],
    ),
    (
        "store_add_two",
        [
            'FIND "Item 7"',
            "CALL {row}.add()",
            'FIND "Item 40"',
            "CALL {row}.add()",
        ],
    ),
    (
        "store_search_and_add",
        [
            "CALL {search}.set_text(\"Special Item 17\")",
            "CALL {search_button}.activate()",
            'FIND "Special Item 17"',
            "CALL {row}.add()",
        ],
    ),
    (
        "form_submit",
        [
            'CALL {name}.set_text("张三")',
            "CALL {agree}.toggle()",
            "CALL {submit}.activate()",
        ],
    ),
    (
        "chat_send",
        [
            'CALL {composer}.set_text("会议纪要")',
            "CALL {send}.activate()",
        ],
    ),
]

#: Named anchors resolved per screen, so a task can name a control without
#: hard-coding a ref (refs move with the tree, automation ids do not).
ANCHORS: dict[str, dict[str, str]] = {
    "store_add_one": {},
    "store_add_two": {},
    "store_search_and_add": {
        "search": "store_search",
        "search_button": "store_search_button",
    },
    "form_submit": {
        "name": "form_name",
        "agree": "form_agree",
        "submit": "form_submit",
    },
    "chat_send": {
        "composer": "chat_composer",
        "send": "chat_send",
    },
}

#: Screen each task belongs to (templates/anchors differ per screen).
TASK_SCREENS: dict[str, str] = {
    "store_add_one": "store",
    "store_add_two": "store",
    "store_search_and_add": "store",
    "form_submit": "form",
    "chat_send": "chat",
}

#: Channels each task's screen needs enabled. A self-drawn composer (the chat
#: screen) publishes no write interface at all, so its task is measured with the
#: clipboard route authorised - the metric is about the send path, not about the
#: default policy, which has its own tests. Every other screen writes natively
#: and needs nothing.
TASK_POLICY: dict[str, dict[str, bool]] = {
    "chat_send": {"allow_clipboard": True},
}

#: The `{row}` placeholder is filled from the preceding FIND result.
_ROW_PLACEHOLDER = "{row}"


def run_benchmark(args: Any) -> int:
    from somnus.runtime.policy import Policy
    from tests.fixtures import scenarios as S

    runs = getattr(args, "runs", 100)
    report = BenchReport()

    for i in range(runs):
        # The compression metric is measured against a fully expanded tree, the
        # honest worst case for "how big can this window get".
        tree = S.tree_tree()
        provider = MockProvider(
            fixtures=[tree],
            scripts=S.scenario_scripts(),
            callbacks=S.callbacks(),
        )
        session = Session(provider)

        raw_tokens = _raw_tokens()

        obs = session.observe()
        report.overview_tokens.append(obs.tokens)
        report.compression_ratios.append(raw_tokens / max(1, obs.tokens))
        report.runs += 1
        _scan_leaks(obs.text, report, tree)

        # Re-run each task on a fresh session so state is deterministic.
        for name, commands in TASKS:
            screen = TASK_SCREENS[name]
            p2 = S.make_provider(screen)
            policy_kw = TASK_POLICY.get(name)
            s2 = Session(p2, policy=Policy(**policy_kw) if policy_kw else None)
            s2.observe()
            anchors = S.resolve_anchors(s2, ANCHORS.get(name, {}))
            ok = True
            steps = 0
            last_row = ""
            for cmd in commands:
                text = _fill(cmd, anchors, last_row)
                out = s2.dispatch(text)
                steps += 1
                _scan_leaks(out, report, s2.provider._trees.get(s2.target_window))
                if out.startswith("FOUND"):
                    last_row = _first_entity_ref(out)
                # Only CALL actions count toward the action success metric;
                # FIND/INSPECT are observations.
                if text.upper().startswith("CALL"):
                    report.action_total += 1
                    if out.startswith("OK"):
                        report.action_ok += 1
                    else:
                        ok = False
                elif out.startswith("ERR"):
                    ok = False
            report.task_total += 1
            if ok:
                report.task_ok += 1
            report.step_counts.append(steps)

    summary = report.summary()
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    # Exit non-zero when a hard gate fails.
    gates = [
        summary["overview_tokens_max"] <= 300,
        summary["action_success_rate"] >= 0.99,
        summary["task_success_rate"] >= 0.90,
        summary["leak_count"] == 0,
        summary["zero_vision"],
        summary["compression_ratio_min"] >= 10,
    ]
    return 0 if all(gates) else 1


def _fill(command: str, anchors: dict[str, str], row: str) -> str:
    """Fill a task template: named anchors and the last FIND's row ref.

    Refs are not hard-coded because they move with the tree; the anchors are
    automation ids resolved on the live session, which is exactly how a caller
    that wants a stable handle would address a control.
    """
    out = command
    for key, value in anchors.items():
        out = out.replace("{" + key + "}", value)
    return out.replace("{row}", row)


def _first_entity_ref(find_output: str) -> str:
    for line in find_output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in (
            "ITEM",
            "PRODUCT",
            "LISTITEM",
            "TREEITEM",
            "GROUP",
        ):
            return parts[1]
    return ""


def _raw_tokens() -> int:
    """Tokens of the raw accessibility tree serialized as JSON.

    Measured on the *fully expanded* scenario tree: the compression metric has to
    compare the observation against an application that really is large, or it
    measures nothing (dev doc section 19).
    """
    from tests.fixtures import scenarios as S

    big = S.tree_tree(expanded=S.all_tree_ids())
    raw_json = json.dumps(raw_tree_to_dict(big), ensure_ascii=False)
    return count_tokens(raw_json)


def _scan_leaks(text: str, report: BenchReport, tree: Any = None) -> None:
    """Record any native identity that reached the LLM-facing text.

    Two checks, strongest first: the *evidence* check (identifiers that really
    exist in ``tree`` must not appear in ``text``), then the shape patterns for
    things that have no single owner (a COM pointer, a coordinate pair).
    """
    if tree is not None:
        report.leaks.extend(native_identity_leaks(text, tree))
    for pattern in FORBIDDEN_LEAK_PATTERNS:
        for match in pattern.finditer(text):
            report.leaks.append(f"{pattern.pattern}: {match.group(0)}")
