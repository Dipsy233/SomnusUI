"""Child sessions: drive the GUI from a second Windows session, not the user's.

A semantic agent that must raise a window and type into it competes with the
person at the keyboard for the desktop's single foreground. A **child session**
removes the competition: it is a second interactive login session for the same
account - same files, same programs - with its own input desktop and foreground.
The agent runs there, the user keeps this one.

Three pieces:

* :mod:`somnus.session.childsession` - the ``wtsapi32`` layer (enable, query,
  "am I inside one?").
* :mod:`somnus.session.rdp` - the RDP loopback that materialises the session,
  hosted by the small .NET container in ``tools/rdp-host``.
* :mod:`somnus.session.launch` - starting and supervising that host from Python.

See ``docs/child-session.md`` for the trade-offs (Pro edition only, administrator
rights once, one child session system-wide, no security isolation).
"""

from __future__ import annotations

from somnus.session.childsession import (
    ChildSessionUnsupported,
    child_session_id,
    current_session_id,
    describe,
    enable,
    inside_child_session,
    is_enabled,
)
from somnus.session.bridge import (
    PeerBridge,
    PeerServer,
    PeerUnavailable,
    pipe_name,
)
from somnus.session.launch import (
    ChildSessionRun,
    HostMissing,
    enable_child_sessions,
    find_host,
)
from somnus.session import setup

__all__ = [
    "ChildSessionRun",
    "ChildSessionUnsupported",
    "HostMissing",
    "PeerBridge",
    "PeerServer",
    "PeerUnavailable",
    "child_session_id",
    "current_session_id",
    "describe",
    "enable",
    "enable_child_sessions",
    "find_host",
    "inside_child_session",
    "is_enabled",
    "pipe_name",
    "setup",
]
