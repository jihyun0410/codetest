"""Flow Tool — 다중 파일 수정 시 호출 순서(Call Graph) 및 의존성 분석.

When a change spans several files the spec asks for **one** business-flow
test. To write that test in the right order the agent needs to know which of
the changed classes calls which: a Controller change and a Service change are
one flow, not two independent tests.

The graph is built from what the pruner already extracted (each context's
dependency bean names), so this tool costs no extra parsing.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Sequence

from ...models import FlowSummary, MethodContext

# Lower rank = closer to the request entry point.
_LAYER_RANK = (
    ("controller", 0), ("resource", 0), ("endpoint", 0),
    ("facade", 1), ("service", 2), ("manager", 2), ("policy", 3),
    ("component", 3), ("client", 4), ("gateway", 4),
    ("repository", 5), ("dao", 5), ("mapper", 5),
)


def _rank(ctx: MethodContext) -> int:
    hay = f"{ctx.file_path} {ctx.class_name}".lower()
    for token, rank in _LAYER_RANK:
        if token in hay:
            return rank
    return 3


def build_flow(contexts: Sequence[MethodContext]) -> FlowSummary:
    """Order the changed targets into a single call sequence.

    Ordering rules, in priority order:

    1. If A depends on B (B is in A's dependency beans), A comes first.
    2. Otherwise fall back to the Spring layer rank (controller → … → repository).

    Cycles cannot hang the sort: anything still unresolved is appended by rank.
    """
    nodes = list(contexts)
    if not nodes:
        return FlowSummary()

    by_class: Dict[str, MethodContext] = {c.class_name: c for c in nodes}
    changed_names = set(by_class)

    # edges: caller -> callees that are also part of this change
    edges: Dict[str, List[str]] = {
        c.class_name: [b for b in c.dependency_beans if b in changed_names and b != c.class_name]
        for c in nodes
    }
    indegree: Dict[str, int] = {name: 0 for name in by_class}
    for callees in edges.values():
        for callee in callees:
            indegree[callee] += 1

    # Kahn's algorithm, ties broken by layer rank then class name.
    ready = sorted([n for n, d in indegree.items() if d == 0],
                   key=lambda n: (_rank(by_class[n]), n))
    ordered: List[str] = []
    while ready:
        name = ready.pop(0)
        ordered.append(name)
        for callee in edges.get(name, []):
            indegree[callee] -= 1
            if indegree[callee] == 0:
                ready.append(callee)
        ready.sort(key=lambda n: (_rank(by_class[n]), n))

    # Any node left over sat in a cycle — append it deterministically.
    for name in sorted(changed_names - set(ordered),
                       key=lambda n: (_rank(by_class[n]), n)):
        ordered.append(name)

    steps = [by_class[n].target + "()" if by_class[n].method_name else by_class[n].target
             for n in ordered]

    # Collaborators that were NOT changed but participate in the flow.
    external: List[str] = []
    for name in ordered:
        for bean in by_class[name].dependency_beans:
            if bean not in changed_names and bean not in external:
                external.append(bean)

    return FlowSummary(steps=steps, entry_point=ordered[0] if ordered else "",
                       external_beans=external)


# --------------------------------------------------------------------------- #
# MCP tool
# --------------------------------------------------------------------------- #

FLOW_SCHEMA = {
    "type": "object",
    "properties": {
        "contexts": {
            "type": "array",
            "description": "ast_change_context 결과를 그대로 전달",
            "items": {"type": "object"},
        },
    },
    "required": ["contexts"],
}


def tool_flow_summary(args: dict) -> dict:
    contexts = [MethodContext.from_dict(d) for d in args.get("contexts", [])]
    flow = build_flow(contexts)
    return {**flow.to_dict(), "summary": flow.summary}


def register(registry) -> None:
    registry.register(
        "flow_summary",
        "여러 변경 대상의 호출 순서(Controller → Service → Repository)와 외부 의존 "
        "Bean을 정리해, 하나의 비즈니스 흐름 테스트를 작성할 순서를 제공합니다.",
        FLOW_SCHEMA, tool_flow_summary,
    )
