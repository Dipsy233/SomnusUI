"""The tools exposed to an LLM.

Model-agnostic: each tool is a thin function over a :class:`Session`. The MCP
server and the JSON-RPC server both call :func:`call_tool`, so behaviour is
identical regardless of transport.

The tool *contracts* are deliberately small and closed. An LLM that only speaks
text uses the same syntax via ``llm/parser``; nothing here depends on function
calling.
"""

from __future__ import annotations

import inspect
import math
from typing import Any

from somnus.config import (
    DEFAULT_POST_ACTION_ENUM,
    DEFAULT_SETTLE_POLL_MS,
    DEFAULT_SETTLE_QUIET_MS,
    DEFAULT_SETTLE_TIMEOUT_S,
)
from somnus.errors import ErrorCode, SomnusError
from somnus.ir.action import Action
from somnus.runtime.session import Session

#: Names + JSON schemas advertised to the LLM.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "observe",
        "description": (
            "L0 overview; assigns refs. Use first/after switch; find for a "
            "name. Returns @rev, WINDOWS, tree (`?` unreadable). Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {
                    "type": "string",
                    "description": "wN; omitted = current",
                },
                "windows_only": {"type": "boolean"},
                "include_other_windows": {"type": "boolean"},
            },
        },
    },
    {
        "name": "find",
        "description": (
            "Search current window incl folded lists; returns entity lines. "
            "Use for a name; observe for layout. FOUND N or 0; timeout s waits, "
            "expiry ERR TARGET_NOT_FOUND after=Ns."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "timeout": {
                    "type": "number",
                    "minimum": 0,
                    "description": "s; omitted = once",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "inspect",
        "description": (
            "Full node: attrs, caps, params, relations. Use when caps/args look "
            "wrong. Returns @rev + node or TEXT range=A:B; a viewport miss "
            "retries one full read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "depth": {"type": "integer"},
                "start": {"type": "integer", "description": "text range start"},
                "end": {"type": "integer", "description": "text range end"},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "call",
        "description": (
            "Invoke caps; ≤20 CALL lines, one trip. Use for independent; run "
            "for opened. Returns OK/ERR + delta, or BATCH status steps=N/M "
            "rev=R (PARTIAL adds failed-at|stopped-at=K). Steps must exist at "
            "start; wN.focus() and context_menu refused."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string"},
                "rev": {"type": "integer"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "run",
        "description": (
            "CALL/AWAIT script. Use for opened; call for independent. Returns "
            "RUN OK|PARTIAL steps=N/M + step lines + delta; AWAIT settles, "
            "first failure stops. ≤40 steps; AWAIT [TYPE] \"name\" [in ref] "
            "[timeout=2] binds $it."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "script": {"type": "string"},
                "window": {"type": "string", "description": "wN; focus first"},
            },
            "required": ["script"],
        },
    },
    {
        "name": "switch",
        "description": (
            "Switch main/child. open stays off the user. main; child must be "
            "open; open starts/moves in; close returns; detach keeps running. "
            "Returns OK + windows; close/detach OK. ERR PERMISSION_DENIED if "
            "missing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "enum": ["main", "child", "open", "close", "detach"],
                }
            },
            "required": ["target"],
        },
    },
    {
        "name": "launch",
        "description": (
            "Start a program; pid. Args: command, args, cwd, window (wN/child; "
            "current default)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string"},
                "window": {"type": "string"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "configure",
        "description": (
            "Tune settle timing; use when calls read mid-animation or slow. "
            "Returns SETTINGS; bad value: ERR INVALID_ARGUMENT, nothing changed."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "settle_quiet_ms": {
                    "type": "integer",
                    "minimum": 10,
                    "maximum": 1000,
                    "description": "ms, 40",
                },
                "settle_poll_ms": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "ms, 10",
                },
                "settle_timeout_s": {
                    "type": "number",
                    "minimum": 0.5,
                    "maximum": 30,
                    "description": "s, 3.0",
                },
                "post_action_enum": {
                    "type": "boolean",
                    "description": "on = report opened",
                },
                "persist": {"type": "boolean", "description": "write config"},
                "reset": {"type": "boolean", "description": "startup values"},
            },
        },
    },
]


