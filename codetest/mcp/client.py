"""Clients for the AST MCP server.

Two transports, one contract:

* :class:`InProcessAstClient` (default) calls the same tool handlers directly —
  no subprocess, no serialization cost, ideal for a single CLI session.
* :class:`StdioAstClient` speaks JSON-RPC to ``python -m codetest.mcp.server``,
  which is how an external MCP host would reach it.

Both return :class:`~codetest.models.MethodContext` objects: only the filtered
signature / dependency beans / call-order summary reaches the pipeline.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..models import ChangeUnit, MethodContext
from .tools import ToolError, call_tool


def _targets_for(units: Sequence[ChangeUnit]) -> List[Dict[str, Any]]:
    targets = []
    for u in units:
        targets.append({
            "file_path": u.file_path,
            "class_name": u.class_name,
            "method_name": u.method.name if u.method else "",
        })
    return targets


class AstMcpClient:
    """Common surface used by the pipeline."""

    transport = "abstract"

    def method_contexts(
        self, project_dir: Path, units: Sequence[ChangeUnit]
    ) -> List[MethodContext]:
        raise NotImplementedError

    def attach_contexts(
        self, project_dir: Path, units: Sequence[ChangeUnit]
    ) -> List[MethodContext]:
        """Fetch contexts and attach each one to its ChangeUnit."""
        contexts = self.method_contexts(project_dir, units)
        by_target = {(c.class_name, c.method_name): c for c in contexts}
        attached: List[MethodContext] = []
        for u in units:
            key = (u.class_name, u.method.name if u.method else "")
            ctx = by_target.get(key)
            if ctx is not None:
                u.context = ctx
                attached.append(ctx)
        return attached


class InProcessAstClient(AstMcpClient):
    """Default transport: the server's tool handlers, called directly."""

    transport = "inprocess"

    def method_contexts(
        self, project_dir: Path, units: Sequence[ChangeUnit]
    ) -> List[MethodContext]:
        if not units:
            return []
        payload = call_tool("ast_change_context", {
            "project_dir": str(project_dir),
            "targets": _targets_for(units),
        })
        return [MethodContext.from_dict(d) for d in payload.get("contexts", [])]


class StdioAstClient(AstMcpClient):
    """Talk to ``python -m codetest.mcp.server`` over JSON-RPC/stdio."""

    transport = "stdio"

    def __init__(self, command: Optional[List[str]] = None):
        self.command = command or [sys.executable, "-m", "codetest.mcp.server"]

    def method_contexts(
        self, project_dir: Path, units: Sequence[ChangeUnit]
    ) -> List[MethodContext]:
        if not units:
            return []
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                        "clientInfo": {"name": "codetest", "version": "0.1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "ast_change_context",
                        "arguments": {"project_dir": str(project_dir),
                                      "targets": _targets_for(units)}}},
        ]
        stdin_data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests)
        proc = subprocess.run(
            self.command, input=stdin_data, capture_output=True, text=True,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise ToolError(proc.stderr.strip() or "AST MCP server failed")

        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") != 2:
                continue
            result = message.get("result") or {}
            payload = result.get("structuredContent")
            if payload is None:
                content = result.get("content") or [{}]
                try:
                    payload = json.loads(content[0].get("text", "{}"))
                except json.JSONDecodeError:
                    payload = {}
            return [MethodContext.from_dict(d) for d in payload.get("contexts", [])]
        return []


def get_ast_client(transport: str = "inprocess") -> AstMcpClient:
    transport = (transport or "inprocess").lower()
    if transport in ("inprocess", "in-process", "local"):
        return InProcessAstClient()
    if transport == "stdio":
        return StdioAstClient()
    raise ValueError(f"unknown AST MCP transport: {transport}")
