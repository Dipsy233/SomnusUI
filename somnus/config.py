"""Persistent settings, so an opt-in stays opted in.

The two synthetic-input channels (``--allow-clipboard`` / ``--allow-keyboard``)
are **on by default** in this build: a semantic agent that cannot fall back to
the clipboard or the keyboard is stopped by any control that publishes no
writable interface (a self-drawn editor, a terminal surface, an omnibox that
only commits on Enter), which measured as the single largest class of unfinished
tasks. The user can turn either off (``allow_clipboard = false`` /
``allow_keyboard = false`` in the file, or ``--no-allow-*``), because both seize
the foreground and the clipboard borrows a resource that belongs to the user.

This module is where that decision is written down. It is deliberately:

* **plain text and hand-editable** - a small TOML file, so the user can open it
  in any editor without the runtime's help;
* **small enough for an LLM to edit** - the same file is what an agent is told
  about when it hits a refusal, so "you may allow this" is actionable rather
  than a dead end;
* **explicit about precedence** - an environment variable beats the file, and a
  command-line flag beats both, so a one-off override never has to edit state.

File location (first that applies)::

    $SOMNUS_CONFIG                      explicit path, for tests and packaging
    <platform config dir>/somnusui/config.toml

The platform directory follows the usual conventions:

======================  =====================================================
Windows                 ``%APPDATA%\\somnusui\\config.toml``
macOS                   ``~/Library/Application Support/somnusui/config.toml``
Linux/BSD               ``$XDG_CONFIG_HOME/somnusui/config.toml`` (or
                        ``~/.config/somnusui/config.toml``)
======================  =====================================================

Schema - every key optional, unknown keys ignored so a newer/older runtime and a
hand-edited file do not fight::

    # SomnusUI settings. Both channels are on unless set to false here.
    allow_clipboard = true    # Ctrl+V channel: needs the foreground
    allow_keyboard  = true    # SendInput channel: needs the foreground

    # Optional: restrict every action to these processes.
    allow_processes = []

    # Optional: run the agent in a child session (a second Windows session for
    # the same account). The synthetic channels stop competing with the user
    # for the foreground because the child session has its own.
    child_session = false
    child_session_debug_window = true   # keep the RDP loopback window visible

    # Optional: let the child-session peer start processes on that desktop
    # (`somnus child-session run -- ...`). On by default; set false to turn the
    # capability off without changing allow_processes, which still restricts
    # *which* processes may be operated/spawned.
    child_allow_spawn = true

    # Call timing. See docs/optimization-plan.md P1-2 for the trade-offs.
    settle_quiet_ms   = 40     # 10..1000: smaller = faster, can read mid-animation
    settle_poll_ms    = 10     # 1..100
    settle_timeout_s  = 3.0    # 0.5..30
    post_action_enum  = true   # report a window the action opened (~100ms/call)

Reading is best-effort by design: a malformed file must not stop the runtime, it
must only mean "the settings could not be read" - reported through
:attr:`Settings.problems` so the caller can say so out loud instead of silently
falling back to defaults the user did not choose.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

#: Environment variable naming an explicit config file.
ENV_CONFIG_PATH = "SOMNUS_CONFIG"
#: Environment overrides for the two switches, so a shell or CI can set them
#: without writing a file. Set to a false-y value ("0", "false", "no", "") to
#: turn a file's ``true`` back off for one run.
ENV_ALLOW_CLIPBOARD = "SOMNUS_ALLOW_CLIPBOARD"
ENV_ALLOW_KEYBOARD = "SOMNUS_ALLOW_KEYBOARD"
#: Environment overrides for the child-session switches.
ENV_CHILD_SESSION = "SOMNUS_CHILD_SESSION"
ENV_CHILD_SESSION_DEBUG = "SOMNUS_CHILD_SESSION_DEBUG_WINDOW"
ENV_CHILD_ALLOW_SPAWN = "SOMNUS_CHILD_ALLOW_SPAWN"
#: Environment overrides for the settle/action timing knobs.
ENV_SETTLE_QUIET_MS = "SOMNUS_SETTLE_QUIET_MS"
ENV_SETTLE_POLL_MS = "SOMNUS_SETTLE_POLL_MS"
ENV_SETTLE_TIMEOUT_S = "SOMNUS_SETTLE_TIMEOUT_S"
ENV_POST_ACTION_ENUM = "SOMNUS_POST_ACTION_ENUM"

#: Built-in defaults for the timing knobs (see docs/optimization-plan.md P1-2).
DEFAULT_SETTLE_QUIET_MS = 40
DEFAULT_SETTLE_POLL_MS = 10
DEFAULT_SETTLE_TIMEOUT_S = 3.0
DEFAULT_POST_ACTION_ENUM = True

#: Accepted ranges. Values outside are refused, never clamped: clamping would
#: silently accept a request the caller did not make.
SETTLE_QUIET_MS_RANGE = (10, 1000)
SETTLE_POLL_MS_RANGE = (1, 100)
SETTLE_TIMEOUT_S_RANGE = (0.5, 30.0)

#: The timing knobs, split by the type they accept.
_INT_KEYS = {
    "settle_quiet_ms": SETTLE_QUIET_MS_RANGE,
    "settle_poll_ms": SETTLE_POLL_MS_RANGE,
}
_FLOAT_KEYS = {"settle_timeout_s": SETTLE_TIMEOUT_S_RANGE}

#: Keys understood in the file. Anything else is ignored (see module docstring).
_KNOWN_KEYS = frozenset(
    {
        "allow_clipboard",
        "allow_keyboard",
        "allow_processes",
        "child_session",
        "child_session_debug_window",
        "child_allow_spawn",
        "settle_quiet_ms",
        "settle_poll_ms",
        "settle_timeout_s",
        "post_action_enum",
    }
)

_FALSEY = {"", "0", "false", "no", "off"}


def _truthy_env(name: str) -> bool | None:
    """Read a boolean environment override: True, False, or None when unset."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() not in _FALSEY


