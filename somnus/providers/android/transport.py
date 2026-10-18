"""Transport to the Somnus Portal app (JSON-RPC over WebSocket).

Design (SomnusDroid doc §3, §4.2):

* the Portal listens on the phone; the host reaches it through
  ``adb forward tcp:<local> tcp:<remote>``;
* every call is one JSON-RPC request over a WebSocket, synchronous from the
  runtime's point of view (the Session is synchronous);
* dependency-free: stdlib ``socket`` speaking the minimal WebSocket framing
  for *client -> server* messages and parsing server frames. A real
  deployment can swap in the ``websockets`` package; the interface here is
  what stays fixed.

Methods the Portal implements (doc §4.2):
``snapshot``, ``perform``, ``global``, ``gesture``, ``launch``, ``apps``,
``events``, ``clipboard.set``, ``ping``.

Only the loopback is bound - the trust boundary is the adb forward itself.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import time
from typing import Any

DEFAULT_REMOTE_PORT = 8677
DEFAULT_LOCAL_PORT = 18677


class PortalError(RuntimeError):
    """The Portal answered with an error, or the channel broke."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class PortalTransport:
    """A JSON-RPC client to one Portal instance over one adb forward."""

    name = "portal"

    def __init__(
        self,
        *,
        serial: str | None = None,
        local_port: int = DEFAULT_LOCAL_PORT,
        remote_port: int = DEFAULT_REMOTE_PORT,
        adb: str = "adb",
        timeout: float = 10.0,
        skip_adb: bool = False,
    ) -> None:
        self.serial = serial
        self.local_port = local_port
        self.remote_port = remote_port
        self.adb = adb
        self.timeout = timeout
        # Loopback/test deployments dial an already-listening server and have
        # nothing for adb to forward.
        self.skip_adb = skip_adb
        self._sock: socket.socket | None = None
        self._req_id = 0

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        """Set up the adb forward and open the WebSocket."""
        self._ensure_forward()
        self._ws_connect()

    def _adb(self, *args: str) -> str:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout
        )
        if out.returncode != 0:
            raise PortalError(f"adb {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout

    def _ensure_forward(self) -> None:
        if self.skip_adb:
            return
        self._adb(
            "forward",
            f"tcp:{self.local_port}",
            f"tcp:{self.remote_port}",
        )

    def _ws_connect(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET /rpc HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.local_port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        sock = socket.create_connection(
            ("127.0.0.1", self.local_port), timeout=self.timeout
        )
        sock.settimeout(self.timeout)
        sock.sendall(request.encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                sock.close()
                raise PortalError("Portal closed during WebSocket handshake")
            response += chunk
        status = response.split(b"\r\n", 1)[0]
        if b"101" not in status:
            sock.close()
            raise PortalError(f"Portal handshake refused: {status!r}")
        self._sock = sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- RPC ----------------------------------------------------------------

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One JSON-RPC request; returns the ``result`` or raises."""
        if self._sock is None:
            self.connect()
        assert self._sock is not None
        self._req_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params or {},
        }
        # A broken socket must retire here, not poison every later call: a
        # bare OSError from send/recv (the Portal restarted, adb dropped the
        # forward) used to escape before ``close()`` ran, leaving the dead
        # socket in place - every subsequent call re-raised the same
        # WinError 10053 forever instead of reconnecting (found live).
        try:
            self._send_frame(json.dumps(request, ensure_ascii=False))
        except OSError as err:
            self.close()
            raise PortalError(f"send failed ({method}): {err}") from err
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                payload = self._recv_frame(deadline)
            except OSError as err:
                self.close()
                raise PortalError(f"recv failed ({method}): {err}") from err
            if payload is None:
                continue
            try:
                reply = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise PortalError(f"bad JSON from Portal: {exc}") from exc
            if reply.get("id") != self._req_id:
                continue  # an event notification; events have their own channel
            if "error" in reply:
                err = reply["error"] or {}
                raise PortalError(
                    str(err.get("message", err)), code=str(err.get("code"))
                )
            return reply.get("result")
        raise PortalError(f"Portal timed out on {method}")

    # -- WebSocket framing (client side, minimal) ---------------------------

    def _send_frame(self, text: str) -> None:
        assert self._sock is not None
        data = text.encode("utf-8")
        header = bytearray([0x81])  # FIN + text
        mask = os.urandom(4)
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self._sock.sendall(bytes(header) + masked)

    def _recv_frame(self, deadline: float) -> str | None:
        assert self._sock is not None
        sock = self._sock
        remaining = max(0.0, deadline - time.monotonic())
        sock.settimeout(remaining or 0.05)
        try:
            header = self._read_exact(2)
            if header is None:
                return None
            opcode = header[0] & 0x0F
            length = header[1] & 0x7F
            if length == 126:
                ext = self._read_exact(2)
                length = struct.unpack(">H", ext)[0]  # type: ignore[arg-type]
            elif length == 127:
                ext = self._read_exact(8)
                length = struct.unpack(">Q", ext)[0]  # type: ignore[arg-type]
            payload = b""
            if length:
                payload = self._read_exact(length) or b""
            if opcode == 0x8:  # close
                self.close()
                raise PortalError("Portal closed the WebSocket")
            return payload.decode("utf-8", errors="replace")
        except (socket.timeout, TimeoutError):
            return None

    def _read_exact(self, n: int) -> bytes | None:
        assert self._sock is not None
        buf = b""
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                raise PortalError("Portal connection dropped")
            buf += chunk
        return buf
