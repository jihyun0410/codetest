"""AST MCP server + clients.

The server owns all Java-AST knowledge and exposes it as MCP tools. It filters
each changed target down to the method signature, the dependency bean class
names and a call-order summary, so the LLM request stays small and stable.
"""
from __future__ import annotations

from .client import (AstMcpClient, InProcessAstClient, StdioAstClient,
                     get_ast_client)
from .tools import TOOLS, ToolError, call_tool

__all__ = [
    "AstMcpClient",
    "InProcessAstClient",
    "StdioAstClient",
    "get_ast_client",
    "TOOLS",
    "ToolError",
    "call_tool",
]
