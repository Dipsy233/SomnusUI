"""Extended write-route probe (closes the gaps in interface-capability-report.md).

The report's probe matrix answers two questions per pattern:

  1. does the element *claim* support (``IsXxxPatternAvailable``)?
  2. can the pattern *actually* be fetched (``GetCurrentPattern``)?

It does that for the first-generation ids only (Value, Text, LegacyIAccessible,
Invoke). This script adds what the report is missing, so that "we tried
everything" is a measured statement rather than a belief:

  * ``TextEditPattern`` (``UIA_TextEditPatternId``) - a *different* interface
    from ``TextPattern``. Chromium/Electron contenteditable editors commonly
    implement this one. The report fetched ``UIA_TextPatternId``, got a pattern
    back, and concluded "text interfaces are read-only". That conclusion only
    covers the read-only member of the family.
  * second-generation ids (``ValuePattern2``, ``TextPattern2``,
    ``TextEditPattern`` is itself the 8.1-era member).
  * the element's own ``CurrentNativeWindowHandle`` - the real precondition for
    ``WM_SETTEXT``. The report tested "is there a native Edit child window
    under the process", which is a different (and wrong) question.

Finally it *writes* through each route that looked usable and reads the value
back, because the report's own third law is that "the method exists" says
nothing until it is actually called (``LegacyIAccessible.SetValue`` is
fetchable and returns E_NOTIMPL).

Usage
-----
    python tools/probe_write_routes.py                # list candidate targets
    python tools/probe_write_routes.py --list         # same
    python tools/probe_write_routes.py --match ProseMirror
    python tools/probe_write_routes.py --window w3
    python tools/probe_write_routes.py --write        # actually attempt writes

Nothing here is imported by the runtime; it is a measurement tool, kept so the
next person does not have to re-derive these facts.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from ctypes import wintypes
from typing import Any

from somnus.providers.windows.uia import UiaProvider

# ---------------------------------------------------------------------------
# Pattern matrix: (label, IsXxxAvailable property, pattern id, interface, generation)
#
# Names are written out explicitly rather than composed with getattr(..., 0):
# property id 0 makes GetCurrentPropertyValue raise a COMError and would make
# every result look like "unsupported".
# ---------------------------------------------------------------------------

PROBE: list[tuple[str, str, str, str, str]] = [
    # -- first generation (what the report already covers) -------------------
    ("Value", "UIA_IsValuePatternAvailablePropertyId", "UIA_ValuePatternId",
     "IUIAutomationValuePattern", "1st"),
    ("Text", "UIA_IsTextPatternAvailablePropertyId", "UIA_TextPatternId",
     "IUIAutomationTextPattern", "1st"),
    ("LegacyIAccessible", "UIA_IsLegacyIAccessiblePatternAvailablePropertyId",
     "UIA_LegacyIAccessiblePatternId", "IUIAutomationLegacyIAccessiblePattern", "1st"),
    ("Invoke", "UIA_IsInvokePatternAvailablePropertyId", "UIA_InvokePatternId",
     "IUIAutomationInvokePattern", "1st"),
    # -- the gaps ------------------------------------------------------------
    # TextEditPattern is the writable member of the text family (Win8.1+).
    # It is a separate pattern id from TextPattern, and it is the one a
    # contenteditable editor tends to implement.
    ("TextEdit", "UIA_IsTextEditPatternAvailablePropertyId", "UIA_TextEditPatternId",
     "IUIAutomationTextEditPattern", "8.1"),
    ("Text2", "UIA_IsTextPattern2AvailablePropertyId", "UIA_TextPattern2Id",
     "IUIAutomationTextPattern2", "8.1"),
    # NOT probed: "ValuePattern2". There is no such pattern in UIA.
    #
    # This was checked against the local typelib rather than assumed: the full
    # UIA_*PatternId list contains no ValuePattern2, and there is no
    # IUIAutomationValuePattern2 interface. ValuePattern.SetValue is the only
    # value-writing pattern UIA has ever defined. An earlier draft of this
    # script probed for it anyway, which would have printed a fabricated
    # "MISSING" row that reads like a measurement but means nothing.
    #
    # The genuinely newer members of the family are TextPattern2 and
    # TransformPattern2 / SelectionPattern2 (both 8.1-era), not ValuePattern2.
]

#: Additional patterns that gate whether a native write path exists at all.
EXTRA_PROPERTIES: list[tuple[str, str]] = [
    ("NativeWindowHandle", "UIA_NativeWindowHandlePropertyId"),
    ("ControlType", "UIA_ControlTypePropertyId"),
    ("ClassName", "UIA_ClassNamePropertyId"),
    ("AutomationId", "UIA_AutomationIdPropertyId"),
    ("IsKeyboardFocusable", "UIA_IsKeyboardFocusablePropertyId"),
    ("IsEnabled", "UIA_IsEnabledPropertyId"),
    ("IsContentElement", "UIA_IsContentElementPropertyId"),
    ("ProcessId", "UIA_ProcessIdPropertyId"),
]


class _Probe:
    """Runs inside the provider's MTA worker thread."""

    def __init__(self, provider: UiaProvider, spec: str, el: Any = None, window_id: str = "") -> None:
        self.p = provider
        self.UIA = provider._UIA
        self.spec = spec
        self._el = el
        self.window_id = window_id

    # -- target selection ---------------------------------------------------

    def element(self) -> Any:
        """The element to probe.

        The live element is captured during discovery rather than read from
        ``provider._element_cache``: that cache is cleared at the start of every
        ``snapshot()`` (uia.py:618), so after snapshotting several windows it
        holds only the last window's elements. The report's reproduction script
        walked its own tree for the same reason.
        """
        return self._el

    # -- probing ------------------------------------------------------------

    def run(self, write: bool) -> None:
        el = self.element()
        if el is None:
            print(f"no element matching {self.spec!r} in the element cache")
            return

        print("=" * 72)
        print(f"TARGET  class={self._safe(el, 'CurrentClassName')!r}")
        print(f"        name ={self._safe(el, 'CurrentName')!r}")
        print("=" * 72)

        print("\n-- element properties --")
        for label, prop in EXTRA_PROPERTIES:
            value = self._prop(el, prop)
            if label == "NativeWindowHandle" and value:
                value = f"{value} (0x{int(value):X})"
            print(f"  {label:<22} = {value!r}")

        print("\n-- does it CLAIM support? --")
        claims: dict[str, bool | None] = {}
        for label, avail, _pid, _iface, gen in PROBE:
            claimed = self._prop(el, avail)
            claims[label] = claimed
            print(f"  {label:<18} [{gen}] available={claimed}")

        print("\n-- can the pattern ACTUALLY be fetched? (decisive) --")
        fetched: dict[str, Any] = {}
        for label, _avail, pid, iface, gen in PROBE:
            pattern, note = self._fetch(el, pid, iface)
            fetched[label] = pattern
            mark = "FETCHED" if pattern is not None else "no"
            print(f"  {label:<18} [{gen}] {mark:<8} {note}")

        # Follow-up reads on anything we did fetch.
        if fetched.get("Value") is not None:
            print(f"\n  Value.IsReadOnly = {self._safe(fetched['Value'], 'CurrentIsReadOnly')!r}")
        if fetched.get("TextEdit") is not None:
            # Verified outcome (see docs/interface-capability-report.md 2.9): on
            # Chromium this pattern fetches fine but its ranges are permanently
            # NULL and expose no SetText, so it is NOT a write route. Fetched
            # here does not mean writable - that is exactly the distinction the
            # report exists to keep clear.
            print("  TextEdit FETCHED -> present, but NOT necessarily writable:")
            print("      on Chromium its ranges are NULL with no SetText (report 2.9.2).")
            print("      run with --write to see the ranges for this element.")
        if fetched.get("Text") is not None:
            print(f"  Text.SupportedTextSelection = "
                  f"{self._safe(fetched['Text'], 'SupportedTextSelection')!r}")

        if not write:
            print("\n(pass --write to attempt actual writes and read back)")
            return

        self.attempt_writes(el, fetched)

    # -- writing ------------------------------------------------------------

    def attempt_writes(self, el: Any, fetched: dict[str, Any]) -> None:
        original = self._safe(el, "CurrentName")
        probe_text = "SomnusProbe"

        print("\n-- write attempts (each is read back) --")

        # 1. ValuePattern.SetValue
        if fetched.get("Value") is not None:
            self._report(
                "ValuePattern.SetValue",
                lambda: fetched["Value"].SetValue(probe_text),
            )
        else:
            print("  ValuePattern.SetValue          skipped (no pattern)")

        # 2. TextEditPattern - the writable member of the text family.
        #    Unlike TextPattern (read-only), TextEditPattern owns a *conversion
        #    target* range that Chromium maps onto the active editing region,
        #    and ITextRangeProvider exposes SetText on it.
        if fetched.get("TextEdit") is not None:
            pat = fetched["TextEdit"]
            for range_name in ("GetActiveComposition", "GetConversionTarget"):
                fn = getattr(pat, range_name, None)
                if fn is None:
                    print(f"  TextEdit.{range_name:<18} not implemented")
                    continue
                try:
                    rng = fn()
                except Exception as exc:  # noqa: BLE001 - reporting
                    print(f"  TextEdit.{range_name:<18} raised {type(exc).__name__}: {exc}")
                    continue
                if rng is None:
                    print(f"  TextEdit.{range_name:<18} returned NULL range")
                    continue
                # Does the range even expose a write method?
                has_set = hasattr(rng, "SetText")
                print(f"  TextEdit.{range_name:<18} range={rng!r} has SetText={has_set}")
                if has_set:
                    self._report(
                        f"TextEdit.{range_name}.SetText",
                        lambda r=rng: r.SetText(probe_text),
                    )

        # 2b. TextPattern's DocumentRange: the report noted it has no SetText.
        #     Re-checked here because the write method lives on the *range*
        #     object, and TextEditPattern hands out a different one.
        if fetched.get("Text") is not None:
            try:
                doc = fetched["Text"].DocumentRange
                print(f"  TextPattern.DocumentRange  has SetText = {hasattr(doc, 'SetText')}")
            except Exception as exc:  # noqa: BLE001 - reporting
                print(f"  TextPattern.DocumentRange  raised {type(exc).__name__}")

        # 3. WM_SETTEXT on the element's own HWND (not a child Edit search).

        hwnd = self._prop(el, "UIA_NativeWindowHandlePropertyId") or 0
        if hwnd:
            self._report(
                f"WM_SETTEXT(hwnd=0x{int(hwnd):X})",
                lambda: self._wm_settext(int(hwnd), probe_text),
            )
            self._report(
                f"EM_REPLACESEL(hwnd=0x{int(hwnd):X})",
                lambda: self._em_replacesel(int(hwnd), probe_text),
            )
        else:
            print("  WM_SETTEXT / EM_REPLACESEL   skipped (element has no HWND)")
            print("      NOTE: the report's precondition was 'a native Edit child "
                  "exists under the process'.")
            print("      The real precondition is this per-element HWND, and it is "
                  "not even stored in NativeRef today.")

        # 4. LegacyIAccessible.SetValue - the report saw E_NOTIMPL on QQ; confirm
        #    whether that is general or specific to that subtree.
        if fetched.get("LegacyIAccessible") is not None:
            self._report(
                "LegacyIAccessible.SetValue",
                lambda: fetched["LegacyIAccessible"].SetValue(probe_text),
            )

        # Read back whatever the control now reports.
        print(f"\n  name before = {original!r}")
        print(f"  name after  = {self._safe(el, 'CurrentName')!r}")
        if fetched.get("Value") is not None:
            print(f"  value after = {self._safe(fetched['Value'], 'CurrentValue')!r}")

    def _report(self, label: str, fn) -> None:
        try:
            fn()
            print(f"  {label:<30} OK (call returned)")
        except Exception as exc:  # noqa: BLE001 - reporting
            print(f"  {label:<30} {type(exc).__name__}: {exc}")

    # -- native helpers (run on the worker thread; no COM involved) ---------

    def _wm_settext(self, hwnd: int, text: str) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        WM_SETTEXT = 0x000C
        buf = ctypes.create_unicode_buffer(text)
        # SendMessageTimeout keeps a hung target from hanging the probe.
        result = ctypes.c_size_t()
        ok = user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd), WM_SETTEXT, 0, ctypes.byref(buf), 0x0002, 2000,
            ctypes.byref(result),
        )
        if not ok:
            raise OSError(f"SendMessageTimeout failed (err {ctypes.get_last_error()})")

    def _em_replacesel(self, hwnd: int, text: str) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        EM_SETSEL = 0x00B1
        EM_REPLACESEL = 0x00C2
        result = ctypes.c_size_t()
        # Select the whole content, then replace the selection.
        user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd), EM_SETSEL, 0, -1, 0x0002, 2000, ctypes.byref(result)
        )
        buf = ctypes.create_unicode_buffer(text)
        user32.SendMessageTimeoutW(
            wintypes.HWND(hwnd), EM_REPLACESEL, 1, ctypes.byref(buf), 0x0002, 2000,
            ctypes.byref(result),
        )

    # -- small helpers ------------------------------------------------------

    def _prop(self, el: Any, name: str) -> Any:
        pid = getattr(self.UIA, name, None)
        if pid is None:
            return "<not in this UIA version>"
        try:
            return el.GetCurrentPropertyValue(pid)
        except Exception as exc:  # noqa: BLE001 - reporting
            return f"<{type(exc).__name__}>"

    def _fetch(self, el: Any, pid_name: str, iface_name: str) -> tuple[Any, str]:
        pid = getattr(self.UIA, pid_name, None)
        if pid is None:
            return None, "<pattern id not in this UIA version>"
        iface = getattr(self.UIA, iface_name, None)
        if iface is None:
            return None, "<interface not in this typelib>"
        try:
            raw = el.GetCurrentPattern(pid)
        except Exception as exc:  # noqa: BLE001 - this is the measurement
            return None, f"{type(exc).__name__}"
        if raw is None:
            return None, "NULL pointer"
        try:
            return raw.QueryInterface(iface), "queried"
        except Exception as exc:  # noqa: BLE001
            return None, f"QueryInterface {type(exc).__name__}"

    def _safe(self, obj: Any, attr: str) -> Any:
        try:
            return getattr(obj, attr)
        except Exception as exc:  # noqa: BLE001
            return f"<{type(exc).__name__}>"