def call_tool(session: Session, name: str, arguments: dict[str, Any] | None = None) -> str:
    """Execute one tool call and return SUI-Text (never raises to the caller)."""
    args = arguments or {}
    try:
        if name == "observe":
            return session.observe(
                args.get("window"),
                windows_only=bool(args.get("windows_only")),
                include_other_windows=bool(args.get("include_other_windows")),
            ).text
        if name == "find":
            # The `query` argument accepts the FIND syntax verbatim
            # (``FIND product "Big Mac"``) or a bare query string.
            raw = (args.get("query") or "").strip()
            from somnus.sui.parse import parse_action

            if raw.upper().startswith("FIND"):
                action = parse_action(raw)
            else:
                action = Action(kind="find", query=raw)
            timeout_s, problem = _find_timeout(args.get("timeout"))
            if problem:
                return SomnusError(
                    ErrorCode.INVALID_ARGUMENT, message=problem
                ).render()
            # Omitted timeout keeps the old one-argument call: the collection
            # side's signature is `find(pattern, window=None, timeout_s=None)`,
            # so an absent timeout means exactly the same thing there.
            if timeout_s is None:
                return session.find(action)
            if not _accepts_timeout_s(session.find):
                # Never crash the host on an optional argument: say the wait
                # this build cannot honour is unavailable.
                return SomnusError(
                    ErrorCode.ACTION_NOT_SUPPORTED,
                    message="this session's find has no timeout yet",
                ).render()
            return session.find(action, timeout_s=timeout_s)
        if name == "inspect":
            rng = None
            if args.get("start") is not None and args.get("end") is not None:
                rng = (int(args["start"]), int(args["end"]))
            action = Action(
                kind="inspect",
                ref=args.get("ref"),
                depth=args.get("depth"),
                range=rng,
            )
            return session.inspect(action).text
        if name == "call":
            from somnus.sui.parse import parse_call_block

            actions = parse_call_block(args.get("action", ""), rev=args.get("rev"))
            if len(actions) == 1:
                return session.call(actions[0]).text
            return session.call_batch(actions).text
        if name == "run":
            from somnus.sui.parse import parse_script

            script = args.get("script")
            if not isinstance(script, str) or not script.strip():
                return SomnusError(
                    ErrorCode.PARSE_ERROR,
                    message="run needs a non-empty script string",
                ).render()
            window = args.get("window")
            if window is not None and not isinstance(window, str):
                return SomnusError(
                    ErrorCode.INVALID_ARGUMENT,
                    message="window expected a ref string",
                ).render()
            return session.run_script(
                parse_script(script),
                window=window,
            )
        if name == "switch":
            target = (args.get("target") or "main").strip().lower()
            if target == "open":
                return session.open_child_session()
            if target == "close":
                return session.close_child_session()
            if target == "detach":
                return session.detach_child_session()
            return session.switch_session(target)
        if name == "launch":
            return session.launch(
                args.get("command"),
                args=args.get("args"),
                cwd=args.get("cwd"),
                window=args.get("window"),
            )
        if name == "configure":
            return _configure(session, args)
        return SomnusError(ErrorCode.PARSE_ERROR, message=f"unknown tool {name}").render()
    except SomnusError as err:
        return err.render()


#: The timing knobs ``configure`` accepts. ``persist``/``reset`` are verbs, not
#: settings, so they are handled separately.
_SETTING_ARGS = (
    "settle_quiet_ms",
    "settle_poll_ms",
    "settle_timeout_s",
    "post_action_enum",
)


