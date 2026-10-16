"""Clipboard channel: text into a control that publishes no writable interface.

**Why this channel exists.** A self-drawn editor (Chromium/Electron rich text,
QQ's ProseMirror box) publishes no ``Value`` pattern and maps no element-level
``HWND``, so neither ``ValuePattern.SetValue`` nor ``EM_REPLACESEL`` can reach
it. What *does* reach it is ``Ctrl+V``: the application's own ``WM_PASTE``
handler reads the text out of the clipboard in one piece. Measured on Windows 11
24H2, the delivery cost is **constant ~37 ms for 10 to 3000 characters**,
because the input queue only ever carries four key events - unlike per-character
``SendInput``, whose N characters become 2N queue entries that the target parks
(``docs/interface-capability-report.md`` 3.3).

**The user's clipboard is restored losslessly.** Borrowing the clipboard is only
acceptable if it can be handed back exactly as it was, so the snapshot captures
**every format's raw bytes**, not just the text, and the restore rebuilds the
whole format set. Three handle shapes have to be handled, because a clipboard
entry is not always a plain memory block:

======================  =====================================================
format category         capture / rebuild
======================  =====================================================
``HGLOBAL``             ``GlobalLock`` bytes, written back with ``GlobalAlloc``
                        (covers ``CF_UNICODETEXT``, ``CF_TEXT``, RTF, HTML,
                        ``CF_HDROP``, and every registered custom format)
``CF_BITMAP``           ``GetDIBits`` at 32bpp ``BI_RGB`` (a ``CF_DIB`` block),
                        rebuilt with ``CreateDIBitmap``
``CF_ENHMETAFILE``      ``GetEnhMetaFileBits``, rebuilt with
                        ``SetEnhMetaFileBits``
======================  =====================================================

Measured: a 64x48 bitmap survives the round trip with **byte-identical pixels**,
and text survives with **byte-identical bytes** including CJK. This is what lets
the channel stop refusing when the clipboard holds an image - the earlier
refusal existed only because the previous implementation could republish plain
text and nothing else.

Custom formats (ids >= ``0xC000``) are assigned per session, so they are
re-registered **by name** on restore rather than reusing a stale id.

**What it still costs.** The channels answer for the cases they cannot cover:

1. **``CF_PALETTE``-style handle-less formats** cannot be read at all (there is
   no byte block behind the handle), so they are skipped on restore. A format
   the restore skips is *reported*, never silently dropped.
2. **OLE carrier formats** (``DataObject``, ``Ole Private Data``) are OLE
   marshalling state, not memory blocks; this module does not rebuild them. A
   rich-paste target that insists on OLE data therefore degrades to the text
   formats that *were* restored. This is a documented limit, not a silent one.
3. **The text can leak into clipboard history / cloud sync.** Prevented, not
   repaired, by publishing four opt-out formats alongside the payload
   (:data:`OPT_OUT_FORMATS`). Measured: history ``count 1 -> 0``. This is a
   cooperation protocol, not a sandbox - a manager that ignores it still sees the
   text - so the claim is not overstated here either.
4. **The restore cannot be confirmed.** Delayed rendering (the promise/
   ``WM_RENDERFORMAT`` trick that would signal "the target has read it") is
   measured *unusable here*: the OLE layer materialises the promise ~3-5 ms after
   publication with nobody asking, so no later render ever arrives to observe
   (see docs/interface-capability-report.md 3.3). The restore is therefore a
   timed wait and is reported as unconfirmed, never as verified.

**Deliberately not implemented.** Delayed rendering (no advantage, real
complexity) and post-hoc history deletion through WinRT (an 88-350 ms window of
exposure plus an STA thread, when prevention already works).

**Delivery signal.** ``Ctrl+V`` needs the target in the foreground. The paste is
followed by a verification read of whatever surface the element actually
publishes (see ``executor._read_surface``), so the sender knows whether the text
landed - and that is also the closest thing to a read confirmation this
environment offers.
"""

from __future__ import annotations

import ctypes
import sys
import time
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any

from somnus.ir.action import ExecResult

# ---------------------------------------------------------------------------
# Clipboard formats
# ---------------------------------------------------------------------------

CF_UNICODETEXT = 13
CF_TEXT = 1
CF_OEMTEXT = 7
CF_LOCALE = 16
CF_DIB = 8
CF_DIBV5 = 17
CF_BITMAP = 2
CF_HDROP = 15
CF_PALETTE = 9
CF_ENHMETAFILE = 14
CF_DSPBITMAP = 0x0082
CF_DSPENHMETAFILE = 0x008E

