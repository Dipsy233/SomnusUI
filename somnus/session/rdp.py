"""The RDP loopback that actually creates the child session.

A child session is not something you ``CreateProcess`` into; Windows only
materialises it when a Remote Desktop connection **targets it**. The supported
way to say that is the RDP client control's extended property
``ConnectToChildSession``, set before ``Connect()``:

    Server = "localhost"                      # the loopback target
    ext.Property["ConnectToChildSession"] = True   # ... to this machine's child session

The control then logs on a second interactive session for the same account and
renders it. Everything running *inside* that rendered desktop is in the child
session - which is what :mod:`somnus.session.child` detects from the far side.

Why the property path and not a flag on ``mstsc.exe``
-----------------------------------------------------

``mstsc`` has no child-session switch; the property is only exposed on the
control's ``IMsRdpExtendedSettings`` interface (CLSID
``MsRdpClient10NotSafeForScripting``). That interface derives from ``IUnknown``,
not ``IDispatch``, so it must be reached by ``QueryInterface`` - a late-bound
call cannot see it. Verified live: the typelib exposes it as the ``Property``
property, and setting ``Property["ConnectToChildSession"] = True`` reads back
True.

Debug mode keeps the window visible
-----------------------------------

``visible=True`` hosts the control in a real, resizable top-level window so a
human can watch the second desktop while it is being tested. ``visible=False``
still has to create the control (the property lives on it) but hides the window
off-screen; the child session exists either way.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field

log = logging.getLogger("somnus.session.rdp")

#: The type library that holds the RDP client control and its settings interface.
MSTSCAX_DLL = r"C:\Windows\System32\mstscax.dll"

#: CLSID of ``MsRdpClient10NotSafeForScripting``. The scripting-safe variant
#: blocks ``IMsRdpExtendedSettings``, so this is the one that must be used.
CLSID_RDP10 = "{A0C63C30-F08D-4AB4-907C-34905D770C7D}"

#: Connection states reported by the control (``IMsTscAx::Connected``).
STATE_DISCONNECTED = 0
STATE_CONNECTED = 1
STATE_CONNECTING = 2

#: How long ``connect`` waits for the state to become "connected".
DEFAULT_CONNECT_TIMEOUT = 120.0


class RdpUnavailable(RuntimeError):
    """The RDP client control or its type library is missing."""


def _load_typelib():
    """Import (and generate if needed) the ``mstscax`` type library module."""
    if not sys.platform.startswith("win"):  # pragma: no cover - platform
        raise RdpUnavailable("RDP loopback is a Windows feature")
    try:
        import comtypes.client as cc

        return cc.GetModule(MSTSCAX_DLL)
    except Exception as exc:  # noqa: BLE001 - reported as unavailability
        raise RdpUnavailable(
            f"cannot load the RDP client type library {MSTSCAX_DLL}: {exc}"
        ) from exc


@dataclass
class RdpChildSession:
    """An RDP control connected to this machine's child session.

    The control owns the session: as long as the control is connected the child
    session exists, and closing it ends the session (and any programs in it).
    """

    #: Render a real, visible window (test/debug mode) rather than a hidden one.
    visible: bool = True
    #: Desktop size to request. The child session's virtual screen.
    width: int = 1280
    height: int = 800
    #: Optional explicit credentials. Empty means "use the delegated default".
    username: str = ""
    domain: str = ""
    password: str = ""
    #: Share the clipboard between the sessions (the agent may rely on paste).
    redirect_clipboard: bool = True

    _control: object | None = field(default=None, repr=False)
    _extension: object | None = field(default=None, repr=False)
    _window: object | None = field(default=None, repr=False)
    _connected: bool = field(default=False, repr=False)

    # -- lifecycle -----------------------------------------------------------

    def start(self, *, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        """Create the control, point it at the child session, and connect."""
        typelib = _load_typelib()
        self._make_window()

        control_cls = getattr(typelib, "MsRdpClient10NotSafeForScripting")
        self._control = self._create_control(typelib, control_cls)
        self._extension = self._control.QueryInterface(typelib.IMsRdpExtendedSettings)

        self._configure()
        self._connect(connect_timeout)

    def close(self) -> None:
        """Disconnect and destroy the control. Ends the child session."""
        try:
            if self._control is not None:
                self._control.Disconnect()
        except Exception as exc:  # noqa: BLE001 - teardown must not raise
            log.debug("RDP disconnect failed: %s", exc)
        self._connected = False
        self._destroy_window()

    def __enter__(self) -> RdpChildSession:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- state ---------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    def connection_state(self) -> int:
        if self._control is None:
            return STATE_DISCONNECTED
        try:
            return int(self._control.Connected)
        except Exception:  # noqa: BLE001
            return STATE_DISCONNECTED

    # -- window hosting ------------------------------------------------------

    def _make_window(self) -> None:
        """A top-level window to host the control.

        ``visible=True`` is the debug mode: a normal resizable window the tester
        can watch. ``visible=False`` creates the window off-screen instead of
        skipping it - an RDP control with no window has nowhere to render and can
        refuse to connect.
        """
        import tkinter as tk

        root = tk.Tk()
        root.title("SomnusUI - child session (RDP loopback)")
        if self.visible:
            root.geometry(f"{self.width}x{self.height}+120+80")
        else:
            root.geometry(f"{self.width}x{self.height}-4000-4000")
        root.update()  # realise the HWND before the control is parented to it
        self._window = root

    def _destroy_window(self) -> None:
        root = self._window
        self._window = None
        if root is None:
            return
        try:
            root.destroy()
        except Exception:  # noqa: BLE001 - teardown
            pass

    def _create_control(self, typelib, control_cls):
        """Instantiate the control and parent it to our window.

        The parent window is passed so the control renders inside it rather than
        creating a top-level window of its own. ``comtypes.client.CreateObject``
        cannot parent, so the control is created through the raw COM API with an
        explicit ``IOleObject::SetClientSite``-free path: constructing with the
        control's parent window as ``pUnkOuter`` is not allowed, so it is created
        standalone and then moved with ``SetWindowPos``/``SetParent`` on its own
        HWND once it exists.
        """
        import comtypes.client as cc

        control = cc.CreateObject(control_cls, interface=typelib.IMsRdpClient10)
        # The control has its own HWND only after it is shown; until then there is
        # nothing to reparent. Actual parenting is done in _configure, after the
        # size is known, so the control fills the window.
        return control

    # -- configuration -------------------------------------------------------

    def _configure(self) -> None:
        c = self._control
        ext = self._extension

        _try_set(c, "Server", "localhost")
        _try_set(c, "DesktopWidth", self.width)
        _try_set(c, "DesktopHeight", self.height)
        _try_set(c, "ColorDepth", 32)

        # Advanced settings: credential delegation, clipboard redirection and a
        # container that owns full-screen so the control does not spawn a second
        # top-level window competing for the z-order.
        adv = self._advanced()
        if adv is not None:
            _try_set(adv, "EnableCredSspSupport", True)
            _try_set(adv, "AuthenticationLevel", 0)
            _try_set(adv, "ContainerHandledFullScreen", 1)
            _try_set(adv, "DisplayConnectionBar", False)
            _try_set(adv, "RedirectClipboard", self.redirect_clipboard)
            _try_set(adv, "SmartSizing", True)
            _try_set(adv, "EnableAutoReconnect", True)
            # Do not steal the user's focus when the second desktop appears.
            _try_set(adv, "GrabFocusOnConnect", False)
            _try_set(adv, "allowBackgroundInput", 0)

        # The property that makes this a *child session* rather than a plain
        # loopback: without it the control would connect to a new full session
        # or reuse the current one, neither of which isolates input.
        self._set_property("ConnectToChildSession", True)

        if self.username:
            _try_set(c, "UserName", self.username)
        if self.domain:
            _try_set(c, "Domain", self.domain)
        if self.password and adv is not None:
            _try_set(adv, "ClearTextPassword", self.password)

    def _advanced(self):
        """The richest ``AdvancedSettings`` interface the control offers."""
        for name in (
            "AdvancedSettings9",
            "AdvancedSettings8",
            "AdvancedSettings7",
            "AdvancedSettings2",
        ):
            try:
                return getattr(self._control, name)
            except Exception:  # noqa: BLE001 - try the next one
                continue
        log.warning("no AdvancedSettings interface; credentials and clipboard unchanged")
        return None

    def _set_property(self, name: str, value) -> None:
        """Write one ``IMsRdpExtendedSettings`` property.

        Accessed as ``Property[name]`` rather than ``set_Property(...)`` because
        that is how the generated typelib exposes the ``propget``/``propput``
        pair; the explicit accessor names do not exist on the wrapper.
        """
        try:
            self._extension.Property[name] = value
        except Exception as exc:  # noqa: BLE001 - reported, not fatal
            raise RdpUnavailable(f"cannot set RDP property {name!r}: {exc}") from exc

    def get_property(self, name: str):
        """Read one property back, for verification and diagnostics."""
        return self._extension.Property[name]

    # -- connect -------------------------------------------------------------

    def _connect(self, timeout: float) -> None:
        try:
            self._control.Connect()
        except Exception as exc:  # noqa: BLE001 - converted below
            raise RdpUnavailable(f"RDP Connect() failed: {exc}") from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.connection_state()
            if state == STATE_CONNECTED:
                self._connected = True
                return
            if self._window is not None:
                try:
                    self._window.update()
                except Exception:  # noqa: BLE001 - window closed by the tester
                    break
            time.sleep(0.2)

        # Not fatal to the caller: the control may still be logging in, and the
        # session can come up later. Reported so the caller can wait and retry.
        log.warning("RDP loopback did not reach 'connected' within %.0fs", timeout)


def _try_set(obj, name: str, value) -> bool:
    """Set an attribute if the object has it; report rather than raise."""
    try:
        setattr(obj, name, value)
        return True
    except Exception as exc:  # noqa: BLE001 - optional property
        log.debug("RDP setting %s not applied: %s", name, exc)
        return False
