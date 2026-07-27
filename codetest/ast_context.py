"""Build the *filtered* AST context served by the AST MCP server.

A changed Java file's full source (or full AST) is far more than the model
needs — and it is the single biggest driver of prompt size. This module
reduces a change down to exactly three fields:

1. 수정된 대상 메서드의 시그니처   (``method_signature``)
2. 의존 Bean 클래스의 이름 목록    (``dependency_beans``)
3. 호출 순서 요약 텍스트           (``call_flow``)

Everything else (imports, unrelated methods, javadoc, field bodies) is
dropped before the payload leaves the server.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .ast_analyzer import ClassInfo
from .models import MethodContext, MethodInfo

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
    raw = raw.split("<", 1)[0].strip()
    return raw.rsplit(".", 1)[-1]


def _looks_like_bean(type_name: str) -> bool:
    if not type_name or not type_name[0].isupper():
        return False
    if type_name in _NON_BEAN_TYPES:
        return False
    return type_name.endswith(_BEAN_SUFFIXES)


def _method_line_ranges(cls: ClassInfo) -> List[Tuple[int, int]]:
    return [(m.start_line, m.end_line) for m in cls.methods]


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
    out: List[Tuple[str, str]] = []
    m = pattern.search(source)
    if not m:
        return out
    params = m.group("params").strip()
    if not params:
        return out
    # Split on commas that are not inside generics.
    depth = 0
    current = ""
    chunks: List[str] = []
    for ch in params:
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
    for type_name, field_name, injected in _declared_fields(lines, _method_line_ranges(cls)):
        add(type_name, field_name, force=injected)

    return beans, by_field


def summarize_call_flow(
    source: str, method: Optional[MethodInfo], field_types: Dict[str, str], limit: int = 12
) -> str:
    """Summarize the call order inside the changed method as one line.

    e.g. ``OrderRepository.findById() → DiscountPolicy.apply() → OrderRepository.save()``
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
    """Filter a parsed file down to the MCP payload for one changed target."""
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