def default_config_path() -> Path:
    """Where the settings file lives when nothing overrides the location."""
    override = os.environ.get(ENV_CONFIG_PATH)
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")
        return Path(base) / "somnusui" / "config.toml"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "somnusui" / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "somnusui" / "config.toml"


@dataclass(slots=True)
class Settings:
    """Resolved settings: file values, plus whatever overrode them."""

    #: The Ctrl+V channel. On unless disabled here or by a flag/env override.
    allow_clipboard: bool = True
    #: The SendInput channel. On unless disabled here or by a flag/env override.
    allow_keyboard: bool = True
    #: Optional process allow-list (empty = no restriction).
    allow_processes: tuple[str, ...] = ()
    #: Run the agent in a child session so its synthetic input cannot reach the
    #: user's desktop. Off unless asked for: it needs Pro and a one-time admin
    #: setup.
    child_session: bool = False
    #: Keep the RDP loopback window visible, for testing. Only meaningful when
    #: :attr:`child_session` is on.
    child_session_debug_window: bool = True
    #: Let the peer in the child session start processes there. This is a
    #: capability of the *far* session, not of the action allow-list: turning it
    #: off makes `process.start` refuse, while `allow_processes` still decides
    #: which executables may be operated (and, when set, spawned).
    child_allow_spawn: bool = True
    #: How long the UI must stay unchanged before an action counts as done.
    #: Smaller is faster but can read an animation mid-flight; larger is safer
    #: but makes every call wait longer.
    settle_quiet_ms: int = DEFAULT_SETTLE_QUIET_MS
    #: Probe spacing while waiting for that quiet window.
    settle_poll_ms: int = DEFAULT_SETTLE_POLL_MS
    #: Hard cap for one settle; at the cap the reply says ``settled=false`` and
    #: still reports the delta.
    settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S
    #: Re-enumerate windows after an action so a window the action opened can be
    #: reported. Off saves ~100 ms per call and can hide that window.
    post_action_enum: bool = DEFAULT_POST_ACTION_ENUM
    #: The file these came from (even when it does not exist yet), so a host can
    #: tell the user exactly which path to edit.
    path: Path | None = None
    #: Non-fatal reading problems: a malformed file, a bad value type. Empty
    #: means the file was read cleanly (or does not exist).
    problems: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        """A short human/LLM-readable summary, used by ``somnus config``."""
        where = str(self.path) if self.path else "<none>"
        lines = [
            f"config path      : {where}",
            f"allow_clipboard  : {self.allow_clipboard}",
            f"allow_keyboard   : {self.allow_keyboard}",
            f"allow_processes  : {', '.join(self.allow_processes) or '(any)'}",
            f"child_session    : {self.child_session}",
            f"child_session_win: {self.child_session_debug_window}",
            f"child_allow_spawn: {self.child_allow_spawn}",
            f"settle_quiet_ms  : {self.settle_quiet_ms}",
            f"settle_poll_ms   : {self.settle_poll_ms}",
            f"settle_timeout_s : {self.settle_timeout_s}",
            f"post_action_enum : {self.post_action_enum}",
        ]
        for problem in self.problems:
            lines.append(f"warning          : {problem}")
        return "\n".join(lines)


