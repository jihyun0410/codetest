"""Tool definitions and handlers for the AST MCP server.

Shared by both transports (in-process and stdio) so the filtering logic is
identical no matter how the pipeline talks to the server.

The contract is deliberately narrow: callers describe *which* change they care
about, and the server answers with the filtered context only — the method
signature, the dependency bean names, and a call-order summary. Full source,
full ASTs and unrelated methods never cross the boundary.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from .. import ast_analyzer, ast_context

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "ast_method_context",
        "description": (
            "변경된 대상 메서드의 시그니처 / 의존 Bean 클래스 이름 목록 / 호출 순서 "
            "요약만 필터링해서 반환합니다 (전체 소스·AST는 반환하지 않음)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string", "description": "분석 대상 프로젝트 루트"},
                "file_path": {"type": "string", "description": "프로젝트 기준 상대 경로"},
                "class_name": {"type": "string", "description": "대상 클래스명 (생략 시 첫 클래스)"},
                "method_name": {"type": "string", "description": "대상 메서드명 (생략 시 클래스 레벨 요약)"},
            },
            "required": ["project_dir", "file_path"],
        },
    },
    {
        "name": "ast_change_context",
        "description": (
            "여러 변경 대상을 한 번에 조회하는 배치 버전. targets 배열의 각 항목마다 "
            "필터링된 컨텍스트를 반환합니다."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_dir": {"type": "string"},
                "targets": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "class_name": {"type": "string"},
                            "method_name": {"type": "string"},
                        },
                        "required": ["file_path"],
                    },
                },
            },
            "required": ["project_dir", "targets"],
        },
    },
]


class ToolError(RuntimeError):
    pass


def _one_context(project_dir: Path, target: Dict[str, Any]) -> Dict[str, Any]:
    file_path = target.get("file_path")
    if not file_path:
        raise ToolError("file_path is required")

    abs_path = (project_dir / file_path).resolve()
    if not abs_path.exists():
        raise ToolError(f"file not found: {file_path}")

    source = abs_path.read_text(encoding="utf-8", errors="replace")
    classes = ast_analyzer.analyze_source(source)
    class_name = target.get("class_name") or (classes[0].name if classes else Path(file_path).stem)

    ctx = ast_context.build_method_context(
        source=source,
        classes=classes,
        file_path=file_path,
        class_name=class_name,
        method_name=target.get("method_name") or None,
    )
    return ctx.to_dict()


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a tool call and return its (already filtered) JSON result."""
    if name == "ast_method_context":
        project_dir = Path(arguments["project_dir"])
        return _one_context(project_dir, arguments)

    if name == "ast_change_context":
        project_dir = Path(arguments["project_dir"])
        contexts: List[Dict[str, Any]] = []
        errors: List[str] = []
        for target in arguments.get("targets", []):
            try:
                contexts.append(_one_context(project_dir, target))
            except ToolError as e:
                errors.append(str(e))
        return {"contexts": contexts, "errors": errors}

    raise ToolError(f"unknown tool: {name}")
