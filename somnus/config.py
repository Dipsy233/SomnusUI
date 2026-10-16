"""Persistent settings, so an opt-in stays opted in.

The two synthetic-input channels (``--allow-clipboard`` / ``--allow-keyboard``)
are **on by default** in this build: a semantic agent that cannot fall back to
the clipboard or the keyboard is stopped by any control that publishes no
writable interface (a self-drawn editor, a terminal surface, an omnibox that
only commits on Enter), which measured as the single largest class of unfinished
tasks. The user can turn either off (``allow_clipboard = false`` /
``allow_keyboard = false`` in the file, or ``--no-allow-*``), because both still
seize the foreground and the clipboard borrows a resource that belongs to the
user.

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

Reading is best-effort by design: a malformed file must not stop the runtime, it
must only mean "the settings could not be read" - reported through
:attr:`Settings.problems` so the caller can say so out loud instead of silently
falling back to defaults the user did not choose.
"""

from __future__ import annotations

import os
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

#: Keys understood in the file. Anything else is ignored (see module docstring).
_KNOWN_KEYS = frozenset(
    {
        "allow_clipboard",
        "allow_keyboard",
        "allow_processes",
        "child_session",
        "child_session_debug_window",
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

    for key in ("allow_clipboard", "allow_keyboard", "child_session", "child_session_debug_window"):
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, bool):
            setattr(settings, key, value)
        else:
            problems.append(f"{key} must be true/false, got {type(value).__name__}")

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
    """
    for env_name, key in (
        (ENV_ALLOW_CLIPBOARD, "allow_clipboard"),
        (ENV_ALLOW_KEYBOARD, "allow_keyboard"),
        (ENV_CHILD_SESSION, "child_session"),
        (ENV_CHILD_SESSION_DEBUG, "child_session_debug_window"),
    ):
        override = _truthy_env(env_name)
        if override is not None:
            setattr(settings, key, override)
    return settings


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
    )


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