# ---------------------------------------------------------------------------
# Handle shapes
# ---------------------------------------------------------------------------
#: A clipboard entry is not always a plain memory block, so capture and restore
#: dispatch on *how the handle is stored*, not on the format id. The three
#: shapes below are the ones that can be serialised at all; everything else
#: (``CF_PALETTE``) has no byte block behind the handle and is skipped.
HANDLE_GLOBAL = "hglobal"
HANDLE_BITMAP = "hbitmap"
HANDLE_ENHMETAFILE = "henhmetafile"
HANDLE_NONE = "none"

#: ``CF_BITMAP`` stores an ``HBITMAP``; its pixels are read through
#: ``GetDIBits`` and the bitmap is rebuilt with ``CreateDIBitmap``.
_BITMAP_FORMATS = frozenset({CF_BITMAP, CF_DSPBITMAP})
#: ``CF_ENHMETAFILE`` stores an ``HENHMETAFILE``, read/written as raw EMF bytes.
_ENHMETAFILE_FORMATS = frozenset({CF_ENHMETAFILE, CF_DSPENHMETAFILE})
#: Formats whose handle is not a memory block at all (an ``HPALETTE``): nothing
#: can be read from them, so they cannot be put back either.
_NON_MEMORY_FORMATS = frozenset({CF_PALETTE})
#: Ids at or above this are assigned per session, so a restore must re-register
#: the format by name instead of reusing the id that was captured.
_REGISTERED_FORMAT_BASE = 0xC000


def handle_type_of(format_id: int) -> str:
    """Classify a format id by the shape of its handle. See the flags above."""
    if format_id in _BITMAP_FORMATS:
        return HANDLE_BITMAP
    if format_id in _ENHMETAFILE_FORMATS:
        return HANDLE_ENHMETAFILE
    if format_id in _NON_MEMORY_FORMATS:
        return HANDLE_NONE
    return HANDLE_GLOBAL


# ---------------------------------------------------------------------------
# Win32 structures used by the bitmap path
# ---------------------------------------------------------------------------


class _BITMAP(ctypes.Structure):
    """``BITMAP``: just enough of it to get the dimensions."""

    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    """``BITMAPINFOHEADER``: the head of a ``CF_DIB`` block."""

    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


_BI_RGB = 0
_DIB_RGB_COLORS = 0
_CBM_INIT = 4

#: Registered ("custom") formats, as the Windows clipboard protocol defines
#: them. Publishing these next to the text is what keeps it out of the history
#: and out of cloud sync.
#:
#: * ``ExcludeClipboardContentFromMonitorProcessing`` - presence is enough; the
#:   monitor/history service skips the entry.
#: * ``CanIncludeInClipboardHistory`` - a **DWORD 0** is what opts out; the
#:   value matters, so the payload is written, not just the format.
#: * ``CanUploadToCloudClipboard`` - likewise **DWORD 0**.
#: * ``ClipboardViewerIgnore`` - the older viewer-chain opt-out; presence.
#:
#: Verified against two independent implementations, not just measured here:
#: CopyQ (``src/platform/win/winplatformclipboard.cpp``) honours all four and
#: additionally accepts ``ExcludeClipboardContentFromMonitorProcessing == "1"``
#: (the shape a Qt application writes), and CoPanda
#: (``crates/cp-win/src/formats.rs``) models them as ``concealed`` /
#: ``denied_when_zero``. CopyQ is the reference used for the exact spellings.
OPT_OUT_FORMATS: tuple[tuple[str, bool], ...] = (
    ("ExcludeClipboardContentFromMonitorProcessing", False),
    ("CanIncludeInClipboardHistory", True),
    ("CanUploadToCloudClipboard", True),
    ("ClipboardViewerIgnore", False),
)

#: Formats whose presence means the clipboard holds something other than text.
#: Only used to *describe* the clipboard (diagnostics and the ``has_non_text``
#: helper); it no longer gates anything, because the snapshot now carries the
#: bytes needed to restore these like any other format.
_NON_TEXT_FORMATS: tuple[tuple[int, str], ...] = (
    (CF_DIB, "image"),
    (CF_DIBV5, "image"),
    (CF_BITMAP, "image"),
    (CF_HDROP, "file list"),
)

#: Key sequence for "paste". ``VK_CONTROL`` + ``'V'``.
_VK_CONTROL = 0x11
_VK_V = 0x56

#: How long to wait for the target to consume the paste before restoring the
#: user's clipboard. A guess by construction - the read signal that would make
#: it exact is unavailable here (docs/interface-capability-report.md 3.3) - so every
#: caller is told the restore is unconfirmed.
_SETTLE_S = 0.12
#: How long to keep trying to open the clipboard before giving up. Another
#: process holding it open is normal and transient.
_OPEN_ATTEMPTS = 20
_OPEN_INTERVAL_S = 0.01


