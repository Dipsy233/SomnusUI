"""Scroll-find: search a virtualised list by scrolling it (SomnusDroid doc §7.3).

Android's RecyclerView only realises the visible rows, so a keyword that is
not on screen is genuinely absent from the tree. ``FIND ... scroll`` loops
SCROLL_FORWARD on the target list, recompiling after each step, until the
query appears, the content stops changing, or the step budget runs out.

Design rules carried from the doc:

* only a node that *proves* the scroll capability (SCROLL_FORWARD in its
  patterns) is scrolled - never a gesture;
* two consecutive steps with identical content mean the end of the list;
* the loop is capped (default 15 steps) so a bottomless feed cannot hang a
  turn;
* the query re-matches the *whole window* each step, so a row that scrolled
  past (rendered then virtualised away) can still be caught at the boundary.
"""

from __future__ import annotations

import time
from typing import Callable

from somnus.ir.action import Action
from somnus.ir.node import SemanticNode

DEFAULT_MAX_STEPS = 15

#: One scroll's settle: the mock is deterministic; a live list needs a beat.
SCROLL_SETTLE_S = 0.05


def scroll_find(
    session,
    action: Action,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Run ``FIND "query" in REF scroll``; returns SUI-Text.

    ``session`` is a :class:`~somnus.runtime.session.Session` whose current
    compilation holds the scope ref. Falls back to a plain in-tree FIND when
    the scope node cannot scroll.
    """
    query = action.query or ""
    scope_ref = action.scope

    # Step 0: maybe it is already visible.
    plain = _plain_find(session, action)
    if not plain.startswith("FOUND 0"):
        return plain

    if not scope_ref:
        return plain
    result = session.result
    assert result is not None
    scope = result.find(scope_ref)
    if scope is None or "scroll" not in scope.patterns:
        # Not scrollable (or ref gone): the plain answer plus the usual
        # container hints is the honest result.
        return plain

    direction = "down"
    seen_signatures: list[str] = []
    for _step in range(max_steps):
        before = _window_signature(session)
        out = _scroll(session, scope_ref, direction)
        if not out.startswith("OK"):
            # A live RecyclerView re-renders while scrolling and may refuse a
            # step or retire the scope's key ("perform refused" / STALE).
            # That is not the end of the search: recompile and re-check, so
            # the loop either resumes on the fresh ref or honestly reports
            # exhaustion (found on device: MIUI's settings list refuses the
            # second SCROLL_FORWARD while its rows re-layout).
            session.refresh_windows()
            session._recompile()
            plain = _plain_find(session, action)
            if not plain.startswith("FOUND 0"):
                return plain
            scope = session.result.find(scope_ref)
            if scope is None or "scroll" not in scope.patterns:
                return plain
            continue
        sleeper(SCROLL_SETTLE_S)
        after = _window_signature(session)
        if after == before:
            break  # the list did not move: bottom (or top) reached

        plain = _plain_find(session, action)
        if not plain.startswith("FOUND 0"):
            return plain

        seen_signatures.append(after)

    return "FOUND 0\nscroll search exhausted the list without a match"


def _plain_find(session, action: Action) -> str:
    return session.find(action)


def _scroll(session, list_ref: str, direction: str) -> str:
    """One ``CALL <list_ref>.scroll(to=direction)`` through the session."""
    from somnus.sui.parse import parse_action

    out = session.dispatch(f"CALL {list_ref}.scroll(to={direction})")
    return out


def _window_signature(session) -> str:
    """A cheap fingerprint of the current compilation's row names.

    Names are what scroll changes (values rarely move in a settings list, but
    a catalog's rows swap wholesale); entity refs are excluded on purpose -
    they are re-allocated per compile and would make every step look new.
    """
    assert session.result is not None
    return "|".join(
        f"{n.role}:{n.name or ''}" for n in session.result.root.walk()
    )


def extend_action_for_scroll(action: Action) -> bool:
    """Whether ``action`` requests the scroll variant (``... scroll`` suffix).

    The parser does not know ``scroll``; the session peels it off the raw text
    before the regular FIND regex would reject it.
    """
    raw = (action.raw or "").strip()
    return raw.lower().endswith(" scroll")