def load(path: Path | None = None) -> Settings:
    """Read settings from *path* (or the default location).

    Never raises for a missing or malformed file: a bad config must degrade to
    "defaults, with a warning", not to a runtime that will not start. The
    warning is carried in :attr:`Settings.problems`.
    """
    target = Path(path).expanduser() if path is not None else default_config_path()
    settings = Settings(path=target)

    if target.is_file():
        try:
            with target.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            settings.problems = (*settings.problems, f"cannot read {target}: {exc}")
            data = {}
        settings = _apply_file(settings, data)
    elif target.exists():
        settings.problems = (*settings.problems, f"{target} is not a regular file")

    return _apply_env(settings)


def _apply_file(settings: Settings, data: dict) -> Settings:
    """Fold the file's values in, reporting the ones that made no sense."""
    problems = list(settings.problems)

    for key in (
        "allow_clipboard",
        "allow_keyboard",
        "child_session",
        "child_session_debug_window",
        "child_allow_spawn",
        "post_action_enum",
    ):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            setattr(settings, key, value)
        else:
            problems.append(f"{key} must be true/false, got {type(value).__name__}")

    for key, (lo, hi) in _INT_KEYS.items():
        if key not in data:
            continue
        value = data[key]
        # ``bool`` is an ``int`` in Python; a ``true`` in an integer slot is a
        # typo, not the number 1.
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(
                f"{key} must be an integer in {lo}..{hi}, got {type(value).__name__}"
            )
        elif not lo <= value <= hi:
            problems.append(f"{key} must be in {lo}..{hi}, got {value}")
        else:
            setattr(settings, key, value)

    for key, (lo, hi) in _FLOAT_KEYS.items():
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(
                f"{key} must be a number in {lo}..{hi}, got {type(value).__name__}"
            )
        elif not lo <= float(value) <= hi:
            problems.append(f"{key} must be in {lo}..{hi}, got {value}")
        else:
            setattr(settings, key, float(value))

    if "allow_processes" in data:
        value = data["allow_processes"]
        if isinstance(value, list) and all(isinstance(p, str) for p in value):
            settings.allow_processes = tuple(value)
        else:
            problems.append("allow_processes must be a list of process names")

    for key in data:
        if key not in _KNOWN_KEYS:
            # Ignored rather than rejected: a file written by a newer version,
            # or one the user annotated with their own notes, must still load.
            problems.append(f"ignoring unknown key {key!r}")

    settings.problems = tuple(problems)
    return settings


