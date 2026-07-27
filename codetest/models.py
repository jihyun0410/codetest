"""Shared data models passed between pipeline stages.

Everything here is a plain dataclass with no I/O dependencies: the CLI
session passes these objects between stages **in memory** (no DB round-trip),
so they double as the wire format for the AST MCP server (``to_dict`` /
``from_dict``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MethodInfo:
    """A method discovered by AST analysis."""

    name: str
    signature: str
    start_line: int
    end_line: int
    modifiers: List[str] = field(default_factory=list)
    return_type: str = "void"

    @property
    def full_signature(self) -> str:
        """``public double calculateTotal(Order order)`` — used by the MCP payload."""
        mods = " ".join(self.modifiers)
        parts = [p for p in (mods, self.return_type, self.signature) if p]
        return " ".join(parts)


@dataclass
class MethodContext:
    """The *filtered* AST payload handed to the LLM by the AST MCP server.

    The server deliberately drops the full AST/source and forwards only the
    three things the model actually needs to write a test:

    1. 수정된 대상 메서드의 시그니처   -> ``method_signature``
    2. 의존 Bean 클래스의 이름 목록    -> ``dependency_beans``
    3. 호출 순서 요약 텍스트           -> ``call_flow``
    """

    file_path: str
    class_name: str
    method_name: str
    method_signature: str
    dependency_beans: List[str] = field(default_factory=list)
    call_flow: str = ""

    @property
    def target(self) -> str:
        return f"{self.class_name}#{self.method_name}" if self.method_name else self.class_name

    def as_prompt_block(self) -> str:
        """Compact text block embedded in the single LLM request."""
        beans = ", ".join(self.dependency_beans) or "(없음)"
        return (
            f"- 대상: {self.target}\n"
            f"  · 시그니처: {self.method_signature}\n"
            f"  · 의존 Bean: {beans}\n"
            f"  · 호출 순서: {self.call_flow or '(호출 없음)'}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path,
            "class_name": self.class_name,
            "method_name": self.method_name,
            "method_signature": self.method_signature,
            "dependency_beans": list(self.dependency_beans),
            "call_flow": self.call_flow,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "MethodContext":
        return MethodContext(
            file_path=data.get("file_path", ""),
            class_name=data.get("class_name", ""),
            method_name=data.get("method_name", ""),
            method_signature=data.get("method_signature", ""),
            dependency_beans=list(data.get("dependency_beans", [])),
            call_flow=data.get("call_flow", ""),
        )


@dataclass
class ChangeUnit:
    """A single changed method (or file-level change) detected from a diff.

    This is the atomic unit the agent reasons about and generates a test for.
    """

    file_path: str          # repo-relative path, e.g. src/main/java/.../FooService.java
    class_name: str
    method: Optional[MethodInfo]
    changed_lines: List[int]
    added_lines: List[str]
    removed_lines: List[str]
    intent: str = "modification"   # feature | condition | performance | modification
    intent_reason: str = ""
    importance: str = "Low"        # High | Mid | Low
    importance_reason: str = ""
    is_new_file: bool = False
    is_new_method: bool = False
    context: Optional[MethodContext] = None    # filled by the AST MCP server

    @property
    def display_name(self) -> str:
        if self.method:
            return f"{self.class_name}#{self.method.name}"
        return self.class_name


@dataclass
class UnitAnalysis:
    """[의도/중요도 분석 근거] for one change unit, returned by the LLM."""

    unit_key: str               # matches ChangeUnit.display_name
    intent: str                 # feature | condition | performance | modification
    intent_reason: str
    importance: str             # High | Mid | Low
    importance_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_key": self.unit_key,
            "intent": self.intent,
            "intent_reason": self.intent_reason,
            "importance": self.importance,
            "importance_reason": self.importance_reason,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "UnitAnalysis":
        return UnitAnalysis(
            unit_key=data.get("unit_key", ""),
            intent=data.get("intent", "modification"),
            intent_reason=data.get("intent_reason", ""),
            importance=data.get("importance", "Low"),
            importance_reason=data.get("importance_reason", ""),
        )


@dataclass
class ReasoningTrace:
    """Chain-of-thought produced by the LLM layer before writing a test."""

    steps: List[str]
    scenarios: List[str]        # success/failure cases the test should cover
    rationale: str              # summary of why the test is written this way


@dataclass
class CombinedAnalysis:
    """Response of the **single** LLM call.

    One request → one response carrying both deliverables:
    ``analyses`` ([의도/중요도 분석 근거]) and ``test_source`` ([테스트 코드]).
    """

    analyses: List[UnitAnalysis]
    reasoning: ReasoningTrace
    test_source: str
    llm_calls: int = 1          # kept for reporting: always 1 by design


@dataclass
class TestArtifact:
    """A generated test class ready to be compiled/run."""

    class_name: str             # e.g. OrderServiceGeneratedTest
    package: str
    file_path: str              # absolute path where it was/will be written
    source: str                 # full Java source of the @SpringBootTest class
    reasoning: ReasoningTrace
    covered_units: List[ChangeUnit] = field(default_factory=list)
    analyses: List[UnitAnalysis] = field(default_factory=list)
    contexts: List[MethodContext] = field(default_factory=list)
    llm_calls: int = 1          # how many API calls produced this artifact


@dataclass
class TestResult:
    """Outcome of executing a TestArtifact."""

    passed: bool
    total: int
    failures: int
    errors: int
    skipped: int
    duration_s: float
    coverage_pct: Optional[float]   # from JaCoCo, None if unavailable
    executor: str                   # "gradle" | "simulated"
    log: str
    validity: str = "unknown"       # valid | invalid | inconclusive
    validity_reason: str = ""


@dataclass
class ReportItem:
    """One row in the Terminal UI: a change + its test + its result."""

    unit: ChangeUnit
    artifact: TestArtifact
    result: Optional[TestResult]


@dataclass
class Report:
    command: str
    project_dir: str
    items: List[ReportItem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
