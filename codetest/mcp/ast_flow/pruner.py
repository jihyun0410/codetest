"""가지치기(Pruning) — AST를 LLM에 보낼 최소 컨텍스트로 축약.

A changed Java file's full source (or full AST) is far more than the model
needs — and it is the single biggest driver of prompt size. This module
reduces a change down to exactly three fields:

1. 수정된 대상 메서드의 시그니처   (``method_signature``)
2. 의존 Bean 클래스의 이름 목록    (``dependency_beans``)
3. 호출 순서 요약 텍스트           (``call_flow``)

Everything else (imports, unrelated methods, javadoc, field bodies) is dropped
before the payload leaves the server.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from ...models import ClassInfo, MethodContext, MethodInfo
from . import ast_tool

# Types that are data/utility, never an injected collaborator.
_NON_BEAN_TYPES = {
    "String", "Integer", "Long", "Double", "Float", "Boolean", "Byte", "Short",
    "Character", "BigDecimal", "BigInteger", "Object", "List", "Map", "Set",
    "Collection", "Optional", "Stream", "UUID", "Date", "LocalDate", "Duration",
    "LocalDateTime", "LocalTime", "Instant", "Class", "Exception", "Logger",
    "Log", "Thread", "Enum", "Number", "Iterable", "Queue", "Deque", "Array",
}

# Suffixes that mark a Spring collaborator when there is no explicit annotation.
_BEAN_SUFFIXES = (
    "Service", "Repository", "Client", "Mapper", "Manager", "Component",
    "Gateway", "Provider", "Template", "Handler", "Factory", "Publisher",
    "Validator", "Policy", "Executor", "Dao", "DAO", "Producer", "Consumer",
    "Facade", "Adapter", "Converter", "Encoder", "Resolver", "Engine", "Store",
    "Rule", "Calculator", "Notifier", "Sender", "Broker", "Registry",
)

_INJECT_ANNOTATIONS = ("@Autowired", "@Resource", "@Inject", "@Qualifier")

# Receivers that are stdlib/noise rather than collaborators.
_NOISE_RECEIVERS = {
    "System", "String", "Math", "Integer", "Long", "Double", "Boolean", "Arrays",
    "Collections", "Objects", "Optional", "Stream", "LocalDate", "LocalDateTime",
    "BigDecimal", "UUID", "this", "super", "log", "logger", "LOG", "LOGGER",
    "Assert", "Assertions", "e", "ex", "t",
}

_FIELD_RE = re.compile(
    r"^\s*(?:(?:private|protected|public|static|final|transient|volatile)\s+)*"
    r"(?P<type>[A-Z][A-Za-z0-9_.]*)\s*(?:<[^>=;]*>)?\s+"
    r"(?P<name>[a-z_]\w*)\s*(?:=[^;]*)?;\s*$"
)

_CALL_RE = re.compile(r"\b(?P<recv>[A-Za-z_]\w*)\s*\.\s*(?P<call>\w+)\s*\(")
_ACCESSOR_RE = re.compile(r"^(get|is|set|has)[A-Z_]")


def _simple_type(raw: str) -> str:
    """``com.foo.OrderRepository<Long>`` -> ``OrderRepository``."""
    return raw.split("<", 1)[0].strip().rsplit(".", 1)[-1]


def _looks_like_bean(type_name: str) -> bool:
    if not type_name or not type_name[0].isupper():
        return False
    if type_name in _NON_BEAN_TYPES:
        return False
    return type_name.endswith(_BEAN_SUFFIXES)


def _declared_fields(
    source_lines: Sequence[str], method_ranges: Sequence[Tuple[int, int]]
) -> List[Tuple[str, str, bool]]:
    """Return (type, field_name, explicitly_injected) for class-level fields.

    Lines inside a method body are skipped so local variables are not mistaken
    for injected collaborators.
    """
    in_method = set()
    for start, end in method_ranges:
        in_method.update(range(start, end + 1))

    fields: List[Tuple[str, str, bool]] = []
    annotated = False
    for idx, line in enumerate(source_lines, start=1):
        stripped = line.strip()
        if stripped.startswith("@"):
            annotated = annotated or stripped.startswith(_INJECT_ANNOTATIONS)
            continue
        if idx in in_method:
            annotated = False
            continue
        m = _FIELD_RE.match(line)
        if m:
            fields.append((_simple_type(m.group("type")), m.group("name"), annotated))
        annotated = False
    return fields


def _constructor_param_types(source: str, class_name: str) -> List[Tuple[str, str]]:
    """Constructor injection is the strongest bean signal — capture (type, name)."""
    pattern = re.compile(
        rf"(?:public|protected)?\s*{re.escape(class_name)}\s*\((?P<params>[^)]*)\)\s*\{{",
        re.DOTALL,
    )
    m = pattern.search(source)
    if not m or not m.group("params").strip():
        return []

    # Split on commas that are not inside generics.
    depth, current, chunks = 0, "", []
    for ch in m.group("params"):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            chunks.append(current)
            current = ""
        else:
            current += ch
    chunks.append(current)

    out: List[Tuple[str, str]] = []
    for chunk in chunks:
        cleaned = re.sub(r"@\w+(\([^)]*\))?", " ", chunk).replace("final ", " ").strip()
        cleaned = re.sub(r"<[^>]*>", "", cleaned)
        parts = cleaned.split()
        if len(parts) >= 2:
            out.append((_simple_type(parts[-2]), parts[-1]))
    return out


def collect_dependency_beans(
    source: str, cls: ClassInfo
) -> Tuple[List[str], Dict[str, str]]:
    """Return (bean class names, field-name -> bean-type map).

    A collaborator qualifies if it is constructor-injected, annotated with
    ``@Autowired``/``@Resource``/``@Inject``, or named like a Spring bean.
    """
    lines = source.splitlines()
    beans: List[str] = []
    by_field: Dict[str, str] = {}

    def add(type_name: str, field_name: str, force: bool) -> None:
        if type_name in _NON_BEAN_TYPES:
            return
        if not (force or _looks_like_bean(type_name)):
            return
        by_field[field_name] = type_name
        if type_name not in beans:
            beans.append(type_name)

    for type_name, field_name in _constructor_param_types(source, cls.name):
        add(type_name, field_name, force=True)
    ranges = [(m.start_line, m.end_line) for m in cls.methods]
    for type_name, field_name, injected in _declared_fields(lines, ranges):
        add(type_name, field_name, force=injected)

    return beans, by_field


def summarize_call_flow(
    source: str, method: Optional[MethodInfo], field_types: Dict[str, str],
    limit: int = 12,
) -> str:
    """Summarize the call order inside the changed method as one line.

    e.g. ``DiscountPolicy.apply() → OrderRepository.save()``
    """
    if method is None:
        return ""
    lines = source.splitlines()
    start = max(1, method.start_line)
    end = min(len(lines), max(method.end_line, method.start_line))
    body = "\n".join(lines[start - 1:end])

    steps: List[str] = []
    for m in _CALL_RE.finditer(body):
        recv, call = m.group("recv"), m.group("call")
        if recv in _NOISE_RECEIVERS or call in ("class", "length"):
            continue
        owner = field_types.get(recv)
        if owner is None:
            # Not a collaborator — keep the call only if it does real work.
            # Plain accessors on DTOs/params would drown out the actual flow.
            if _ACCESSOR_RE.match(call):
                continue
            owner = recv
        step = f"{owner}.{call}()"
        if not steps or steps[-1] != step:      # collapse consecutive repeats
            steps.append(step)
        if len(steps) >= limit:
            steps.append("…")
            break
    return " → ".join(steps)


def build_method_context(
    source: str,
    classes: Sequence[ClassInfo],
    file_path: str,
    class_name: str,
    method_name: Optional[str] = None,
) -> MethodContext:
    """Prune a parsed file down to the MCP payload for one changed target."""
    cls = next((c for c in classes if c.name == class_name), None)
    if cls is None:
        cls = classes[0] if classes else ClassInfo(name=class_name, package="", methods=[])

    method = next((m for m in cls.methods if m.name == method_name), None) if method_name else None
    beans, field_types = collect_dependency_beans(source, cls)

    if method is not None:
        signature = method.full_signature
        call_flow = summarize_call_flow(source, method, field_types)
    else:
        # Class-level change (fields/imports): summarize the public surface instead.
        public = [m.name for m in cls.methods if "public" in m.modifiers] or [
            m.name for m in cls.methods
        ]
        signature = f"class {cls.name} (public: {', '.join(public) or '없음'})"
        call_flow = ""

    return MethodContext(
        file_path=file_path,
        class_name=cls.name,
        method_name=method.name if method else "",
        method_signature=signature,
        dependency_beans=beans,
        call_flow=call_flow,
    )


# --------------------------------------------------------------------------- #
# MCP tools
# --------------------------------------------------------------------------- #

_TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "file_path": {"type": "string"},
        "class_name": {"type": "string", "description": "생략 시 첫 클래스"},
        "method_name": {"type": "string", "description": "생략 시 클래스 레벨 요약"},
    },
    "required": ["file_path"],
}

METHOD_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string"},
        "file_path": {"type": "string"},
        "class_name": {"type": "string"},
        "method_name": {"type": "string"},
    },
    "required": ["project_dir", "file_path"],
}

CHANGE_CONTEXT_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string"},
        "targets": {"type": "array", "items": _TARGET_SCHEMA},
    },
    "required": ["project_dir", "targets"],
}


def _one_context(project_dir: Path, target: dict) -> dict:
    from ..base_server import ToolError

    file_path = target.get("file_path")
    if not file_path:
        raise ToolError("file_path is required")
    abs_path = (project_dir / file_path).resolve()
    if not abs_path.exists():
        raise ToolError(f"file not found: {file_path}")

    source = abs_path.read_text(encoding="utf-8", errors="replace")
    classes = ast_tool.analyze_file(abs_path)
    class_name = target.get("class_name") or (classes[0].name if classes else Path(file_path).stem)
    ctx = build_method_context(source, classes, file_path, class_name,
                               target.get("method_name") or None)
    return ctx.to_dict()


def tool_method_context(args: dict) -> dict:
    return _one_context(Path(args["project_dir"]), args)


def tool_change_context(args: dict) -> dict:
    from ..base_server import ToolError

    project_dir = Path(args["project_dir"])
    contexts, errors = [], []
    for target in args.get("targets", []):
        try:
            contexts.append(_one_context(project_dir, target))
        except ToolError as e:
            errors.append(str(e))
    return {"contexts": contexts, "errors": errors}


def register(registry) -> None:
    registry.register(
        "ast_method_context",
        "변경된 대상 메서드의 시그니처 / 의존 Bean 클래스 이름 목록 / 호출 순서 "
        "요약만 필터링해 반환합니다 (전체 소스·AST는 반환하지 않음).",
        METHOD_CONTEXT_SCHEMA, tool_method_context,
    )
    registry.register(
        "ast_change_context",
        "여러 변경 대상을 한 번에 조회하는 배치 버전.",
        CHANGE_CONTEXT_SCHEMA, tool_change_context,
    )
