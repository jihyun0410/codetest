"""Agent-side MCP clients.

Two transports, one contract:

* ``inprocess`` (default) dispatches straight into the server's registry — no
  subprocess, no serialization cost, ideal for a single CLI session.
* ``stdio`` speaks JSON-RPC to ``python -m codetest.mcp.<server>.server``,
  which is how an external MCP host reaches the same tools.

Because both go through the same registry, a tool can never behave differently
depending on how it was called.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Dict, List, Optional

from .base_server import MCPServer, ToolError

# server key -> module exposing `build_server()`
SERVERS: Dict[str, str] = {
    "git_file": "codetest.mcp.git_file.server",
    "ast_flow": "codetest.mcp.ast_flow.server",
    "test_exec": "codetest.mcp.test_exec.server",
}


def _load_server(key: str) -> MCPServer:
    import importlib

    module_name = SERVERS.get(key)
    if module_name is None:
        raise ToolError(f"unknown MCP server: {key}")
    return importlib.import_module(module_name).build_server()


class McpClient:
    """Base client: ``call(tool, args) -> dict``."""

    transport = "abstract"

    def __init__(self, server_key: str):
        self.server_key = server_key

    def call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_tools(self) -> List[str]:
        raise NotImplementedError


class InProcessClient(McpClient):
    transport = "inprocess"

    def __init__(self, server_key: str):
        super().__init__(server_key)
        self._server = _load_server(server_key)

    def call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return self._server.registry.call(tool, arguments)

    def list_tools(self) -> List[str]:
        return [t["name"] for t in self._server.registry.specs()]


class StdioClient(McpClient):
    transport = "stdio"

    def __init__(self, server_key: str, command: Optional[List[str]] = None):
        super().__init__(server_key)
        module = SERVERS.get(server_key)
        if module is None:
            raise ToolError(f"unknown MCP server: {server_key}")
        self.command = command or [sys.executable, "-m", module]

    def _roundtrip(self, requests: List[Dict[str, Any]], want_id: int) -> Dict[str, Any]:
        stdin_data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests)
        proc = subprocess.run(self.command, input=stdin_data, capture_output=True,
                              text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise ToolError(proc.stderr.strip() or f"{self.server_key} MCP server failed")

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == want_id:
                return message.get("result") or {}
        return {}

    @staticmethod
    def _handshake() -> List[Dict[str, Any]]:
        return [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "codetest", "version": "0.1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        ]

    def call(self, tool: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        result = self._roundtrip([
            *self._handshake(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": tool, "arguments": arguments}},
        ], want_id=2)

        if result.get("isError"):
            content = result.get("content") or [{}]
            raise ToolError(content[0].get("text", "MCP tool failed"))
        payload = result.get("structuredContent")
        if payload is None:
            content = result.get("content") or [{}]
            try:
                payload = json.loads(content[0].get("text", "{}"))
            except json.JSONDecodeError:
                payload = {}
        return payload

    def list_tools(self) -> List[str]:
        result = self._roundtrip([
            *self._handshake(),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ], want_id=2)
        return [t["name"] for t in result.get("tools", [])]


def get_client(server_key: str, transport: str = "inprocess") -> McpClient:
    transport = (transport or "inprocess").lower()
    if transport in ("inprocess", "in-process", "local"):
        return InProcessClient(server_key)
    if transport == "stdio":
        return StdioClient(server_key)
    raise ValueError(f"unknown MCP transport: {transport}")
