"""Topic documents, loaded on demand.

The full protocol is split between a short core prompt (``system_prompt.md``)
and the pages in ``protocol/agent/``. Only the core prompt is paid for every
turn; ``HELP <topic>`` is how a model - or a person at the CLI - asks for one
page instead of carrying all of them in context.

``HELP`` without a topic is the discovery entry point: it lists the tiers in
force, the tools they expose, the tiers that can be enabled, and the topics.
Nothing here executes a GUI action, so the reply is plain text.
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
from typing import Iterable

from somnus.protocol import policy

AGENT_DIR = Path(__file__).with_name("agent")

#: Topic -> one-line description. The file is ``agent/<topic>.md``.
TOPICS: dict[str, str] = {
    "format": "full SUI-Text: flags, folding, values, limits",
    "gestures": "implicit capabilities; select vs activate vs context_menu",
    "batching": "batch calls and run/AWAIT scripts",
    "desktops": "main/child, switch, session lifecycle, ref invalidation",
    "errors": "reading a reply; error codes and recovery",
    "settings": "settle timing and configure()",
}


def _version() -> str:
    try:
        return metadata.version("somnusui")
    except Exception:  # pragma: no cover - source checkout without metadata
        return "dev"


def topic_text(topic: str) -> str | None:
    """The document for *topic*, or ``None`` when there is no such page."""
    key = topic.strip().lower()
    if key not in TOPICS:
        return None
    path = AGENT_DIR / f"{key}.md"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def render_help(
    topic: str | None = None,
    capabilities: Iterable[str] | None = None,
) -> str:
    """``HELP`` (discovery) or ``HELP <topic>`` (one page)."""
    caps = policy.active_capabilities(capabilities)
    if topic:
        text = topic_text(topic)
        if text is not None:
            return text
        return (
            f'HELP unknown topic "{topic}"; topics: '
            + ", ".join(TOPICS)
            + " (HELP lists them)"
        )

    lines = [f"HELP {_version()}"]
    enabled = [tier for tier in policy.CAPABILITY_ORDER if tier in caps]
    lines.append("enabled: " + " ".join(enabled))
    lines.append("tools: " + " ".join(policy.tool_names(caps)))
    for tier in policy.CAPABILITY_ORDER:
        if tier in caps:
            continue
        names = policy.tier_tools(tier)
        if names:
            lines.append(
                f"available: {tier} -> {' '.join(names)} "
                f"(SOMNUS_CAPABILITIES={tier})"
            )
    lines.append("topics:")
    width = max(len(name) for name in TOPICS)
    for name, summary in TOPICS.items():
        lines.append(f"  {name:<{width}}  {summary}")
    lines.append("HELP <topic> prints one; more tiers: SOMNUS_CAPABILITIES=<a,b|all>")
    return "\n".join(lines)
