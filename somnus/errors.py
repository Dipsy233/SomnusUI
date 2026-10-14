"""Unified error model (dev doc section 10).

Every failure the LLM can see is one of these codes, rendered as a single
``ERR CODE ref hint=...`` line. Stack traces go to the log only.
"""

from __future__ import annotations

from dataclasses import dataclass


class ErrorCode:
    PARSE_ERROR = "PARSE_ERROR"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"
    STALE_TARGET = "STALE_TARGET"
    CAPABILITY_NOT_FOUND = "CAPABILITY_NOT_FOUND"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    ACTION_NOT_SUPPORTED = "ACTION_NOT_SUPPORTED"
    TARGET_DISABLED = "TARGET_DISABLED"
    #: A coordinate-based action (double-click) found another window on top of
    #: the target point. Recoverable: focus the window and retry.
    TARGET_NOT_FOREGROUND = "TARGET_NOT_FOREGROUND"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NATIVE_EXECUTION_FAILED = "NATIVE_EXECUTION_FAILED"
    TIMEOUT = "TIMEOUT"
    EFFECT_NOT_CONFIRMED = "EFFECT_NOT_CONFIRMED"
    UNKNOWN_CONTENT = "UNKNOWN_CONTENT"


#: Recovery hint per error code (rendered as ``hint=``).
HINTS: dict[str, str] = {
    ErrorCode.PARSE_ERROR: "fix-syntax",
    ErrorCode.TARGET_NOT_FOUND: "observe",
    ErrorCode.TARGET_AMBIGUOUS: "refine-query",
    ErrorCode.STALE_TARGET: "find",
    ErrorCode.CAPABILITY_NOT_FOUND: "inspect",
    ErrorCode.INVALID_ARGUMENT: "inspect",
    ErrorCode.ACTION_NOT_SUPPORTED: "report-user",
    ErrorCode.TARGET_DISABLED: "inspect",
    ErrorCode.TARGET_NOT_FOREGROUND: "focus-window",
    ErrorCode.CONFIRMATION_REQUIRED: "ask-user",
    ErrorCode.PERMISSION_DENIED: "report-user",
    ErrorCode.NATIVE_EXECUTION_FAILED: "retry-once",
    ErrorCode.TIMEOUT: "observe",
    ErrorCode.EFFECT_NOT_CONFIRMED: "observe",
    ErrorCode.UNKNOWN_CONTENT: "report-unknown",
}


@dataclass(slots=True)
class SomnusError(Exception):
    """An error intended for the LLM surface."""

    code: str
    ref: str | None = None
    message: str | None = None
    hint: str | None = None

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [f"ERR {self.code}"]
        if self.ref:
            parts.append(self.ref)
        parts.append(f"hint={self.hint or HINTS.get(self.code, 'retry')}")
        if self.message:
            parts.append(f'msg="{self.message}"')
        return " ".join(parts)

    def render(self) -> str:
        return str(self)


# Convenience constructors -------------------------------------------------


def parse_error(msg: str | None = None) -> SomnusError:
    """A syntax error; the message names the expected form so the model can fix it."""
    detail = msg or "bad syntax"
    return SomnusError(
        ErrorCode.PARSE_ERROR, message=f"{detail}; use CALL ref.cap(args)"
    )


def target_not_found(ref: str) -> SomnusError:
    return SomnusError(ErrorCode.TARGET_NOT_FOUND, ref=ref)


def stale_target(ref: str) -> SomnusError:
    return SomnusError(ErrorCode.STALE_TARGET, ref=ref)


def ambiguous(ref: str, count: int) -> SomnusError:
    return SomnusError(
        ErrorCode.TARGET_AMBIGUOUS, ref=ref, message=f"{count} matches"
    )


def capability_not_found(ref: str, cap: str) -> SomnusError:
    return SomnusError(
        ErrorCode.CAPABILITY_NOT_FOUND, ref=ref, message=f"no {cap}"
    )


def invalid_argument(ref: str, msg: str) -> SomnusError:
    return SomnusError(ErrorCode.INVALID_ARGUMENT, ref=ref, message=msg)
