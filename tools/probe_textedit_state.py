"""Is QQ's TextEditPattern a dead end, or just an empty/unfocused editor?

The first probe returned ``ptr=0x0`` (NULL) for both
``GetActiveComposition`` and ``GetConversionTarget`` on QQ's ProseMirror box.
That is an ambiguous result and it matters which reading is right:

* NULL because the *provider does not implement them* -> dead end, the report's
  "no writable interface" conclusion stands;
* NULL because the *editor is empty / not in composition* -> the interface is
  real and the probe was simply run in the one state where it yields nothing.

The report's own second law ("a method existing is not a method working") has a
mirror image worth stating: **a method returning NULL once is not proof that it
never works.** It must be run in the state the method is defined for.

TextEditPattern's two ranges are defined for an *active composition* - i.e. an
IME conversion in progress. QQ's box is driven by an IME (the UI is Chinese), so
the honest test is:

  A. with an empty, unfocused editor (what the first probe did)
  B. with focus placed in the editor and text present

This script tries B and reports what changes, without sending synthetic
keystrokes: focus is placed through UIA ``SetFocus`` and content is added only
via routes that are themselves under test.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

from somnus.providers.windows.uia import UiaProvider

TARGET_MATCH = "ExEditor-qq-msg-editor"


def _focus_and_report(provider: UiaProvider, el: Any, UIA: Any) -> None:
    print("-- state A: as found --")
    dump = _ranges(el, UIA)
    print(dump)

    print("\n-- placing focus with UIA SetFocus (no keystrokes) --")
    try:
        el.SetFocus()
        print("  SetFocus returned OK")
    except Exception as exc:  # noqa: BLE001 - reporting
        print(f"  SetFocus raised {type(exc).__name__}: {exc}")
        return

    time.sleep(0.3)

    print("\n-- state B: after focus --")
    print(_ranges(el, UIA))
    print(f"  HasKeyboardFocus now = {el.CurrentHasKeyboardFocus}")

    # The text pattern's own view of the world, for comparison: if the provider
    # reports an empty document, a NULL conversion target is expected and says
    # nothing about writability.
    try:
        text = el.GetCurrentPattern(UIA.UIA_TextPatternId).QueryInterface(
            UIA.IUIAutomationTextPattern
        )
        doc = text.DocumentRange
        print(f"  TextPattern document text = {doc.GetText(-1)!r}")
        sel = text.GetSelection()
        print(f"  TextPattern selection len = {sel.Length}")
        if sel.Length:
            r = sel.GetElement(0)
            print(f"  TextPattern selection text = {r.GetText(-1)!r}")
            print(f"  selection range has SetText = {hasattr(r, 'SetText')}")
    except Exception as exc:  # noqa: BLE001 - reporting
        print(f"  TextPattern read raised {type(exc).__name__}: {exc}")


def _ranges(el: Any, UIA: Any) -> str:
    try:
        pat = el.GetCurrentPattern(UIA.UIA_TextEditPatternId).QueryInterface(
            UIA.IUIAutomationTextEditPattern
        )
    except Exception as exc:  # noqa: BLE001 - reporting
        return f"  TextEditPattern unavailable: {type(exc).__name__}"

    lines = []
    for name in ("GetActiveComposition", "GetConversionTarget"):
        fn = getattr(pat, name, None)
        if fn is None:
            lines.append(f"  {name:<22} not implemented")
            continue
        try:
            rng = fn()
        except Exception as exc:  # noqa: BLE001 - reporting
            lines.append(f"  {name:<22} raised {type(exc).__name__}: {exc}")
            continue
        if rng is None:
            lines.append(f"  {name:<22} NULL (no composition active)")
            continue
        # A range came back: ask it what it covers, and whether it can write.
        info = [f"  {name:<22} range obtained"]
        info.append(f"      has SetText = {hasattr(rng, 'SetText')}")
        try:
            info.append(f"      text        = {rng.GetText(-1)!r}")
        except ValueError as exc:
            # ValueError here is the NULL-pointer signature: the range object
            # marshals but wraps a null underlying pointer, so every call that
            # dereferences it fails. Same shape as the report's
            # "ValueError: NULL COM pointer" for missing patterns.
            info.append(f"      GetText -> ValueError (NULL underlying pointer) {exc}")
        except Exception as exc:  # noqa: BLE001
            info.append(f"      GetText raised {type(exc).__name__}: {exc}")
        lines.append("\n".join(info))
    return "\n".join(lines)


def main() -> int:
    if not sys.platform.startswith("win"):
        print("Windows only")
        return 2

    provider = UiaProvider()
    try:
        windows = provider._executor.submit(provider._list_windows_sync).result(timeout=60)
        for w in windows:
            if "QQ" not in w.process:
                continue
            provider.snapshot(w.window_id)
            el = None
            for _rid, cand in provider._element_cache.items():
                try:
                    if TARGET_MATCH.lower() in (cand.CurrentClassName or "").lower():
                        el = cand
                        break
                except Exception:
                    continue
            if el is None:
                continue
            print(f"window {w.window_id} {w.title!r} ({w.process})")
            print(f"target class = {el.CurrentClassName!r}\n")
            provider._executor.submit(
                _focus_and_report, provider, el, provider._UIA
            ).result(timeout=60)
            return 0
        print("QQ input box not found")
    finally:
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