def _apply_env(settings: Settings) -> Settings:
    """Let the environment override the file, so a run can differ from the file.

    Checked *after* the file, so an override works in both directions: a shell
    can turn on what the file leaves off, and turn off what the file enables.
    An unparseable override is reported and ignored - a typo in a shell must not
    silently become some other timing.
    """
    problems = list(settings.problems)

    for env_name, key in (
        (ENV_ALLOW_CLIPBOARD, "allow_clipboard"),
        (ENV_ALLOW_KEYBOARD, "allow_keyboard"),
        (ENV_CHILD_SESSION, "child_session"),
        (ENV_CHILD_SESSION_DEBUG, "child_session_debug_window"),
        (ENV_CHILD_ALLOW_SPAWN, "child_allow_spawn"),
        (ENV_POST_ACTION_ENUM, "post_action_enum"),
    ):
        override = _truthy_env(env_name)
        if override is not None:
            setattr(settings, key, override)

    for env_name, key, lo, hi in (
        (ENV_SETTLE_QUIET_MS, "settle_quiet_ms", *SETTLE_QUIET_MS_RANGE),
        (ENV_SETTLE_POLL_MS, "settle_poll_ms", *SETTLE_POLL_MS_RANGE),
    ):
        value, problem = _env_int(env_name, lo, hi)
        if problem:
            problems.append(problem)
        elif value is not None:
            setattr(settings, key, value)

    value, problem = _env_float(ENV_SETTLE_TIMEOUT_S, *SETTLE_TIMEOUT_S_RANGE)
    if problem:
        problems.append(problem)
    elif value is not None:
        settings.settle_timeout_s = value

    settings.problems = tuple(problems)
    return settings


def _env_int(name: str, lo: int, hi: int) -> tuple[int | None, str | None]:
    """A strict integer override: ``"40"`` yes, ``"40ms"``/``"4_0"`` no."""
    raw = os.environ.get(name)
    if raw is None:
        return None, None
    text = raw.strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        return None, f"{name} must be an integer in {lo}..{hi}, got {raw!r}"
    value = int(text)
    if not lo <= value <= hi:
        return None, f"{name} must be in {lo}..{hi}, got {value}"
    return value, None


def _env_float(name: str, lo: float, hi: float) -> tuple[float | None, str | None]:
    """A strict float override; no unit suffixes, no locale commas."""
    raw = os.environ.get(name)
    if raw is None:
        return None, None
    text = raw.strip()
    if not re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?", text):
        return None, f"{name} must be a number in {lo}..{hi}, got {raw!r}"
    value = float(text)
    if not lo <= value <= hi:
        return None, f"{name} must be in {lo}..{hi}, got {value}"
    return value, None


#: The keys ``configure`` may change, and the type each one takes.
_RUNTIME_KEYS = (
    "settle_quiet_ms",
    "settle_poll_ms",
    "settle_timeout_s",
    "post_action_enum",
)


def validate_settle_overrides(**values: object) -> list[str]:
    """Check a runtime patch *before* anything is applied.

    The caller applies nothing when this returns a problem: a half-applied
    timing change is worse than a refused one, because the reply would then
    describe values the process does not actually use. Messages are shaped for
    the one-line ``ERR INVALID_ARGUMENT`` reply, e.g. ``settle_quiet_ms expected
    int in 10..1000 got=-5``.
    """
    problems: list[str] = []

    for key, (lo, hi) in _INT_KEYS.items():
        if key not in values or values[key] is None:
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int):
            problems.append(f"{key} expected int in {lo}..{hi} got={value!r}")
        elif not lo <= value <= hi:
            problems.append(f"{key} expected int in {lo}..{hi} got={value}")

    for key, (lo, hi) in _FLOAT_KEYS.items():
        if key not in values or values[key] is None:
            continue
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{key} expected number in {lo}..{hi} got={value!r}")
        elif not lo <= float(value) <= hi:
            problems.append(f"{key} expected number in {lo}..{hi} got={value}")

    if values.get("post_action_enum") is not None and not isinstance(
        values["post_action_enum"], bool
    ):
        problems.append(
            f"post_action_enum expected true/false got={values['post_action_enum']!r}"
        )

    unknown = [key for key in values if key not in _RUNTIME_KEYS and values[key] is not None]
    if unknown:
        problems.append(f"unknown setting {unknown[0]!r}")

    return problems


