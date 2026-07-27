"""① Git & File MCP Server.

Run standalone with::

    python -m codetest.mcp.git_file.server

Tools: ``git_scan_changes``, ``file_read_source``, ``file_write_test``.
"""
from __future__ import annotations

from ..base_server import MCPServer, ToolRegistry
from . import file_tool, git_tool

SERVER_NAME = "codetest-git-file"


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    git_tool.register(registry)
    file_tool.register(registry)
    return registry


def build_server() -> MCPServer:
    return MCPServer(SERVER_NAME, build_registry())


def main() -> None:
    build_server().main()


if __name__ == "__main__":
    main()
