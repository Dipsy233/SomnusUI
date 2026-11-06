"""Environment diagnosis for the self-experience loop.

``doctor`` answers one question before anything is driven: *can this machine run
a check, and on which desktop?* It reports the provider's Windows extras, the
settings file, the child-session prerequisites and - when one is usable - the
result of a generic observe/find/call smoke.

It is read-only with respect to the child session: it reports whether one is
live and what to run if it is not, but it never opens or closes one. Starting a
system-wide resource is a decision for the caller, and ``doctor`` should be safe
to run at any time. The smoke follows the same rule as every other self-check:
the child session by default, fixtures offline, the user's desktop only with the
explicit ``--real``.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from importlib import metadata, util

from somnus.selfcheck.checks import CheckReport

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"


@dataclass(slots=True)
class CheckLine:
    label: str
    status: str
    detail: str = ""


@dataclass(slots=True)
class Environment:
    lines: list[CheckLine] = field(default_factory=list)
    offline: bool = False
    real: bool = False
    can_smoke: bool = False
    smoke_skip: str | None = None

    @property
    def failed(self) -> bool:
        return any(line.status == FAIL for line in self.lines)


def diagnose(*, offline: bool, real: bool, settings) -> Environment:
    """Collect the environment facts; no desktop is touched."""
    env = Environment(offline=offline, real=real)
    windows = sys.platform.startswith("win")

    if offline:
        env.lines.append(
            CheckLine(
                "platform",
                OK,
                f"{sys.platform} (offline fixtures; live desktop checks not needed)",
            )
        )
        env.lines.append(CheckLine("uia provider", SKIP, "not needed offline"))
        env.can_smoke = True
    else:
        if windows:
            env.lines.append(CheckLine("platform", OK, sys.platform))
        else:
            env.lines.append(
                CheckLine(
                    "platform",
                    FAIL,
                    f"{sys.platform}: live desktop checks need Windows; "
                    "use --demo or --scenario for the offline path",
                )
            )
            env.can_smoke = False
            env.smoke_skip = "no Windows desktop in this process"
        if windows:
            ok, detail = _uia_probe()
            env.lines.append(CheckLine("uia provider", OK if ok else FAIL, detail))
            env.can_smoke = ok
            if not ok:
                env.smoke_skip = "the Windows UIA provider is not importable"

    env.lines.append(CheckLine("config", OK, str(settings.path)))
    if settings.problems:
        for problem in settings.problems:
            env.lines.append(CheckLine("config problems", WARN, problem))
    else:
        env.lines.append(CheckLine("config problems", OK, "none"))

    if offline:
        env.lines.append(
            CheckLine("child session", SKIP, "offline run; the sandbox is a fixture")
        )
    elif windows:
        _child_session_lines(env)
    else:
        env.lines.append(CheckLine("child session", SKIP, "not a Windows platform"))

    if not offline and env.can_smoke and not real and _no_child_sandbox():
        env.can_smoke = False
        env.smoke_skip = (
            "no live child session; start one with `somnus child-session "
            "start --auto`, or pass --real to use this desktop"
        )
        env.lines.append(CheckLine("sandbox", WARN, env.smoke_skip))

    return env


def render(env: Environment, smoke: CheckReport | None, *, excerpt_lines: int = 14) -> str:
    """The doctor page: one status line per fact, then the smoke evidence."""
    lines = ["SomnusUI doctor"]
    for line in env.lines:
        lines.append(f"[{line.status:<4}] {line.label:<16} {line.detail}".rstrip())

    if smoke is None:
        reason = env.smoke_skip or "not requested"
        lines.append(f"[{SKIP:<4}] {'smoke':<16} not run ({reason})")
        return "\n".join(lines)

    if smoke.ok:
        tasks = ", ".join(
            f"{task.name} {task.ms:.0f}ms" for task in smoke.considered
        )
        lines.append(
            f"[{OK:<4}] {'smoke':<16} PASS {smoke.passed}/{len(smoke.considered)} "
            f"({tasks}) desktop={smoke.desktop} provider={smoke.provider}"
        )
    else:
        detail = "; ".join(
            f"{task.name}: {task.detail}"
            for task in smoke.considered
            if not task.ok
        )
        lines.append(
            f"[{FAIL:<4}] {'smoke':<16} FAIL desktop={smoke.desktop} "
            f"provider={smoke.provider} {detail}"
        )
        if smoke.observation:
            lines.extend(_indent_excerpt(smoke.observation, excerpt_lines))
        return "\n".join(lines)

    for task in smoke.tasks:
        mark = SKIP if task.skipped else (OK if task.ok else FAIL)
        lines.append(f"[{mark:<4}]   {task.name:<14} {task.detail}")
    if smoke.observation:
        lines.extend(_indent_excerpt(smoke.observation, excerpt_lines))
    return "\n".join(lines)


def _uia_probe() -> tuple[bool, str]:
    """Whether the Windows provider can be imported, with the extras versions."""
    missing = [name for name in ("comtypes", "pywinauto") if util.find_spec(name) is None]
    if missing:
        return False, (
            "missing " + ", ".join(missing) + "; install with "
            "`pip install -e \".[windows]\"`"
        )
    try:
        from somnus.providers.windows.uia import UiaProvider  # noqa: F401
    except Exception as exc:  # noqa: BLE001 - the reason is the report
        return False, f"cannot import the UIA provider: {type(exc).__name__}: {exc}"
    versions = []
    for name in ("comtypes", "pywinauto"):
        try:
            versions.append(f"{name} {metadata.version(name)}")
        except metadata.PackageNotFoundError:  # pragma: no cover - unusual install
            versions.append(name)
    return True, ", ".join(versions)


def _child_session_lines(env: Environment) -> None:
    try:
        from somnus.session import childsession

        status = childsession.describe()
    except Exception as exc:  # noqa: BLE001 - a broken WTS layer is a finding
        env.lines.append(
            CheckLine("child session", FAIL, f"cannot read the WTS state: {exc}")
        )
        return

    if status["inside_child_session"]:
        env.lines.append(
            CheckLine(
                "child session",
                OK,
                f"running inside it (id={status['current_session_id']})",
            )
        )
    elif not status["supported"]:
        env.lines.append(
            CheckLine(
                "child session",
                FAIL,
                "feature off or unsupported; run the elevated "
                "`somnus child-session setup`",
            )
        )
    else:
        live = status["live_child_session_id"]
        if live is None:
            env.lines.append(
                CheckLine(
                    "child session",
                    WARN,
                    "enabled, but no live session; the sandbox needs one",
                )
            )
        else:
            env.lines.append(
                CheckLine(
                    "child session",
                    OK,
                    f"live id={live} user={status['child_session_user']}",
                )
            )


def _no_child_sandbox() -> bool:
    from somnus.session import childsession

    return not childsession.inside_child_session() and (
        childsession.live_child_session() is None
    )


def _indent_excerpt(text: str, limit: int) -> list[str]:
    lines = [line.rstrip() for line in text.splitlines()[:limit]]
    out = [f"       {line[:200]}" for line in lines]
    if len(text.splitlines()) > limit:
        out.append("       …")
    return out