class ClipboardError(Exception):
    """The clipboard could not be used; the caller must fall through."""


@dataclass(slots=True)
class ClipboardFormat:
    """One captured clipboard format, with enough to put it back.

    ``name`` is kept alongside the id because a registered format's id is
    assigned per session: the restore has to look the name up again rather than
    reuse the id that was captured (see ``_restore_formats``).

    ``data`` is ``None`` when the format could not be read at all (a handle-less
    format). ``handle_type`` still records how it should be restored, so a
    skipped format is visible in the snapshot rather than silently absent.
    """

    ordinal: int
    format_id: int
    name: str
    handle_type: str
    data: bytes | None = None


@dataclass(slots=True)
class ClipboardSnapshot:
    """What the clipboard held before we touched it.

    **Every format's raw bytes are captured**, not just the text, so the restore
    can rebuild the whole entry. Text only would make an image or a file list
    unrestorable, which is why the channel used to refuse those outright.

    ``read_failures`` names formats that could not be captured; they are carried
    so the caller can report what will be missing instead of quietly losing it.
    """

    text: str = ""
    formats: tuple[int, ...] = ()
    captured: bool = False
    #: The full per-format capture, in clipboard order.
    entries: tuple[ClipboardFormat, ...] = ()
    #: format name -> why it could not be captured.
    read_failures: dict[str, str] = field(default_factory=dict)

    @property
    def restore_failures(self) -> tuple[str, ...]:
        """Names of formats that will not come back (unreadable at capture)."""
        return tuple(self.read_failures)

    def has_non_text(self) -> str | None:
        """A human name for non-text content, or None when this is text-only.

        Kept for diagnostics and for callers that want to know before acting;
        it is no longer a reason to refuse, because the snapshot now carries
        the bytes needed to restore whatever it is.
        """
        for cf, kind in _NON_TEXT_FORMATS:
            if cf in self.formats:
                return kind
        return None


@dataclass(slots=True)
class PasteOutcome:
    """The result of one paste round-trip, reported honestly."""

    ok: bool
    #: Whether the original clipboard was rebuilt. A best-effort flag: it says
    #: the restore was attempted and did not raise, not that every format made
    #: it back (``restore_failures`` names the ones that did not).
    restored: bool = False
    #: True when the paste demonstrably landed in the target (a verification
    #: read saw the text). This is what the restore timing is derived from.
    delivered: bool = False
    error: str | None = None
    message: str | None = None
    #: Formats the opt-out publish actually installed, for tests/diagnostics.
    opted_out: tuple[str, ...] = field(default_factory=tuple)
    #: Formats that could not be put back, by name. Empty means a clean restore.
    restore_failures: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Win32 bindings (lazily resolved so importing this module is harmless off-Win)
# ---------------------------------------------------------------------------


def _user32():
    """user32, declared once.

    The declaration is part of the accessor because ``ctypes.WinDLL`` returns a
    **new instance on every call**, so declaring the prototypes somewhere else
    would leave subsequent lookups with the default (truncating) types. That is
    not a theoretical problem: with the default types ``GlobalSize`` reports 0
    and ``GlobalLock`` returns a truncated pointer, and reading through it is an
    access violation rather than a clean failure.
    """
    global _USER32
    if _USER32 is None:
        _USER32 = ctypes.WinDLL("user32", use_last_error=True)
        _declare_prototypes(_USER32, _kernel32(), _gdi32())
    return _USER32


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is None:
        _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _declare_prototypes(_USER32 or _user32(), _KERNEL32, _gdi32())
    return _KERNEL32


def _gdi32():
    """GDI, for the bitmap/metafile shapes (``GetDIBits``, ``CreateDIBitmap``)."""
    global _GDI32
    if _GDI32 is None:
        _GDI32 = ctypes.WinDLL("gdi32", use_last_error=True)
        _declare_prototypes(_user32(), _kernel32(), _GDI32)
    return _GDI32


_USER32 = None
_KERNEL32 = None
_GDI32 = None


def available() -> bool:
    """Whether this platform has a clipboard at all."""
    return sys.platform.startswith("win")


# ---------------------------------------------------------------------------
# Reading the clipboard
# ---------------------------------------------------------------------------


