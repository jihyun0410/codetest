"""Java AST analysis using ``javalang``.

Responsibilities:

* Parse a Java source file into classes/methods (used to populate the
  feature DB and to map changed line numbers to the enclosing method).
* A pure-regex fallback is used if a file fails to parse (e.g. partial/
  invalid syntax mid-edit), so the pipeline degrades gracefully.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .models import MethodInfo

try:
    import javalang  # type: ignore
    _HAVE_JAVALANG = True
except Exception:  # pragma: no cover - import guard
    _HAVE_JAVALANG = False


@dataclass
class ClassInfo:
    name: str
    package: str
    methods: List[MethodInfo]


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
            methods.append(
                MethodInfo(
                    name=m.name,
                    signature=f"{m.name}({params})",
                    start_line=line,
                    end_line=line,
                    modifiers=list(m.modifiers),
                    return_type=ret_name,
                )
            )
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
        methods.append(
            MethodInfo(
                name=name,
                signature=f"{name}(...)",
                start_line=start_line,
                end_line=start_line,
                modifiers=mods,
                return_type=(m.group("ret") or "void").strip(),
            )
        )
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


def analyze_file(path: Path) -> List[ClassInfo]:
    source = path.read_text(encoding="utf-8", errors="replace")
    return analyze_source(source)


def find_method_for_lines(
    classes: List[ClassInfo], changed_lines: List[int]
) -> List[tuple[str, MethodInfo]]:
    """Return (class_name, method) pairs whose line range intersects changes."""
    hits: List[tuple[str, MethodInfo]] = []
    changed = set(changed_lines)
    for c in classes:
        for m in c.methods:
            if any(m.start_line <= ln <= m.end_line for ln in changed):
                hits.append((c.name, m))
    return hits