def find_candidates(provider: UiaProvider, window_id: str, spec: str | None,
                    match_class_only: bool = True) -> list[tuple[Any, str]]:
    """Walk one window's tree and return live elements matching *spec*.

    Returns ``(element, description)`` pairs. The live element comes from
    ``provider._element_cache`` *immediately after* this snapshot, before any
    further snapshot clears it.
    """

    def _walk() -> list[str]:
        rows: list[str] = []
        needle = (spec or "").lower()
        for runtime_id, el in provider._element_cache.items():
            try:
                cls = el.CurrentClassName or ""
                name = el.CurrentName or ""
                ctype = el.CurrentControlType
            except Exception:
                continue
            # Match on the CLASS name only by default. Matching the display name
            # is a trap: a pasted screenshot of this very report has the string
            # "ProseMirror" in its OCR'd name, and it matched first.
            hay = cls.lower() if match_class_only else f"{cls} {name}".lower()
            if needle:
                if needle not in hay:
                    continue
            elif not any(k in hay for k in CANDIDATE_HINTS):
                continue
            # A Group/Text node that merely *mentions* the class is content, not
            # the control. Skip huge names: a real editor is named with its
            # conversation, not with 2KB of OCR text.
            if len(name) > 200:
                continue
            rows.append(f"{'.'.join(map(str, runtime_id))}\t{ctype}\t{cls}\t{name[:60]}")
        return rows

    rows = provider._executor.submit(_walk).result(timeout=60)
    out: list[tuple[Any, str]] = []
    for row in rows:
        rid, ctype, cls, name = row.split("\t", 3)
        el = provider._element_cache.get(tuple(int(x) for x in rid.split(".")))
        if el is not None:
            out.append((el, f"ct={ctype} class={cls!r} name={name!r}"))
    return out


