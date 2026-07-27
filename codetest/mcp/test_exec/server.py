"""③ Test Execution MCP Server.

Run standalone with::

    python -m codetest.mcp.test_exec.server

Tools: ``test_run``, ``coverage_report``.

The server reports raw execution facts only. Judging whether a result is
*valid* is a reasoning step and belongs to the agent, not to this tool.
"""
from __future__ import annotations

from ..base_server import MCPServer, ToolRegistry
from . import build_tool, jacoco_tool

SERVER_NAME = "codetest-test-exec"


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    build_tool.register(registry)
    jacoco_tool.register(registry)
    return registry


def build_server() -> MCPServer:
    return MCPServer(SERVER_NAME, build_registry())


def main() -> None:
    build_server().main()


if __name__ == "__main__":
    main()
