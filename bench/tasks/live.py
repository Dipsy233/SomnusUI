"""The live confirmation layer: real ScenarioSuite.exe windows on a child desktop.

Hard rule (plan-v3 §0, kept): **everything runs in the child session; if it is
unavailable the run fails loudly — never a silent fallback to the main
desktop.** The child session is a second interactive logon, so the benchmark
windows never steal the user's foreground.

Shape of one task:

* the agent session sits on the child desktop (one child session per run,
  opened once and reused across tasks);
* the runner starts the scenario *inside* the child session via
  ``session.launch(..., window="child")`` (gated by ``child_allow_spawn``);
* the loop drives the agent session; the assertions run in a **separate
  session** with its own peer connection (the pipe allows unlimited
  instances) — the agent's refs and compile cache cannot vouch for itself;
* teardown kills only ScenarioSuite pids that appeared during the task.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from bench.tasks.runner import Task, evaluate, load_tasks

TASKS_DIR = Path(__file__).parent

_ROOT = Path(__file__).resolve().parents[2]
_APP_CANDIDATES = (
    _ROOT / "apps/scenario-suite/bin/Debug/net8.0-windows/ScenarioSuite.exe",
    _ROOT / "apps/scenario-suite/bin/Release/net8.0-windows/ScenarioSuite.exe",
)

_WINDOW_TIMEOUT = 20.0
_PROCESS = "ScenarioSuite.exe"


class InfrastructureError(RuntimeError):
    """The child desktop cannot carry the run; never fall back silently."""


def find_app() -> Path:
    for path in _APP_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError(
        "ScenarioSuite.exe not built; run: dotnet build apps/scenario-suite"
    )


def _scenario_pids() -> set[int]:
    """Pids of running ScenarioSuite processes, all logon sessions (tasklist)."""
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {_PROCESS}", "/FO", "CSV", "/NH"],
        capture_output=True,
    ).stdout.decode(errors="replace")
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def _kill(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)


class LiveEnv:
    """One child session shared by every live task of a run.

    The peer serves **one client connection at a time**, so the agent session
    and the assertion session cannot be connected simultaneously: the agent's
    connection is released before the prober connects, and the next task
    reconnects (``switch_session("child")`` attaches to the session that is
    still open).
    """

    def __init__(self) -> None:
        self.agent = None
        self.session_id: int | None = None

    def open(self) -> None:
        from somnus.providers.base import get_provider
        from somnus.runtime.session import Session
        from somnus.session import childsession

        agent = Session(get_provider("uia"), allow_packs=False)
        reply = agent.open_child_session()
        if reply.startswith("ERR"):
            agent.close()
            raise InfrastructureError(
                "child session did not open; live layer refuses to fall back "
                f"to the main desktop. Peer reply:\n{reply}"
            )
        live = childsession.live_child_session()
        if live is None:
            agent.close()
            raise InfrastructureError(
                "open_child_session reported success but no live child session "
                "exists; refusing to continue on the main desktop"
            )
        self.agent = agent
        self.session_id = live.session_id

    def reconnect_agent(self) -> None:
        """A fresh agent session on the still-open child desktop."""
        from somnus.providers.base import get_provider
        from somnus.runtime.session import Session

        self.close_agent()
        agent = Session(get_provider("uia"), allow_packs=False)
        reply = agent.switch_session("child")
        if reply.startswith("ERR"):
            agent.close()
            raise InfrastructureError(
                f"could not reattach to child session {self.session_id}:\n{reply}"
            )
        self.agent = agent

    def close_agent(self) -> None:
        if self.agent is not None:
            try:
                self.agent.close()
            finally:
                self.agent = None

    def close(self) -> None:
        self.close_agent()
        try:
            from somnus.providers.base import get_provider
            from somnus.runtime.session import Session

            Session(get_provider("uia"), allow_packs=False).close_child_session()
        except Exception:  # noqa: BLE001 - teardown
            pass
        self.session_id = None


def run_live_task(
    task: Task,
    surface,
    dispatch_fn,
    client,
    env: LiveEnv,
    *,
    app: Path | None = None,
    max_steps: int = 12,
):
    """Run one task on the child desktop; assertions via an independent session."""
    from bench.loop.agent import run_loop
    from somnus.session.peerprovider import make_peer_provider
    from somnus.runtime.session import Session

    app = app or find_app()
    instruction = task.instruction.replace("{app}", str(app))
    pids_before = _scenario_pids()
    try:
        # Start the scenario *inside* the child session (the peer's token),
        # so it appears on the desktop the agent is driving.
        reply = env.agent.launch(str(app), args=[task.screen], window="child")
        if reply.startswith("ERR"):
            raise InfrastructureError(
                f"launch on the child desktop failed:\n{reply}"
            )
        _wait_for_window(env.agent, task.screen)

        result = run_loop(
            env.agent,
            task.id,
            instruction,
            scheme_id=surface.scheme_id,
            tools=list(surface.openai_tools),
            system_prompt=surface.system_prompt,
            dispatch_fn=dispatch_fn,
            client=client,
            max_steps=max_steps,
        )
        # The peer serves one client at a time: release the agent's connection
        # so the prober's independent session can connect.
        env.close_agent()
        ok, assertion_results = _assert_independently(env.session_id, task)
        result.success = ok
        result.transcript.append(
            {
                "assertions": [
                    {
                        "aid": a.get("aid") or a.get("window_title_contains"),
                        "passed": r.passed,
                        "detail": r.detail,
                    }
                    for a, r in zip(task.assertions, assertion_results)
                ]
            }
        )
        return result
    finally:
        # Kill only what appeared during the task — the runner's scenario plus
        # anything the agent itself launched — and nothing that was running.
        for pid in _scenario_pids() - pids_before:
            _kill(pid)
        # The next task needs an agent session again.
        env.reconnect_agent()


def _wait_for_window(session, screen: str) -> None:
    """The scenario window must appear on the child desktop before the loop."""
    deadline = time.monotonic() + _WINDOW_TIMEOUT
    while time.monotonic() < deadline:
        session.refresh_windows()
        if any(
            f"Scenario Suite - {screen}" in (w.title or "") for w in session.windows
        ):
            return
        time.sleep(0.4)
    raise InfrastructureError(f"scenario window {screen!r} did not appear")


def _assert_independently(session_id: int, task: Task) -> tuple[bool, list[Any]]:
    """A fresh session with its own peer connection reads the finished state."""
    from somnus.runtime.session import Session
    from somnus.session.peerprovider import make_peer_provider

    prober = Session(make_peer_provider(session_id=session_id), allow_packs=False)
    try:
        prober.refresh_windows()
        target = next(
            (
                w
                for w in prober.windows
                if f"Scenario Suite - {task.screen}" in (w.title or "")
            ),
            None,
        )
        if target is not None:
            # Full read: the default viewport-bounded observe folds small
            # status texts out of the frame, which would make the prober miss
            # nodes the app really shows.
            prober.dispatch(f"OBSERVE {target.window_id} full")
        return evaluate(prober, task)
    finally:
        prober.close()


def load_live_tasks() -> list[Task]:
    return load_tasks(TASKS_DIR / "tasks-live-v1.json")
