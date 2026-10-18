"""SomnusDroid: Android provider over a Somnus Portal phone-side app.

Layers (SomnusDroid doc §3):

* ``transport``  - adb forward + WebSocket JSON-RPC to the Portal app
* ``rawtree``    - Portal JSON -> :class:`RawNode` (this module)
* ``provider``   - the :class:`AccessibilityProvider` implementation

The provider is the only Android-specific module; everything above it
(compiler, projection, runtime, protocol) is shared with SomnusUI unchanged.
Android native identity lives in :class:`NativeRef` with:

* ``window_id``   - ``"<displayId>/<windowId>"``
* ``runtime_id``  - the Portal's stable node key path (e.g. ``(12, 0, 3, 1)``)
* ``automation_id`` - the resource-id. Used for *internal fingerprints only*,
  never serialized to the LLM (same contract as UIA's AutomationId).
* ``class_name``  - the Android className (used by the compiler's editor
  recognition and by :mod:`somnus.compiler.rules.android` normalization).
"""

from __future__ import annotations

from .provider import AndroidProvider
from .rawtree import portal_json_to_tree, portal_json_to_windows

__all__ = [
    "AndroidProvider",
    "portal_json_to_tree",
    "portal_json_to_windows",
]