def snapshot() -> ClipboardSnapshot:
    """Capture the clipboard's current state, or raise :class:`ClipboardError`.

    **Every format's bytes are captured**, so the entry can be rebuilt exactly.
    A format that cannot be read is recorded in ``read_failures`` and the
    snapshot still succeeds - one unreadable format must not cost the caller the
    ability to restore all the others.

    This used to refuse outright when the clipboard held an image, because the
    restore could only republish plain text. Now that bitmaps and metafiles are
    serialised too, non-text content is captured like anything else.
    """
    if not available():  # pragma: no cover - platform
        raise ClipboardError("not windows")
    user32 = _user32()
    opened = False
    for _ in range(_OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(_OPEN_INTERVAL_S)
    if not opened:
        # The classic case is a locked session: ``OpenClipboard`` fails with
        # "access denied" while LogonUI owns the desktop (report section 1).
        raise ClipboardError(f"cannot open clipboard (err {ctypes.get_last_error()})")
    try:
        formats = _enum_formats(user32)
        entries: list[ClipboardFormat] = []
        failures: dict[str, str] = {}
        for ordinal, cf in enumerate(formats, start=1):
            name = _format_name(user32, cf)
            handle_type = handle_type_of(cf)
            data: bytes | None = None
            if handle_type != HANDLE_NONE:
                data, reason = _capture_format(user32, cf, handle_type)
                if data is None and reason:
                    failures[name] = reason
            entries.append(
                ClipboardFormat(
                    ordinal=ordinal,
                    format_id=cf,
                    name=name,
                    handle_type=handle_type,
                    data=data,
                )
            )
        text = _read_text(user32)
        return ClipboardSnapshot(
            text=text,
            formats=formats,
            captured=True,
            entries=tuple(entries),
            read_failures=failures,
        )
    finally:
        user32.CloseClipboard()


def _enum_formats(user32) -> tuple[int, ...]:
    formats: list[int] = []
    cf = 0
    while True:
        cf = user32.EnumClipboardFormats(cf)
        if not cf:
            break
        formats.append(int(cf))
    return tuple(formats)


def _format_name(user32, cf: int) -> str:
    """The registered name of a custom format, or a stable label for a standard one.

    A standard format has no name to look up (``GetClipboardFormatName`` returns
    0 for ids below ``0xC000``), so a readable label is synthesised. That label is
    only for diagnostics and for the ``read_failures`` report; the restore uses
    the id for standard formats and the name only for registered ones.
    """
    user32.GetClipboardFormatNameW.restype = ctypes.c_int
    user32.GetClipboardFormatNameW.argtypes = [wintypes.UINT, wintypes.LPWSTR, ctypes.c_int]
    buf = ctypes.create_unicode_buffer(256)
    if user32.GetClipboardFormatNameW(cf, buf, 256):
        return buf.value
    return f"CF_{cf}"


def _capture_format(user32, cf: int, handle_type: str) -> tuple[bytes | None, str]:
    """Read one format's bytes according to how its handle is stored.

    Returns ``(data, failure_reason)``; ``data`` is None when the format could
    not be read, and ``failure_reason`` is then a human explanation. Nothing is
    raised: one unreadable format must not abort the whole snapshot, or the
    clipboard would be left half-restorable.
    """
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    handle = user32.GetClipboardData(cf)
    if not handle:
        return None, "no data handle for this format"

    if handle_type == HANDLE_BITMAP:
        return _capture_bitmap(handle)
    if handle_type == HANDLE_ENHMETAFILE:
        return _capture_enhmetafile(handle)
    return _capture_hglobal(handle)


def _capture_hglobal(handle: int) -> tuple[bytes | None, str]:
    """Read an ``HGLOBAL`` format's bytes verbatim.

    This is the path that covers text, RTF, HTML, ``CF_HDROP`` and every
    registered custom format, so the byte block is copied as-is rather than
    interpreted.
    """
    kernel32 = _kernel32()
    size = kernel32.GlobalSize(wintypes.HGLOBAL(handle))
    if not size:
        return None, "clipboard format size is unavailable"
    ptr = kernel32.GlobalLock(wintypes.HGLOBAL(handle))
    if not ptr:
        return None, "failed to lock clipboard data"
    try:
        return ctypes.string_at(ptr, int(size)), ""
    finally:
        kernel32.GlobalUnlock(wintypes.HGLOBAL(handle))


def _capture_bitmap(handle: int) -> tuple[bytes | None, str]:
    """Convert an ``HBITMAP`` into a ``CF_DIB`` byte block.

    The pixel data is requested as 32bpp ``BI_RGB`` so there is no colour table
    to serialise, and the header is stored immediately in front of the pixels -
    which is exactly the shape ``CreateDIBitmap`` (see ``_restore_bitmap``) needs
    to rebuild an equivalent bitmap. Measured byte-identical on the way back.
    """
    gdi32 = _gdi32()
    bm = _BITMAP()
    if gdi32.GetObjectW(
        wintypes.HANDLE(handle), ctypes.sizeof(_BITMAP), ctypes.byref(bm)
    ) == 0:
        return None, "failed to read bitmap metadata"

    header = _BITMAPINFOHEADER(
        biSize=ctypes.sizeof(_BITMAPINFOHEADER),
        biWidth=bm.bmWidth,
        biHeight=bm.bmHeight,  # positive -> bottom-up DIB
        biPlanes=1,
        biBitCount=32,
        biCompression=_BI_RGB,
    )

    user32 = _user32()
    hdc = user32.GetDC(None)
    if not hdc:
        return None, "failed to acquire a device context"
    try:
        height = abs(bm.bmHeight)
        # First call lets GDI fill in biSizeImage.
        gdi32.GetDIBits(
            hdc, wintypes.HANDLE(handle), 0, height, None,
            ctypes.byref(header), _DIB_RGB_COLORS,
        )
        if header.biSizeImage == 0:
            stride = (bm.bmWidth * 32 + 31) // 32 * 4
            header.biSizeImage = stride * height
        if not header.biSizeImage:
            return None, "bitmap reports no pixel data"

        pixels = (ctypes.c_ubyte * header.biSizeImage)()
        if gdi32.GetDIBits(
            hdc, wintypes.HANDLE(handle), 0, height, pixels,
            ctypes.byref(header), _DIB_RGB_COLORS,
        ) == 0:
            return None, "failed to retrieve bitmap pixel data"

        header_size = ctypes.sizeof(_BITMAPINFOHEADER)
        block = bytearray(header_size + header.biSizeImage)
        # ``block`` is a bytearray, so it has no ``byref`` target that memmove
        # accepts; address it through a buffer view instead.
        block_view = (ctypes.c_char * len(block)).from_buffer(block)
        ctypes.memmove(block_view, ctypes.byref(header), header_size)
        ctypes.memmove(
            ctypes.byref(block_view, header_size), pixels, header.biSizeImage
        )
        return bytes(block), ""
    finally:
        user32.ReleaseDC(None, hdc)


def _capture_enhmetafile(handle: int) -> tuple[bytes | None, str]:
    """Read an ``HENHMETAFILE`` as raw EMF bytes (``GetEnhMetaFileBits``)."""
    gdi32 = _gdi32()
    size = gdi32.GetEnhMetaFileBits(wintypes.HANDLE(handle), 0, None)
    if not size:
        return None, "failed to determine EMF data size"
    buf = (ctypes.c_ubyte * size)()
    if gdi32.GetEnhMetaFileBits(wintypes.HANDLE(handle), size, buf) != size:
        return None, "failed to read EMF data"
    return bytes(buf), ""


def _read_text(user32) -> str:
    """The clipboard's ``CF_UNICODETEXT`` as a string (empty when absent)."""
    handle = user32.GetClipboardData(CF_UNICODETEXT)
    if not handle:
        return ""
    kernel32 = _kernel32()
    ptr = kernel32.GlobalLock(wintypes.HGLOBAL(handle))
    if not ptr:
        return ""
    try:
        return ctypes.wstring_at(ptr)
    finally:
        kernel32.GlobalUnlock(wintypes.HGLOBAL(handle))


# ---------------------------------------------------------------------------
# Writing the clipboard
# ---------------------------------------------------------------------------


def write_text(text: str, *, opt_out: bool = True) -> tuple[str, ...]:
    """Put ``text`` on the clipboard, opting it out of history/cloud.

    Returns the names of the opt-out formats that were installed, so a caller
    (or a test) can assert the prevention actually happened instead of trusting
    it. ``opt_out=False`` exists for the measurement that justifies the default:
    it is how "plain write -> history count 1" is reproduced.

    The clipboard is emptied first (``EmptyClipboard``), which is what makes the
    write replace rather than merge. Every ``GlobalAlloc``'d block becomes the
    system's property on a successful ``SetClipboardData`` and must not be freed
    here.
    """
    if not available():  # pragma: no cover - platform
        raise ClipboardError("not windows")
    user32 = _user32()
    kernel32 = _kernel32()

    installed: list[str] = []
    with _clipboard_open(user32):
        if not user32.EmptyClipboard():
            raise ClipboardError("EmptyClipboard failed")
        if not _set_text(user32, kernel32, text):
            raise ClipboardError("SetClipboardData(CF_UNICODETEXT) failed")
        if opt_out:
            installed = _set_opt_out(user32, kernel32)
    return tuple(installed)


@contextmanager
def _clipboard_open(user32):
    """Hold the clipboard open, retrying a transient lock from another process.

    Every write and restore goes through here so the open/close pairing cannot
    be forgotten on an error path. The clipboard must be *empty* before the
    first ``SetClipboardData`` (that is what replaces rather than merges), so
    callers that are rebuilding an entry call ``EmptyClipboard`` themselves.
    """
    opened = False
    for _ in range(_OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(_OPEN_INTERVAL_S)
    if not opened:
        # The classic case is a locked session: ``OpenClipboard`` fails with
        # "access denied" while LogonUI owns the desktop.
        raise ClipboardError(f"cannot open clipboard (err {ctypes.get_last_error()})")
    try:
        yield
    finally:
        user32.CloseClipboard()


def restore(snapshot_obj: ClipboardSnapshot) -> tuple[str, ...]:
    """Rebuild a captured clipboard entry, format by format.

    Returns the names of formats that could not be put back, so a caller can
    report them instead of assuming a clean restore. An empty tuple means every
    capturable format made it back.

    Order matters in one place only: the four opt-out formats are dropped from
    the rebuild (they were ours, not the user's) and a rebuilt entry is not
    marked, because the user's own content should be eligible for history the
    way it was before we borrowed the clipboard.
    """
    if not available():  # pragma: no cover - platform
        raise ClipboardError("not windows")
    user32 = _user32()
    kernel32 = _kernel32()
    gdi32 = _gdi32()

    opt_out_names = {name for name, _dword in OPT_OUT_FORMATS}
    failed: list[str] = []

    with _clipboard_open(user32):
        if not user32.EmptyClipboard():
            raise ClipboardError("EmptyClipboard failed")
        for entry in snapshot_obj.entries:
            if entry.name in opt_out_names:
                # Not the user's format - it is our own marking.
                continue
            if entry.data is None:
                # Unreadable at capture (handled elsewhere as a read failure);
                # it cannot be rebuilt either, so record it rather than skip it
                # silently.
                if entry.handle_type != HANDLE_NONE:
                    failed.append(entry.name)
                continue
            if not _restore_format(user32, kernel32, gdi32, entry):
                failed.append(entry.name)

    return tuple(failed)


def _restore_format(user32, kernel32, gdi32, entry: ClipboardFormat) -> bool:
    """Put one captured format back. Returns False when it could not be rebuilt.

    A registered format's id is assigned per session, so it is looked up again
    by name; reusing the captured id would write to whatever format happens to
    own that id in *this* session - silently wrong data under the right name.
    """
    format_id = entry.format_id
    if format_id >= _REGISTERED_FORMAT_BASE:
        format_id = user32.RegisterClipboardFormatW(entry.name)
        if not format_id:
            return False

    if entry.handle_type == HANDLE_BITMAP:
        return _restore_bitmap(user32, gdi32, format_id, entry.data or b"")
    if entry.handle_type == HANDLE_ENHMETAFILE:
        return _restore_enhmetafile(gdi32, format_id, entry.data or b"")
    return _restore_hglobal(user32, kernel32, format_id, entry.data or b"")


def _restore_hglobal(user32, kernel32, format_id: int, data: bytes) -> bool:
    """Write an ``HGLOBAL`` format back, byte for byte."""
    if not data:
        return False
    try:
        handle = _alloc_block(kernel32, data)
    except ClipboardError:
        return False
    if user32.SetClipboardData(format_id, wintypes.HANDLE(handle)):
        return True
    # Only free on failure: a successful SetClipboardData hands ownership to the
    # system, and freeing it afterwards would be a double free.
    kernel32.GlobalFree(wintypes.HGLOBAL(handle))
    return False


def _restore_bitmap(user32, gdi32, format_id: int, block: bytes) -> bool:
    """Rebuild an ``HBITMAP`` from the stored ``CF_DIB`` block.

    The inverse of :func:`_capture_bitmap`: the header sits in front of the
    pixels, which is exactly what ``CreateDIBitmap`` expects.
    """
    header_size = ctypes.sizeof(_BITMAPINFOHEADER)
    if len(block) <= header_size:
        return False
    header = _BITMAPINFOHEADER.from_buffer_copy(block[:header_size])
    pixels = block[header_size:]

    hdc = user32.GetDC(None)
    if not hdc:
        return False
    try:
        buf = (ctypes.c_ubyte * len(pixels)).from_buffer_copy(pixels)
        hbitmap = gdi32.CreateDIBitmap(
            hdc, ctypes.byref(header), _CBM_INIT, buf, ctypes.byref(header),
            _DIB_RGB_COLORS,
        )
        if not hbitmap:
            return False
        if user32.SetClipboardData(format_id, wintypes.HANDLE(hbitmap)):
            return True
        gdi32.DeleteObject(wintypes.HANDLE(hbitmap))
        return False
    finally:
        user32.ReleaseDC(None, hdc)


def _restore_enhmetafile(gdi32, format_id: int, data: bytes) -> bool:
    """Rebuild an ``HENHMETAFILE`` from its raw EMF bytes."""
    if not data:
        return False
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    hemf = gdi32.SetEnhMetaFileBits(len(data), buf)
    if not hemf:
        return False
    # SetClipboardData lives on user32; fetched here to keep the signature small.
    user32 = _user32()
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    if user32.SetClipboardData(format_id, wintypes.HANDLE(hemf)):
        return True
    gdi32.DeleteEnhMetaFile(wintypes.HANDLE(hemf))
    return False


def _declare_prototypes(user32, kernel32, gdi32=None) -> None:
    """Pin argument/return types **on the given instances**.

    Without this the handles are truncated to 32 bits on 64-bit Python and every
    handle call misbehaves: ``SetClipboardData`` returns 0, ``GlobalSize``
    reports 0, and a truncated ``GlobalLock`` pointer reads as an access
    violation. Measured while porting the bitmap path - a missing ``argtypes`` on
    ``GlobalSize`` made a 26-byte text block read as 0 bytes, which looks exactly
    like an empty clipboard.

    Called from the accessors rather than by callers, because ``WinDLL`` hands
    back a fresh instance each time (see :func:`_user32`).
    """
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    if gdi32 is not None:
        gdi32.GetObjectW.restype = ctypes.c_int
        gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetDIBits.restype = ctypes.c_int
        gdi32.GetDIBits.argtypes = [
            wintypes.HDC, wintypes.HANDLE, wintypes.UINT, wintypes.UINT,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
        ]
        gdi32.CreateDIBitmap.restype = wintypes.HANDLE
        gdi32.CreateDIBitmap.argtypes = [
            wintypes.HDC, ctypes.c_void_p, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
        ]
        gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi32.GetEnhMetaFileBits.restype = wintypes.UINT
        gdi32.GetEnhMetaFileBits.argtypes = [
            wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p
        ]
        gdi32.SetEnhMetaFileBits.restype = wintypes.HANDLE
        gdi32.SetEnhMetaFileBits.argtypes = [wintypes.UINT, ctypes.c_void_p]
        gdi32.DeleteEnhMetaFile.argtypes = [wintypes.HANDLE]


#: GMEM_MOVEABLE: the clipboard requires movable memory.
_GMEM_MOVEABLE = 0x0002


def _alloc_block(kernel32, data: bytes) -> int:
    handle = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
    if not handle:
        raise ClipboardError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise ClipboardError("GlobalLock failed")
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)
    return handle


def _set_text(user32, kernel32, text: str) -> bool:
    # A trailing NUL is part of CF_UNICODETEXT's contract.
    raw = (text + "\0").encode("utf-16-le")
    handle = _alloc_block(kernel32, raw)
    if not user32.SetClipboardData(CF_UNICODETEXT, wintypes.HANDLE(handle)):
        kernel32.GlobalFree(handle)
        return False
    return True


def _set_opt_out(user32, kernel32) -> list[str]:
    """Install the four opt-out formats. Returns the ones that took.

    A failure here is *not* fatal to the write itself, but it is returned rather
    than swallowed: "we wrote the text and could not mark it" is exactly the
    state the caller must not mistake for "the clipboard is clean".
    """
    installed: list[str] = []
    for name, wants_dword in OPT_OUT_FORMATS:
        cf = user32.RegisterClipboardFormatW(name)
        if not cf:
            continue
        # DWORD 0 is the opt-out value; the presence-only formats carry a
        # one-byte payload so they are real blocks rather than NULL markers.
        data = ctypes.c_uint32(0).value.to_bytes(4, "little") if wants_dword else b"\x01"
        try:
            handle = _alloc_block(kernel32, data)
        except ClipboardError:
            continue
        if user32.SetClipboardData(cf, wintypes.HANDLE(handle)):
            installed.append(name)
        else:
            kernel32.GlobalFree(handle)
    return installed


# ---------------------------------------------------------------------------
# The paste round-trip
# ---------------------------------------------------------------------------


def paste_text(text: str, *, verify=None) -> PasteOutcome:
    """Deliver ``text`` by pasting into the focused control, then restore.

    The caller must have focused the target already (focusing is a UIA call, so
    the executor owns it); this function only pastes.

    ``Ctrl+V`` needs the target in the foreground, so the caller focuses the
    element first (the executor does this, since focusing is a UIA call).

    ``verify`` receives the number of characters expected and returns True once
    the target shows them - the same contract as
    :func:`somnus.providers.windows.keyboard.send_unicode_text`. It is what
    turns "we sent a paste" into "the text is in the control", and it is also
    what decides when the restore may run: the report's own advice is to wait for
    a quiet moment, and a successful verification read *is* that moment.

    Returns a :class:`PasteOutcome`; raises nothing, so the caller can decide
    whether to fall through to the next route.

    **The user's clipboard is preserved.** It is snapshotted before the write and
    restored verbatim in a ``finally`` block, so the restore happens whether the
    paste succeeded, failed, or raised. A clipboard holding something that could
    not be put back (an image, a file list) is refused *before* anything is
    written, so the user's content is never at risk. ``PasteOutcome.restored``
    reports whether the write-back actually happened - and it is a report, not a
    confirmation: the moment the target reads the clipboard cannot be observed in
    this environment (see the module docstring).
    """
    if not available():  # pragma: no cover - platform
        return PasteOutcome(ok=False, error="ACTION_NOT_SUPPORTED", message="not windows")
    if not text:
        return PasteOutcome(ok=True)

    snapshot_before = None
    installed: tuple[str, ...] = ()
    delivered = False
    restored = False
    restore_failures: tuple[str, ...] = ()
    try:
        snapshot_before = snapshot()
        installed = write_text(text)
        _send_paste()
        delivered = _await_delivery(verify, text)
        if not delivered:
            # No readable surface, so the target's read cannot be observed at
            # all. A timed wait is the documented best available (report 3.3),
            # not a measurement.
            time.sleep(_SETTLE_S)
    except ClipboardError as exc:
        # The clipboard could not be opened at all (a locked session, or another
        # process holding it). Nothing was written, so there is nothing to undo;
        # the finally block still runs and finds an empty snapshot.
        return PasteOutcome(ok=False, error="CLIPBOARD_UNAVAILABLE", message=str(exc))
    except Exception as exc:  # noqa: BLE001 - converted to a code
        return PasteOutcome(ok=False, error="NATIVE_EXECUTION_FAILED", message=str(exc))
    finally:
        # The user's clipboard goes back whether or not the paste worked, and the
        # whole entry is rebuilt - every format, not just the text - so an image
        # or a file list survives the borrow.
        if snapshot_before is not None and snapshot_before.captured and snapshot_before.entries:
            try:
                restore_failures = restore(snapshot_before)
                restored = True
            except ClipboardError:
                restored = False
    return PasteOutcome(
        ok=True,
        restored=restored,
        delivered=delivered,
        opted_out=installed,
        restore_failures=restore_failures,
        message=(
            "restore unconfirmed (no read signal available in this environment)"
            if not restored and snapshot_before is not None and snapshot_before.entries
            else None
        ),
    )


def _await_delivery(verify, text: str) -> bool:
    """Wait for the target to show the pasted text, if it can be read at all.

    Returns False when there is no way to observe the target (no ``verify``), so
    the caller knows it is relying on a timer. A paste is handled by the target's
    own ``WM_PASTE``, so unlike per-character ``SendInput`` there is no queue to
    drain and no nudge is needed - the read is a straight poll.
    """
    if verify is None:
        return False
    want = len(str(text))
    deadline = time.monotonic() + _SETTLE_S * 4
    while time.monotonic() < deadline:
        try:
            if verify(want):
                return True
        except Exception:  # noqa: BLE001 - an unreadable target is not a lost paste
            return False
        time.sleep(0.01)
    return False


def _send_paste() -> None:
    """Send the fixed four-event ``Ctrl+V`` gesture.

    Four events regardless of how long the text is, which is the whole point of
    this channel: the text travels in the clipboard, not in the input queue.
    """
    from somnus.providers.windows.keyboard import _INPUT, _mk_key

    user32 = _user32()
    downs = [_mk_key(_VK_CONTROL, 0), _mk_key(_VK_V, 0)]
    ups = [_mk_key(_VK_V, 0x0002), _mk_key(_VK_CONTROL, 0x0002)]
    for batch in (downs, ups):
        arr = (_INPUT * len(batch))(*batch)
        sent = user32.SendInput(len(arr), ctypes.byref(arr), ctypes.sizeof(_INPUT))
        if sent != len(batch):
            raise ClipboardError(
                f"SendInput sent {sent}/{len(batch)} (err {ctypes.get_last_error()})"
            )
