"""File Tool — 소스코드 읽기, @SpringBootTest .java 파일 생성/저장.

All filesystem writes the agent performs go through here, so "where does a
generated test land" is answered in exactly one place.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

from ..base_server import ToolError


def read_source(project_dir: Path, file_path: str) -> str:
    abs_path = (project_dir / file_path).resolve()
    if not abs_path.exists():
        raise ToolError(f"file not found: {file_path}")
    return abs_path.read_text(encoding="utf-8", errors="replace")


def write_test_file(test_source_dir: Path, package: str, class_name: str,
                    source: str) -> Path:
    """Write a test class into ``<test_source_dir>/<package as dirs>/<Class>.java``."""
    pkg_dir = test_source_dir / Path(*package.split(".")) if package else test_source_dir
    pkg_dir.mkdir(parents=True, exist_ok=True)
    path = pkg_dir / f"{class_name}.java"
    path.write_text(source, encoding="utf-8")
    return path


def test_file_path(test_source_dir: Path, package: str, class_name: str) -> Path:
    pkg_dir = test_source_dir / Path(*package.split(".")) if package else test_source_dir
    return pkg_dir / f"{class_name}.java"


def parse_java_identity(source: str, default_package: str = "",
                        default_class: str = "ProvidedTest") -> Tuple[str, str]:
    """Return (package, class_name) declared in a Java source."""
    pkg = re.search(r"package\s+([\w.]+)\s*;", source)
    cls = re.search(r"\b(?:class|interface)\s+(\w+)", source)
    return (pkg.group(1) if pkg else default_package,
            cls.group(1) if cls else default_class)


def ensure_springboot_annotation(source: str) -> str:
    """Guarantee ``@SpringBootTest`` is present (spec: 제공된 테스트도 동일 방식 실행)."""
    if "@SpringBootTest" in source:
        return source

    out = []
    injected = False
    import_added = False
    for line in source.splitlines():
        if not import_added and line.startswith("package "):
            out.append(line)
            out.append("import org.springframework.boot.test.context.SpringBootTest;")
            import_added = True
            continue
        if not injected and re.match(r"\s*(public\s+)?(class|interface)\s+\w+", line):
            out.append("@SpringBootTest")
            injected = True
        out.append(line)
    if not injected:
        out.insert(0, "@SpringBootTest")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string"},
        "file_path": {"type": "string", "description": "프로젝트 기준 상대 경로"},
    },
    "required": ["project_dir", "file_path"],
}

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "test_source_dir": {"type": "string", "description": "예: <project>/src/test/java"},
        "package": {"type": "string"},
        "class_name": {"type": "string"},
        "source": {"type": "string", "description": "@SpringBootTest 클래스 전체 소스"},
        "ensure_springboot": {"type": "boolean", "description": "누락 시 @SpringBootTest 주입"},
    },
    "required": ["test_source_dir", "class_name", "source"],
}


def tool_read_source(args: dict) -> dict:
    source = read_source(Path(args["project_dir"]), args["file_path"])
    return {"file_path": args["file_path"], "source": source,
            "line_count": len(source.splitlines())}


def tool_write_test(args: dict) -> dict:
    source = args["source"]
    if args.get("ensure_springboot"):
        source = ensure_springboot_annotation(source)
    path = write_test_file(Path(args["test_source_dir"]), args.get("package", ""),
                           args["class_name"], source)
    return {"file_path": str(path), "bytes_written": len(source.encode("utf-8"))}


def register(registry) -> None:
    registry.register(
        "file_read_source", "프로젝트 내 소스 파일 내용을 읽습니다.",
        READ_SCHEMA, tool_read_source,
    )
    registry.register(
        "file_write_test",
        "생성된 @SpringBootTest 클래스를 테스트 소스 트리에 저장합니다.",
        WRITE_SCHEMA, tool_write_test,
    )
