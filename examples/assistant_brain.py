"""The brain of the Somnus voice assistant.

The Portal APK owns the *system* parts of being an assistant: it registers
as the device's digital assistant (long-press home / power-menu wake), shows
the listening card, captures the utterance, and speaks the reply. The brain
owns everything that needs thinking:

    poll (block on AssistantBus) -> run the LLM agent loop over Somnus's
    tools (observe / find / call / switch) -> post the final answer back.

The brain runs wherever Python runs: a PC over adb (development) or Termux
on the same phone (self-contained assistant - the Portal listens on the
phone's loopback, so no adb and no network setup are needed).

Usage::

    # on a PC (adb transport, as always)
    python examples/assistant_brain.py --base-url http://... --model qwen...

    # on the phone itself (Termux; Portal loopback, no adb)
    python examples/assistant_brain.py --on-phone --base-url ... --model ...

Only the OpenAI-compatible chat API is required (tool calling); any vendor
endpoint works.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from somnus.protocol.tools import call_tool  # noqa: E402
from somnus.runtime.policy import Policy  # noqa: E402
from somnus.runtime.session import Session  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent import LlmClient, SYSTEM_PROMPT, TOOL_RULES, openai_tools  # noqa: E402

#: Ask for the final answer as speakable text: this is read aloud by the
#: phone, not rendered in a chat pane.
VOICE_RULES = """

You are the phone's voice assistant. The user SPOKE the task and hears your
final answer read aloud. Drive the GUI with the tools, then reply with ONE
short spoken sentence in Chinese (what you did or what you found). No
markdown, no lists, no ref names in the reply.
"""


def build_session(on_phone: bool, args: argparse.Namespace) -> Session:
    from somnus.providers.base import get_provider

    kwargs: dict[str, Any] = {"allow_list": []}
    if on_phone:
        # Same phone: the Portal's loopback listener is reachable directly.
        # adb does not exist here, and the recovery callback (which shells
        # out to adb) must not be wired.
        from somnus.providers.android.provider import AndroidProvider
        from somnus.providers.android.transport import PortalTransport, DEFAULT_REMOTE_PORT

        provider = AndroidProvider(
            transport=PortalTransport(
                skip_adb=True,
                local_port=DEFAULT_REMOTE_PORT,
                timeout=15.0,
            ),
        )
        return Session(
            provider, policy=Policy(confirmed=bool(args.confirmed))
        )
    provider = get_provider("android", **kwargs)
    return Session(provider, policy=Policy(confirmed=bool(args.confirmed)))


def assistant_loop(session: Session, client: LlmClient, *, max_steps: int, verbose: bool) -> None:
    """poll -> think -> act -> reply, forever."""
    transport = session.provider.transport
    while True:
        result = transport.call("assistant.poll")
        request = result.get("request")
        if not request:
            time.sleep(0.5)
            continue

        task = request["text"]
        if verbose:
            print(f"\n🎤 唤醒: {task!r}")

        # A fresh session object per utterance keeps refs and route memory
        # from one conversation from leaking into the next - the phone may
        # be anywhere by the time the next wake comes.
        answer = run_voice_task(session, client, task, max_steps=max_steps, verbose=verbose)

        transport.call("assistant.reply", {"id": request["id"], "text": answer})
        if verbose:
            print(f"🔊 回复: {answer}")


def run_voice_task(session, client, task, *, max_steps, verbose):
    # Imported here so --help stays dependency-light.
    from agent import run_agent

    return run_agent(
        session,
        task,
        client=client,
        max_steps=max_steps,
        verbose=verbose,
        system_prompt=SYSTEM_PROMPT.replace(TOOL_RULES, "") + TOOL_RULES + VOICE_RULES,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Somnus voice-assistant brain")
    parser.add_argument(
        "--on-phone",
        action="store_true",
        help="brain runs on the phone (Termux): connect Portal loopback, no adb",
    )
    parser.add_argument("--base-url", default="https://api.deepseek.com/v1")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--api-key", default=None)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--confirmed", action="store_true", help="allow critical caps")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    client = LlmClient(args.base_url, args.model, api_key=args.api_key)

    session = build_session(args.on_phone, args)
    try:
        if not args.quiet:
            print("Somnus 助手大脑已就绪 (等待唤醒…)")
        assistant_loop(session, client, max_steps=args.max_steps, verbose=not args.quiet)
    except KeyboardInterrupt:
        return 0
    finally:
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
