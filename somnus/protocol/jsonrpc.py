"""Minimal JSON-RPC server over stdio (no external dependencies).

Line-delimited JSON requests::

    {"jsonrpc":"2.0","id":1,"method":"tools/list"}
    {"jsonrpc":"2.0","id":2,"method":"tools/call",
     "params":{"name":"call","arguments":{"action":"CALL p1.add()"}}}

Also supports a raw text mode where each request is a SUI-Text command line,
which is what the CLI uses for scripting.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from somnus.protocol.tools import TOOL_SPECS, call_tool
from somnus.runtime.session import Session


class JsonRpcServer:
    def __init__(self, session: Session) -> None:
        self.session = session

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        try:
            if method == "tools/list":
                return _ok(req_id, {"tools": TOOL_SPECS})
            if method == "tools/call":
                name = params.get("name")
                text = call_tool(self.session, name, params.get("arguments"))
                return _ok(req_id, {"content": [{"type": "text", "text": text}]})
            if method == "sui":
                text = self.session.dispatch(params.get("text", ""))
                return _ok(req_id, {"text": text})
            return _err(req_id, -32601, f"unknown method {method}")
        except Exception as exc:  # pragma: no cover - defensive
            return _err(req_id, -32000, str(exc))

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                stdout.write(json.dumps(_err(None, -32700, "parse error")) + "\n")
                stdout.flush()
                continue
            stdout.write(json.dumps(self.handle(request), ensure_ascii=False) + "\n")
            stdout.flush()


def _ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
