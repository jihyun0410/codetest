"""[계층 3] MCP Servers — 로컬 환경 제어 Tool 제공자.

Three servers, each independently runnable by any MCP host:

| 서버 | 모듈 | Tool |
|------|------|------|
| ① Git & File | ``codetest.mcp.git_file.server`` | ``git_scan_changes``, ``file_read_source``, ``file_write_test`` |
| ② AST & Flow | ``codetest.mcp.ast_flow.server`` | ``ast_parse_file``, ``ast_method_context``, ``ast_change_context``, ``flow_summary`` |
| ③ Test Exec  | ``codetest.mcp.test_exec.server`` | ``test_run``, ``coverage_report`` |

Servers depend on :mod:`codetest.models` and :mod:`codetest.storage` only —
never on the agent or the CLI, so the boundary stays serializable.
"""
from __future__ import annotations

from .base_server import MCPServer, Tool, ToolError, ToolRegistry
from .client import (SERVERS, InProcessClient, McpClient, StdioClient,
                     get_client)

__all__ = [
    "MCPServer",
    "Tool",
    "ToolRegistry",
    "ToolError",
    "McpClient",
    "InProcessClient",
    "StdioClient",
    "get_client",
    "SERVERS",
]