def _configure(session: Session, args: dict[str, Any]) -> str:
    """Tune this session's settle/action timing (the ``configure`` tool).

    Validation happens **before** anything is applied: one bad value refuses the
    whole patch, so the reply never describes a state the process is not in.
    Timing is a session-scope change; ``persist=true`` also writes the same file
    :func:`somnus.config.load` reads, and ``reset=true`` returns to what that
    load produced at startup.
    """
    from somnus import config as config_module

    reset = bool(args.get("reset", False))
    persist = bool(args.get("persist", False))
    # ``reset`` is the whole instruction, so a patch sent beside it is ignored
    # rather than partially applied and then undone.
    patch = (
        {}
        if reset
        else {
            key: args[key]
            for key in _SETTING_ARGS
            if args.get(key) is not None
        }
    )

    problems = config_module.validate_settle_overrides(**patch)
    if problems:
        return SomnusError(
            ErrorCode.INVALID_ARGUMENT,
            message="; ".join(problems) + "; nothing changed",
        ).render()

    apply_settings = getattr(session, "apply_settings", None)
    if apply_settings is None:
        # The session side is a separate change; refuse loudly instead of
        # pretending a setting took effect nowhere.
        return SomnusError(
            ErrorCode.ACTION_NOT_SUPPORTED,
            message="configure: this session has no settings interface",
        ).render()

    try:
        applied = apply_settings(
            quiet_ms=patch.get("settle_quiet_ms"),
            poll_ms=patch.get("settle_poll_ms"),
            timeout_s=patch.get("settle_timeout_s"),
            post_action_enum=patch.get("post_action_enum"),
            persist=persist,
            reset=reset,
        )
    except NotImplementedError:
        # The session exists but its persistence hook is not wired yet; nothing
        # was applied, so say so instead of half-answering.
        return SomnusError(
            ErrorCode.ACTION_NOT_SUPPORTED,
            message="configure: this session cannot persist settings yet",
        ).render()
    except OSError as exc:
        return SomnusError(
            ErrorCode.NATIVE_EXECUTION_FAILED,
            message=(
                "settings were not changed; the config file could not be "
                f"written: {exc}"
            ),
        ).render()

    effective = _effective_settings(session, applied)
    persisted = (
        applied.get("persisted") if isinstance(applied, dict) else None
    )

    line = (
        f"SETTINGS quiet_ms={int(effective['quiet_ms'])} "
        f"poll_ms={int(effective['poll_ms'])} "
        f"timeout_s={float(effective['timeout_s'])} "
        f"post_action_enum={'on' if effective['post_action_enum'] else 'off'} "
        "scope=session"
    )
    if persisted is not None:
        line += f" persisted={persisted}"
    return line


def _effective_settings(session: Session, applied: Any) -> dict[str, Any]:
    """The values now in force, preferring what the session returned.

    The session returns its canonical ``settle_*`` keys plus ``scope``; the
    shorter names are accepted too, and the live attributes are the last
    fallback so a partially wired host still answers with the truth rather than
    a copy of the request.
    """
    settler = getattr(session, "settler", None)

    def pick(short: str, canonical: str, attr: str, default: Any) -> Any:
        if isinstance(applied, dict):
            for key in (canonical, short):
                if key in applied:
                    return applied[key]
        if short == "post_action_enum":
            return getattr(session, "post_action_enum", default)
        return getattr(settler, attr, default)

    return {
        "quiet_ms": pick(
            "quiet_ms", "settle_quiet_ms", "quiet_ms", DEFAULT_SETTLE_QUIET_MS
        ),
        "poll_ms": pick(
            "poll_ms", "settle_poll_ms", "poll_interval_ms", DEFAULT_SETTLE_POLL_MS
        ),
        "timeout_s": pick(
            "timeout_s", "settle_timeout_s", "timeout_s", DEFAULT_SETTLE_TIMEOUT_S
        ),
        "post_action_enum": pick(
            "post_action_enum",
            "post_action_enum",
            "",
            DEFAULT_POST_ACTION_ENUM,
        ),
    }


def _find_timeout(value: Any) -> tuple[float | None, str | None]:
    """Validate ``find``'s optional ``timeout`` (seconds).

    Strict on purpose: ``"2"`` is a string and is refused rather than parsed,
    and a negative or non-finite value is refused rather than clamped, because
    the caller asked for a wait that cannot be honoured.
    """
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, f"timeout expected number >= 0 got={value!r}"
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        return None, f"timeout expected number >= 0 got={value!r}"
    return seconds, None


def _accepts_timeout_s(method: Any) -> bool:
    """Whether the session's ``find`` takes the optional ``timeout_s``."""
    try:
        return "timeout_s" in inspect.signature(method).parameters
    except (TypeError, ValueError):  # pragma: no cover - builtins only
        return False
