"""
Workflow 생성기.

정의서:
  · "Project별로 workflow를 생성 후 DB에 저장합니다."
  · "workflow 생성할 때 LLM 토큰 소비 없이 AST 파싱(Tree-sitter)을 통해 진행합니다."
  · "(3) Flow별 실행 파일 표시 — 기능별 정보를 갖고 있는 파일이 무엇이고,
     물리적/논리적 경로가 어떻게 되는지 즉시 알 수 있어야 한다."

동작
  1. 그래프에서 진입점(entrypoint) 메서드를 모은다.
     · Spring @GetMapping/@PostMapping…, NestJS @Get/@Post, Express router.get,
       FastAPI/Flask @app.get, main(), @Scheduled 등
  2. 각 진입점에서 Calls / Executes 간선을 따라 DFS 로 실행 흐름을 추적한다.
  3. 각 단계에 물리 경로(file_path)와 논리 경로(qualified_name)를 함께 기록한다.
  4. mermaid 다이어그램 문자열을 함께 생성해 UI 가 렌더만 하면 되도록 한다.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_logger
from app.db import EdgeType, GraphEdge, GraphNode, NodeType, Workflow
from app.graph.store import GraphStore

logger = get_logger(__name__)

#: 하나의 Flow 를 추적할 최대 깊이 (너무 깊으면 다이어그램 가독성이 떨어진다)
MAX_FLOW_DEPTH = 6
#: 하나의 Flow 최대 단계 수
MAX_FLOW_STEPS = 60


@dataclass
class FlowStep:
    step_id: str
    node_id: str
    node_type: str
    label: str
    physical_path: str
    logical_path: str
    start_line: int | None
    end_line: int | None
    kind: str
    depth: int
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "physical_path": self.physical_path,
            "logical_path": self.logical_path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "kind": self.kind,
            "depth": self.depth,
            "meta": self.meta,
        }


class WorkflowGenerator:
    """프로젝트 그래프로부터 Flow 목록을 생성/갱신한다."""

    def __init__(self, db: Session, project_id: str) -> None:
        self.db = db
        self.project_id = project_id
        self.store = GraphStore(db, project_id)

    # ------------------------------------------------------------------
    def generate(self) -> list[Workflow]:
        """
        전체 워크플로우를 재생성한다.

        기존 Workflow 행과 key(진입점 논리 경로) 기준으로 대조해
        추가(ADDED) / 수정(MODIFIED) / 삭제(DELETED) 를 표시한다.
        """
        nodes = {node.id: node for node in self.store.all_nodes()}
        outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
        for edge in self.store.all_edges():
            if edge.edge_type in {EdgeType.CALLS.value, EdgeType.EXECUTES.value}:
                outgoing[edge.source_id].append(edge)

        entrypoints = [
            node
            for node in nodes.values()
            if node.node_type == NodeType.METHOD.value and (node.meta or {}).get("entrypoint")
        ]
        # 진입점이 하나도 없으면(라이브러리성 프로젝트) 호출당하지 않는 public 메서드를
        # 대체 진입점으로 사용한다.
        if not entrypoints:
            entrypoints = self._fallback_entrypoints(nodes)

        existing: dict[str, Workflow] = {
            wf.key: wf
            for wf in self.db.scalars(
                select(Workflow).where(Workflow.project_id == self.project_id)
            )
        }

        generated_keys: set[str] = set()
        results: list[Workflow] = []

        for entry in entrypoints:
            steps, transitions = self._trace(entry, nodes, outgoing)
            key = entry.qualified_name
            generated_keys.add(key)

            name = _flow_name(entry)
            files = sorted({step.physical_path for step in steps if step.physical_path})
            steps_payload = [step.to_dict() for step in steps]

            workflow = existing.get(key)
            if workflow is None:
                workflow = Workflow(
                    project_id=self.project_id,
                    key=key,
                    name=name,
                    entry_node_id=entry.id,
                    steps=steps_payload,
                    transitions=transitions,
                    involved_files=files,
                    last_change="ADDED",
                )
                self.db.add(workflow)
            else:
                changed = (
                    workflow.steps != steps_payload
                    or workflow.transitions != transitions
                    or workflow.name != name
                )
                workflow.name = name
                workflow.entry_node_id = entry.id
                workflow.steps = steps_payload
                workflow.transitions = transitions
                workflow.involved_files = files
                workflow.last_change = "MODIFIED" if changed else "UNCHANGED"
            results.append(workflow)

        # 이번 생성에서 사라진 Flow → 삭제 표시 후 제거
        for key, workflow in existing.items():
            if key not in generated_keys:
                workflow.last_change = "DELETED"
                self.db.delete(workflow)

        self.db.flush()
        return results

    # ------------------------------------------------------------------
    def _trace(
        self,
        entry: GraphNode,
        nodes: dict[str, GraphNode],
        outgoing: dict[str, list[GraphEdge]],
    ) -> tuple[list[FlowStep], list[dict]]:
        """진입점에서 시작해 호출 체인을 DFS 로 따라간다 (사이클 방지 포함)."""
        steps: list[FlowStep] = []
        transitions: list[dict] = []
        step_ids: dict[str, str] = {}   # node_id → step_id
        visited: set[str] = set()

        def register(node: GraphNode, depth: int, kind: str) -> str:
            if node.id in step_ids:
                return step_ids[node.id]
            step_id = f"S{len(steps)}"
            step_ids[node.id] = step_id
            steps.append(
                FlowStep(
                    step_id=step_id,
                    node_id=node.id,
                    node_type=node.node_type,
                    label=_step_label(node),
                    physical_path=node.file_path,
                    logical_path=node.qualified_name,
                    start_line=node.start_line,
                    end_line=node.end_line,
                    kind=kind,
                    depth=depth,
                    meta={
                        "signature": node.signature,
                        "http_method": (node.meta or {}).get("http_method"),
                        "route": (node.meta or {}).get("route"),
                        "operation": (node.meta or {}).get("operation"),
                        "tables": (node.meta or {}).get("tables"),
                        "language": node.language,
                    },
                )
            )
            return step_id

        def walk(node: GraphNode, depth: int) -> None:
            if depth > MAX_FLOW_DEPTH or len(steps) >= MAX_FLOW_STEPS:
                return
            if node.id in visited:
                return
            visited.add(node.id)
            source_step = step_ids[node.id]

            # 호출 순서를 소스 라인 순으로 정렬해 다이어그램이 실행 순서를 따르게 한다.
            edges = sorted(
                outgoing.get(node.id, []),
                key=lambda e: (e.meta or {}).get("line") or 0,
            )
            for edge in edges:
                target = nodes.get(edge.target_id)
                if target is None:
                    continue
                kind = "sql" if edge.edge_type == EdgeType.EXECUTES.value else "call"
                target_step = register(target, depth + 1, kind)
                transitions.append(
                    {
                        "source": source_step,
                        "target": target_step,
                        "edge_type": edge.edge_type,
                    }
                )
                if target.node_type != NodeType.SQL.value:
                    walk(target, depth + 1)

        register(entry, 0, "entry")
        walk(entry, 0)
        # 중복 전이 제거 (동일 호출이 여러 번 등장할 수 있음)
        unique = {(t["source"], t["target"], t["edge_type"]): t for t in transitions}
        return steps, list(unique.values())

    def _fallback_entrypoints(self, nodes: dict[str, GraphNode]) -> list[GraphNode]:
        """
        명시적 진입점이 없을 때: 아무도 호출하지 않는 메서드를 진입점으로 본다.

        (라이브러리/유틸 성격 프로젝트에서도 Flow 를 만들 수 있게 하는 안전장치)
        """
        called: set[str] = {
            edge.target_id
            for edge in self.store.all_edges()
            if edge.edge_type == EdgeType.CALLS.value
        }
        candidates = [
            node
            for node in nodes.values()
            if node.node_type == NodeType.METHOD.value and node.id not in called
        ]
        # 너무 많으면 상위 50개만 (호출 대상이 많은 순)
        outdegree: dict[str, int] = defaultdict(int)
        for edge in self.store.all_edges():
            outdegree[edge.source_id] += 1
        candidates.sort(key=lambda n: outdegree[n.id], reverse=True)
        return [node for node in candidates[:50] if outdegree[node.id] > 0]


# ---------------------------------------------------------------------------
#  표시용 헬퍼
# ---------------------------------------------------------------------------
def _flow_name(entry: GraphNode) -> str:
    """Flow 이름 — HTTP 엔드포인트면 'POST /api/users/login' 형태로 만든다."""
    meta = entry.meta or {}
    http_method = meta.get("http_method")
    route = meta.get("route")
    if http_method and route:
        return f"{http_method} {route}"
    owner = (meta.get("owner") or "").split("::")[-1].split(".")[-1]
    return f"{owner}.{entry.name}" if owner else entry.name


def _step_label(node: GraphNode) -> str:
    """다이어그램 노드에 표시할 짧은 라벨."""
    meta = node.meta or {}
    if node.node_type == NodeType.SQL.value:
        operation = meta.get("operation") or "SQL"
        tables = meta.get("tables") or []
        return f"{operation} {tables[0]}" if tables else f"{operation} {node.name}"
    owner = (meta.get("owner") or "").split("::")[-1].split(".")[-1]
    return f"{owner}.{node.name}" if owner else node.name


def to_mermaid(workflow: Workflow) -> str:
    """
    Workflow → mermaid flowchart 정의문.

    노드 종류별로 모양/색을 달리해 진입점·SQL 을 눈으로 구분할 수 있게 한다.
    """
    lines = ["flowchart TD"]
    steps = {step["step_id"]: step for step in (workflow.steps or [])}

    for step_id, step in steps.items():
        label = _escape_mermaid(step.get("label") or step_id)
        kind = step.get("kind")
        if kind == "entry":
            lines.append(f'    {step_id}(["{label}"]):::entry')
        elif kind == "sql" or step.get("node_type") == NodeType.SQL.value:
            lines.append(f'    {step_id}[("{label}")]:::sql')
        else:
            lines.append(f'    {step_id}["{label}"]:::call')

    for transition in workflow.transitions or []:
        source = transition.get("source")
        target = transition.get("target")
        if source not in steps or target not in steps:
            continue
        edge_type = transition.get("edge_type", "")
        arrow = "-.->|Executes|" if edge_type == EdgeType.EXECUTES.value else "-->"
        lines.append(f"    {source} {arrow} {target}")

    lines += [
        "    classDef entry fill:#1f6feb,stroke:#0b2e6f,color:#ffffff,font-weight:bold;",
        "    classDef call fill:#e6f0ff,stroke:#3b82f6,color:#0f172a;",
        "    classDef sql fill:#fff4e0,stroke:#f59e0b,color:#7c2d12;",
    ]
    return "\n".join(lines)


def _escape_mermaid(text: str) -> str:
    """mermaid 라벨에서 문제를 일으키는 문자를 정리한다."""
    cleaned = re.sub(r'["\n\r]', " ", text).strip()
    return cleaned[:60] or "step"
