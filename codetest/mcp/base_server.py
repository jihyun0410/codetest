"""MCP 공통 프로토콜 및 Tool 등록 인터페이스.

A minimal JSON-RPC 2.0 / MCP implementation with no third-party dependency, so
each domain server (git_file, ast_flow, test_exec) only has to register its
tools and call :meth:`MCPServer.main`. Any MCP host can then run it:

    {"command": "python", "args": ["-m", "codetest.mcp.ast_flow.server"]}

Servers never import the agent, cli or a running pipeline — they expose tools
over a narrow, serializable boundary and nothing else.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2024-11-05"

Handler = Callable[[Dict[str, Any]], Dict[str, Any]]


class ToolError(RuntimeError):
    """Raised by a tool handler for an expected, reportable failure."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Handler

    def spec(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


@dataclass
class ToolRegistry:
    """The set of tools one MCP server exposes."""

    tools: Dict[str, Tool] = field(default_factory=dict)

    def register(self, name: str, description: str,
                 input_schema: Dict[str, Any], handler: Handler) -> None:
        self.tools[name] = Tool(name, description, input_schema, handler)

    def specs(self) -> List[Dict[str, Any]]:
        return [t.spec() for t in self.tools.values()]

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        tool = self.tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")
        return tool.handler(arguments or {})


class MCPServer:
    """JSON-RPC dispatch over a :class:`ToolRegistry`."""

    def __init__(self, name: str, registry: ToolRegistry, version: str = "0.1.0"):
        self.name = name
        self.registry = registry
        self.version = version

    # -- protocol ---------------------------------------------------------- #
    def handle_request(self, request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC request. Returns None for notifications."""
        method = request.get("method")
        req_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            return self._result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.name, "version": self.version},
            })

        if method in ("notifications/initialized", "initialized"):
            return None

        if method == "ping":
            return self._result(req_id, {})

        if method == "tools/list":
            return self._result(req_id, {"tools": self.registry.specs()})

        if method == "tools/call":
            return self._result(req_id, self.call_tool(
                params.get("name", ""), params.get("arguments") or {}))

        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Run a tool and wrap it in an MCP ``tools/call`` result."""
        try:
            payload = self.registry.call(name, arguments)
        except (ToolError, KeyError, ValueError, OSError) as e:
            return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
        return {
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "structuredContent": payload,
            "isError": False,
        }

    @staticmethod
    def _result(req_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    # -- transport --------------------------------------------------------- #
    def serve(self, stdin=None, stdout=None) -> None:
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
            response = self.handle_request(request)
            if response is not None:
                stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                stdout.flush()

    def main(self) -> None:
        for stream in (sys.stdin, sys.stdout):
            try:
                stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
            except Exception:
                pass
        self.serve()
