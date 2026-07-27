"""② AST & Flow MCP Server.

Run standalone with::

    python -m codetest.mcp.ast_flow.server

Tools: ``ast_parse_file``, ``ast_method_context``, ``ast_change_context``,
``flow_summary``.

Whatever the transport, the pruned payload is all that leaves this server —
시그니처 / 의존 Bean 목록 / 호출 순서 요약.
"""
from __future__ import annotations

from ..base_server import MCPServer, ToolRegistry
from . import ast_tool, flow_tool, pruner

SERVER_NAME = "codetest-ast-flow"


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    ast_tool.register(registry)
    pruner.register(registry)
    flow_tool.register(registry)
    return registry


def build_server() -> MCPServer:
    return MCPServer(SERVER_NAME, build_registry())


def main() -> None:
    build_server().main()


if __name__ == "__main__":
    main()
