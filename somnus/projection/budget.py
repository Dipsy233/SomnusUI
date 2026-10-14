"""Token accounting and observation budgets (dev doc section 7.3).

Token counting uses a real tokenizer when one is installed (``tiktoken`` for
o200k/cl100k), otherwise a documented estimator: ASCII ~4 chars/token, CJK
~1 token/char. Both paths are deterministic, which is what golden tests need.
"""

from __future__ import annotations

from dataclasses import dataclass

try:  # pragma: no cover - optional dependency
    import tiktoken

    _ENC = None

    def _get_enc():
        global _ENC
        if _ENC is None:
            _ENC = tiktoken.get_encoding("o200k_base")
        return _ENC

except Exception:  # pragma: no cover - fallback path
    tiktoken = None

    def _get_enc():  # type: ignore[misc]
        return None


def count_tokens(text: str) -> int:
    """Count tokens exactly when possible, else estimate."""
    enc = _get_enc()
    if enc is not None:
        return len(enc.encode(text))
    return estimate_tokens(text)


def estimate_tokens(text: str) -> int:
    """Estimate: non-ASCII (roughly CJK) counts 1/char, ASCII 1/4 chars."""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    other = len(text) - ascii_chars
    return (ascii_chars + 3) // 4 + other


@dataclass(slots=True)
class ObservationBudget:
    """Limits applied to any observation returned to the LLM."""

    max_nodes: int = 100
    max_depth: int = 8
    max_tokens: int = 1500
    hard_tokens: int = 3000

    def for_level(self, level: int) -> "ObservationBudget":
        """Return a budget tuned to observation level L0/L1/L2."""
        if level == 0:
            return ObservationBudget(
                max_nodes=40, max_depth=3, max_tokens=300, hard_tokens=300
            )
        if level == 1:
            return ObservationBudget(
                max_nodes=120, max_depth=6, max_tokens=1500, hard_tokens=1500
            )
        return ObservationBudget(
            max_nodes=200, max_depth=12, max_tokens=500, hard_tokens=500
        )