def save(settings: Settings, path: Path | None = None) -> Path:
    """Write settings to *path* (or the default location), creating parents.

    TOML is written by hand rather than with a library: the schema is three keys,
    and a hand-written file keeps the comments that tell a person (or an agent)
    what the switches do. A round-trip that dropped those comments would make the
    file worse to edit every time it was saved.
    """
    target = Path(path).expanduser() if path is not None else (settings.path or default_config_path())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_toml(settings), encoding="utf-8")
    settings.path = target
    return target


def persist_session_settings(
    settle_quiet_ms: int,
    settle_poll_ms: int,
    settle_timeout_s: float,
    post_action_enum: bool,
) -> Path:
    """Write the four timing values into the same file :func:`load` reads.

    This is the entry the session's ``apply_settings(persist=True)`` calls: it
    receives the *effective* values (defaults already resolved, including any
    env override), so the file becomes the next process's baseline. Unrelated
    keys are preserved by loading the file first.
    """
    settings = load()
    settings.settle_quiet_ms = int(settle_quiet_ms)
    settings.settle_poll_ms = int(settle_poll_ms)
    settings.settle_timeout_s = float(settle_timeout_s)
    settings.post_action_enum = bool(post_action_enum)
    return save(settings)


def render_toml(settings: Settings) -> str:
    """The file's text, comments included."""
    processes = ", ".join(f'"{p}"' for p in settings.allow_processes)
    return (
        "# SomnusUI settings.\n"
        "#\n"
        "# Both synthetic-input channels are ON unless set to false below.\n"
        "# They seize the screen's foreground (and the clipboard channel borrows\n"
        "# your clipboard), so turn one off if you do not accept that a window\n"
        "# the agent writes into may come to the front.\n"
        "\n"
        "# Ctrl+V channel: reaches a self-drawn editor that publishes no\n"
        "# writable interface. Borrows your clipboard, then restores it\n"
        "# format by format (images included).\n"
        f"allow_clipboard = {_toml_bool(settings.allow_clipboard)}\n"
        "\n"
        "# SendInput channel: types the text. Last resort - it is slower, can\n"
        "# drop characters, and is the least verifiable route.\n"
        f"allow_keyboard = {_toml_bool(settings.allow_keyboard)}\n"
        "\n"
        "# Optional: restrict every action to these processes (empty = any).\n"
        f"allow_processes = [{processes}]\n"
        "\n"
        "# Run the agent in a child session: a second Windows session for the\n"
        "# same account (same files, same programs) with its own input desktop,\n"
        "# so synthetic input stops competing with you for the foreground.\n"
        "# Needs Windows Pro/Enterprise/Education and a one-time admin setup:\n"
        "# `somnus child-session setup`. End users normally do not need this.\n"
        f"child_session = {_toml_bool(settings.child_session)}\n"
        "\n"
        "# Keep the RDP loopback window visible (testing). It does not affect\n"
        "# isolation - the session is the same either way - only what you see.\n"
        f"child_session_debug_window = {_toml_bool(settings.child_session_debug_window)}\n"
        "\n"
        "# Let the peer in the child session start processes on that desktop\n"
        "# (`somnus child-session run -- ...`). allow_processes still restricts\n"
        "# which executables may be operated or spawned.\n"
        f"child_allow_spawn = {_toml_bool(settings.child_allow_spawn)}\n"
        "\n"
        "# How long the UI must stay unchanged before an action counts as done.\n"
        "# Smaller = faster calls, but an animation that has not finished can be\n"
        "# read mid-flight (a button may still look disabled); larger = safer,\n"
        "# but every call waits that much longer.\n"
        f"settle_quiet_ms = {settings.settle_quiet_ms}\n"
        "\n"
        "# Probe spacing while waiting for that quiet window.\n"
        f"settle_poll_ms = {settings.settle_poll_ms}\n"
        "\n"
        "# Hard cap for one settle (seconds); at the cap the reply says\n"
        "# settled=false and still reports the delta.\n"
        f"settle_timeout_s = {settings.settle_timeout_s}\n"
        "\n"
        "# Re-enumerate windows after an action, so a window the action opened\n"
        "# can be reported. Off saves ~100 ms per call and can hide that window.\n"
        f"post_action_enum = {_toml_bool(settings.post_action_enum)}\n"
    )


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
