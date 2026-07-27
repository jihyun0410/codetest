"""AST MCP server (JSON-RPC 2.0 over stdio).

Run standalone with::

    python -m codetest.mcp.server

Implements the subset of MCP the agent needs — ``initialize``, ``tools/list``
and ``tools/call`` — with no third-party dependency, so the server can be
registered in any MCP host config:

    {"command": "python", "args": ["-m", "codetest.mcp.server"]}

Whatever the transport, the server only ever emits the *filtered* context
(시그니처 / 의존 Bean 목록 / 호출 순서 요약).
"""
from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional

from .tools import TOOLS, ToolError, call_tool

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "codetest-ast", "version": "0.1.0"}


def handle_request(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC request. Returns None for notifications."""
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return _result(req_id, {})

    if method == "tools/list":
        return _result(req_id, {"tools": TOOLS})

    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            payload = call_tool(name, arguments)
        except (ToolError, KeyError, OSError) as e:
            return _result(req_id, {
                "content": [{"type": "text", "text": f"error: {e}"}],
                "isError": True,
            })
        return _result(req_id, {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload,
            "isError": False,
        })

    if req_id is None:
        return None
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"method not found: {method}"},
    }


def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def serve(stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC messages until EOF."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_request(request)
        if response is not None:
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def main() -> None:
    for stream in (sys.stdin, sys.stdout):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass
    serve()


if __name__ == "__main__":
    main()