def list_candidates(provider: UiaProvider) -> None:
    """Show every element whose class/name suggests a text surface."""

    def _dump() -> list[str]:
        rows: list[str] = []
        for runtime_id, el in provider._element_cache.items():
            try:
                cls = el.CurrentClassName or ""
                name = el.CurrentName or ""
                ctype = el.CurrentControlType
            except Exception:
                continue
            if any(k in f"{cls} {name}".lower() for k in CANDIDATE_HINTS):
                rows.append(f"  {'.'.join(map(str, runtime_id)):<28} ct={ctype} "
                            f"class={cls!r} name={name[:48]!r}")
        return rows

    rows = provider._executor.submit(_dump).result(timeout=20)
    print(f"{len(rows)} cached candidate(s) (snapshot a window first):")
    for row in rows:
        print(row)


CANDIDATE_HINTS = (
    "edit", "prosemirror", "exeditor", "codemirror", "monaco", "richedit",
    "textbox", "contenteditable", "document",
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", help="only snapshot this window id (default: all)")
    parser.add_argument("--match", help="class/name substring to probe")
    parser.add_argument("--all", action="store_true", help="probe every match, not just the first")
    parser.add_argument("--any-field", dest="match_class_only", action="store_false",
                        help="match class OR name (default: class only, which avoids "
                             "matching OCR text in a pasted screenshot)")
    parser.set_defaults(match_class_only=True)
    parser.add_argument("--list", action="store_true", help="list candidate elements")
    parser.add_argument("--write", action="store_true", help="attempt writes")
    args = parser.parse_args(argv)

    if not sys.platform.startswith("win"):
        print("Windows only")
        return 2

    provider = UiaProvider()
    try:
        # snapshot() takes a concrete window id and raises KeyError(None) on
        # None, so enumerate first.
        windows = provider._executor.submit(provider._list_windows_sync).result(timeout=60)
        targets = [w for w in windows if args.window is None or w.window_id == args.window]
        if args.window and not targets:
            print(f"window {args.window!r} not found; available:")
            for w in windows:
                print(f"  {w.window_id:<4} {w.title!r} ({w.process})")
            return 2

        # Probe each window right after its own snapshot: the element cache is
        # cleared by the next snapshot (uia.py:618), so candidates must be used
        # while they are still live.
        probed = 0
        for w in targets:
            try:
                provider.snapshot(w.window_id)
            except Exception as exc:  # noqa: BLE001 - one dead window is fine
                print(f"skip {w.window_id}: {type(exc).__name__}: {exc}")
                continue

            found = find_candidates(provider, w.window_id, args.match, args.match_class_only)
            if args.list or not args.match:
                print(f"\n{w.window_id} {w.title!r} ({w.process}): {len(found)} candidate(s)")
                for _el, desc in found:
                    print(f"  {desc}")
                continue

            for el, desc in found:
                print(f"\n{w.window_id} {w.title!r}: {desc}")
                probe = _Probe(provider, args.match, el=el, window_id=w.window_id)
                provider._executor.submit(probe.run, args.write).result(timeout=120)
                probed += 1
                if not args.all:
                    break

        if args.match and probed == 0:
            print(f"\nno element matching {args.match!r} in any snapshotted window")
        elif not args.match:
            print("\npass --match <substring> to probe one element (add --all to probe every match)")
    finally:
        provider.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
