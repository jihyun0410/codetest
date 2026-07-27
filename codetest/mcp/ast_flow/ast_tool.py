"""AST Tool — Java AST 파싱 및 변경 라인 ↔ 메서드 매핑.

Uses ``javalang`` with a pure-regex fallback, so a file that fails to parse
mid-edit degrades gracefully instead of breaking the run. Parsed results are
memoized by file fingerprint (:mod:`codetest.storage.cache_service`), so
unchanged files are never re-parsed within a session.

Pruning (what actually gets sent to the model) lives in :mod:`.pruner`; this
module stays a faithful representation of the file.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from ...models import ClassInfo, MethodInfo
from ...storage.cache_service import AstCache, default_cache

try:
    import javalang  # type: ignore
    _HAVE_JAVALANG = True
except Exception:  # pragma: no cover - import guard
    _HAVE_JAVALANG = False


_METHOD_RE = re.compile(
    r"^\s*(?:@\w+\s*)*"
    r"(?P<mods>(?:public|private|protected|static|final|synchronized|abstract\s+)*)"
    r"(?P<ret>[\w<>\[\],.\s]+?)\s+"
    r"(?P<name>\w+)\s*\([^;]*\)\s*(?:throws [\w,\s.]+)?\{",
    re.MULTILINE,
)


def _method_end_lines(methods: List[MethodInfo], total_lines: int) -> None:
    """Approximate each method's end line as the line before the next method."""
    ordered = sorted(methods, key=lambda m: m.start_line)
    for i, m in enumerate(ordered):
        if i + 1 < len(ordered):
            m.end_line = max(m.start_line, ordered[i + 1].start_line - 1)
        else:
            m.end_line = total_lines


def _parse_with_javalang(source: str) -> List[ClassInfo]:
    tree = javalang.parse.parse(source)
    package = tree.package.name if tree.package else ""
    classes: List[ClassInfo] = []
    total_lines = len(source.splitlines())
    for _, node in tree.filter(javalang.tree.TypeDeclaration):
        if not hasattr(node, "methods"):
            continue
        methods: List[MethodInfo] = []
        for m in getattr(node, "methods", []):
            line = m.position.line if m.position else 0
            params = ", ".join(
                f"{p.type.name if hasattr(p.type, 'name') else p.type} {p.name}"
                for p in m.parameters
            )
            ret = getattr(m, "return_type", None)
            ret_name = ret.name if ret and hasattr(ret, "name") else "void"
            methods.append(MethodInfo(
                name=m.name, signature=f"{m.name}({params})",
                start_line=line, end_line=line,
                modifiers=list(m.modifiers), return_type=ret_name,
            ))
        _method_end_lines(methods, total_lines)
        classes.append(ClassInfo(name=node.name, package=package, methods=methods))
    return classes


def _parse_with_regex(source: str) -> List[ClassInfo]:
    total_lines = len(source.splitlines())
    pkg_m = re.search(r"package\s+([\w.]+)\s*;", source)
    package = pkg_m.group(1) if pkg_m else ""
    cls_m = re.search(r"\b(?:class|interface|enum)\s+(\w+)", source)
    class_name = cls_m.group(1) if cls_m else "Unknown"

    methods: List[MethodInfo] = []
    for m in _METHOD_RE.finditer(source):
        name = m.group("name")
        if name in {"if", "for", "while", "switch", "catch", "return"}:
            continue
        start_line = source.count("\n", 0, m.start()) + 1
        mods = [w for w in (m.group("mods") or "").split() if w]
        methods.append(MethodInfo(
            name=name, signature=f"{name}(...)",
            start_line=start_line, end_line=start_line,
            modifiers=mods, return_type=(m.group("ret") or "void").strip(),
        ))
    _method_end_lines(methods, total_lines)
    return [ClassInfo(name=class_name, package=package, methods=methods)]


def analyze_source(source: str) -> List[ClassInfo]:
    """Parse Java source into class/method info, with graceful fallback."""
    if _HAVE_JAVALANG:
        try:
            return _parse_with_javalang(source)
        except Exception:
            pass
    return _parse_with_regex(source)


def analyze_file(path: Path, cache: Optional[AstCache] = None) -> List[ClassInfo]:
    """Parse a file, reusing the cached AST when it has not changed."""
    cache = cache or default_cache()
    cached = cache.get(path)
    if cached is not None:
        return cached
    classes = analyze_source(path.read_text(encoding="utf-8", errors="replace"))
    cache.put(path, classes)
    return classes


def find_method_for_lines(
    classes: Sequence[ClassInfo], changed_lines: Sequence[int]
) -> List[Tuple[str, MethodInfo]]:
    """Return (class_name, method) pairs whose line range intersects changes."""
    hits: List[Tuple[str, MethodInfo]] = []
    changed = set(changed_lines)
    for c in classes:
        for m in c.methods:
            if any(m.start_line <= ln <= m.end_line for ln in changed):
                hits.append((c.name, m))
    return hits


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #

PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string"},
        "file_path": {"type": "string"},
        "changed_lines": {"type": "array", "items": {"type": "integer"},
                          "description": "주어지면 해당 라인을 포함하는 메서드만 표시"},
    },
    "required": ["project_dir", "file_path"],
}


def tool_parse_file(args: dict) -> dict:
    path = (Path(args["project_dir"]) / args["file_path"]).resolve()
    classes = analyze_file(path)
    payload = {"file_path": args["file_path"],
               "package": classes[0].package if classes else "",
               "classes": [c.to_dict() for c in classes]}
    changed = args.get("changed_lines")
    if changed:
        payload["changed_methods"] = [
            {"class_name": name, "method": m.to_dict()}
            for name, m in find_method_for_lines(classes, changed)
        ]
    return payload


def register(registry) -> None:
    registry.register(
        "ast_parse_file",
        "Java 파일을 파싱해 클래스/메서드 목록을 반환합니다. changed_lines를 주면 "
        "해당 라인을 포함하는 메서드도 함께 표시합니다.",
        PARSE_SCHEMA, tool_parse_file,
    )
