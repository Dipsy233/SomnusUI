"""Persistent settings, so an opt-in stays opted in.

The gesture channel (``allow_gesture``) is **on by default** in this build: a
semantic agent that cannot tap is stopped by any control that publishes no
CLICK action (a self-drawn post card, a custom search bar, a game surface),
which measured as the largest class of unreachable UI on a phone. The tap's
coordinates come from the accessibility bounds the model never sees, and its
result is ``OK_UNVERIFIED`` unless the delta proves the effect - that honesty
is the safety net, not an off switch. The user can still turn it off
(``allow_gesture = false`` in the file, or ``--no-allow-gesture``).

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
    <platform config dir>/somnusdroid/config.toml

The platform directory follows the usual conventions:

======================  =====================================================
Windows                 ``%APPDATA%\\somnusdroid\\config.toml``
macOS                   ``~/Library/Application Support/somnusdroid/config.toml``
Linux/BSD               ``$XDG_CONFIG_HOME/somnusdroid/config.toml`` (or
                        ``~/.config/somnusdroid/config.toml``)
======================  =====================================================

Schema - every key optional, unknown keys ignored so a newer/older runtime and a
hand-edited file do not fight::

    # SomnusDroid settings. The gesture channel is on unless set to false here.
    allow_gesture = true      # taps at a node's bounds centre

    # The two host-side synthetic channels below matter only for routes that
    # leave the phone (they are kept for symmetry with the desktop build).
    allow_clipboard = true
    allow_keyboard  = true

    # Optional: restrict every action to these processes (apps).
    allow_processes = []

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
#: Environment overrides, so a shell or CI can set them without writing a
#: file. Set to a false-y value ("0", "false", "no", "") to turn a file's
#: ``true`` back off for one run.
ENV_ALLOW_GESTURE = "SOMNUS_ALLOW_GESTURE"
ENV_ALLOW_CLIPBOARD = "SOMNUS_ALLOW_CLIPBOARD"
ENV_ALLOW_KEYBOARD = "SOMNUS_ALLOW_KEYBOARD"

#: Keys understood in the file. Anything else is ignored (see module docstring).
_KNOWN_KEYS = frozenset(
    {
        "allow_gesture",
        "allow_clipboard",
        "allow_keyboard",
        "allow_processes",
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
        return Path(base) / "somnusdroid" / "config.toml"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "somnusdroid" / "config.toml"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "somnusdroid" / "config.toml"


@dataclass(slots=True)
class Settings:
    """Resolved settings: file values, plus whatever overrode them."""

    #: Taps at a node's bounds centre, for controls that publish no CLICK
    #: action. On unless disabled here or by a flag/env override.
    allow_gesture: bool = True
    #: The clipboard write channel (host-side routes).
    allow_clipboard: bool = True
    #: The synthetic typing channel (host-side routes).
    allow_keyboard: bool = True
    #: Optional app allow-list (empty = no restriction).
    allow_processes: tuple[str, ...] = ()
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
            f"allow_gesture    : {self.allow_gesture}",
            f"allow_clipboard  : {self.allow_clipboard}",
            f"allow_keyboard   : {self.allow_keyboard}",
            f"allow_processes  : {', '.join(self.allow_processes) or '(any)'}",
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

    for key in ("allow_gesture", "allow_clipboard", "allow_keyboard"):
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
        (ENV_ALLOW_GESTURE, "allow_gesture"),
        (ENV_ALLOW_CLIPBOARD, "allow_clipboard"),
        (ENV_ALLOW_KEYBOARD, "allow_keyboard"),
    ):
        override = _truthy_env(env_name)
        if override is not None:
            setattr(settings, key, override)
    return settings


def save(settings: Settings, path: Path | None = None) -> Path:
    """Write settings to *path* (or the default location), creating parents.

    TOML is written by hand rather than with a library: the schema is a
    handful of keys, and a hand-written file keeps the comments that tell a
    person (or an agent) what the switches do. A round-trip that dropped those
    comments would make the file worse to edit every time it was saved.
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
        "# SomnusDroid settings.\n"
        "#\n"
        "# The gesture channel is ON unless set to false below. It taps at a\n"
        "# control's own bounds centre for controls that publish no CLICK\n"
        "# action (self-drawn apps); without it those controls are unreachable.\n"
        "# The tap's coordinates come from the accessibility tree, never from\n"
        "# the model, and the result is OK_UNVERIFIED unless the delta proves\n"
        "# the effect.\n"
        "\n"
        f"allow_gesture = {_toml_bool(settings.allow_gesture)}\n"
        "\n"
        "# Host-side synthetic channels, kept for symmetry with the desktop\n"
        "# build. They only matter for routes that leave the phone.\n"
        f"allow_clipboard = {_toml_bool(settings.allow_clipboard)}\n"
        f"allow_keyboard = {_toml_bool(settings.allow_keyboard)}\n"
        "\n"
        "# Optional: restrict every action to these processes/apps (empty = any).\n"
        f"allow_processes = [{processes}]\n"
    )


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"
