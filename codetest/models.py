"""Shared domain models — the contract every layer speaks.

This module sits **above** all four layers on purpose: cli / agent / mcp /
storage all import from here, and it imports nothing from them. Keeping it at
the package root is what stops the layering from inverting (a storage-owned
model would force layers 1-3 to depend on layer 4).

Everything is a plain dataclass with no I/O, so the same objects serve as
in-memory pipeline payloads *and* as the MCP wire format (``to_dict`` /
``from_dict``).

Note the split from the DB schema: table DDL lives in
:mod:`codetest.storage.schema`. These models are the runtime contract, not
a persistence layout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Git layer
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DiffOptions:
    """How aggressively to filter noise out of the diff.

    ``ignore_whitespace``  - 들여쓰기/정렬 등 공백만 바뀐 변경 무시 (git ``-w``).
    ``ignore_blank_lines`` - 의미 없는 빈 줄 추가/삭제 무시 (git ``--ignore-blank-lines``).
    """

    ignore_whitespace: bool = True
    ignore_blank_lines: bool = True

    def git_flags(self) -> List[str]:
        flags: List[str] = []
        if self.ignore_whitespace:
            flags += ["--ignore-all-space", "--ignore-space-at-eol"]
        if self.ignore_blank_lines:
            flags.append("--ignore-blank-lines")
        return flags

    def to_dict(self) -> Dict[str, Any]:
        return {"ignore_whitespace": self.ignore_whitespace,
                "ignore_blank_lines": self.ignore_blank_lines}


DEFAULT_DIFF_OPTIONS = DiffOptions()


@dataclass
class FileDiff:
    path: str            # repo-relative
    diff_text: str
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)
    changed_new_lines: List[int] = field(default_factory=list)   # NEW-file line numbers
    is_new_file: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "diff_text": self.diff_text,
            "added_lines": self.added_lines, "removed_lines": self.removed_lines,
            "changed_new_lines": self.changed_new_lines, "is_new_file": self.is_new_file,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "FileDiff":
        return FileDiff(
            path=d.get("path", ""), diff_text=d.get("diff_text", ""),
            added_lines=list(d.get("added_lines", [])),
            removed_lines=list(d.get("removed_lines", [])),
            changed_new_lines=list(d.get("changed_new_lines", [])),
            is_new_file=bool(d.get("is_new_file", False)),
        )


@dataclass
class DiffScan:
    """Result of one scan: the meaningful diffs plus what was filtered out."""

    diffs: List[FileDiff] = field(default_factory=list)
    skipped_whitespace_only: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"diffs": [d.to_dict() for d in self.diffs],
                "skipped_whitespace_only": self.skipped_whitespace_only}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "DiffScan":
        return DiffScan(
            diffs=[FileDiff.from_dict(x) for x in d.get("diffs", [])],
            skipped_whitespace_only=list(d.get("skipped_whitespace_only", [])),
        )


# --------------------------------------------------------------------------- #
# AST layer
# --------------------------------------------------------------------------- #


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
        return " ".join(p for p in (mods, self.return_type, self.signature) if p)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "signature": self.signature,
            "start_line": self.start_line, "end_line": self.end_line,
            "modifiers": list(self.modifiers), "return_type": self.return_type,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MethodInfo":
        return MethodInfo(
            name=d.get("name", ""), signature=d.get("signature", ""),
            start_line=int(d.get("start_line", 0)), end_line=int(d.get("end_line", 0)),
            modifiers=list(d.get("modifiers", [])), return_type=d.get("return_type", "void"),
        )


@dataclass
class ClassInfo:
    """A parsed Java type and its methods."""

    name: str
    package: str
    methods: List[MethodInfo] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "package": self.package,
                "methods": [m.to_dict() for m in self.methods]}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ClassInfo":
        return ClassInfo(
            name=d.get("name", ""), package=d.get("package", ""),
            methods=[MethodInfo.from_dict(m) for m in d.get("methods", [])],
        )


@dataclass
class MethodContext:
    """The *pruned* AST payload the AST & Flow MCP server hands to the agent.

    The server drops the full AST/source and forwards only the three things the
    model actually needs to write a test:

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
            "file_path": self.file_path, "class_name": self.class_name,
            "method_name": self.method_name, "method_signature": self.method_signature,
            "dependency_beans": list(self.dependency_beans), "call_flow": self.call_flow,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "MethodContext":
        return MethodContext(
            file_path=d.get("file_path", ""), class_name=d.get("class_name", ""),
            method_name=d.get("method_name", ""),
            method_signature=d.get("method_signature", ""),
            dependency_beans=list(d.get("dependency_beans", [])),
            call_flow=d.get("call_flow", ""),
        )


@dataclass
class FlowSummary:
    """Cross-file call order for a bundled change (Controller → Service → …).

    Produced by the Flow tool when several files changed together, so the
    generated business-flow test follows the real invocation order.
    """

    steps: List[str] = field(default_factory=list)
    entry_point: str = ""
    external_beans: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return " → ".join(self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {"steps": list(self.steps), "entry_point": self.entry_point,
                "external_beans": list(self.external_beans)}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "FlowSummary":
        return FlowSummary(
            steps=list(d.get("steps", [])), entry_point=d.get("entry_point", ""),
            external_beans=list(d.get("external_beans", [])),
        )


# --------------------------------------------------------------------------- #
# Change / analysis
# --------------------------------------------------------------------------- #


@dataclass
class ChangeUnit:
    """A single changed method (or file-level change) detected from a diff.

    This is the atomic unit the agent reasons about and generates a test for.
    """

    file_path: str          # repo-relative, e.g. src/main/java/.../FooService.java
    class_name: str
    method: Optional[MethodInfo] = None
    changed_lines: List[int] = field(default_factory=list)
    added_lines: List[str] = field(default_factory=list)
    removed_lines: List[str] = field(default_factory=list)
    intent: str = "modification"   # feature | condition | performance | modification
    intent_reason: str = ""
    importance: str = "Low"        # High | Mid | Low
    importance_reason: str = ""
    is_new_file: bool = False
    is_new_method: bool = False
    context: Optional[MethodContext] = None    # pruned payload from the AST MCP server

    @property
    def display_name(self) -> str:
        if self.method:
            return f"{self.class_name}#{self.method.name}"
        return self.class_name


@dataclass
class ChangeAnalysis:
    """Stage 1-2 output, handed to stage 3 as variables (no DB round-trip)."""

    units: List[ChangeUnit] = field(default_factory=list)
    contexts: List[MethodContext] = field(default_factory=list)
    flow: Optional[FlowSummary] = None
    package: str = ""
    diffs_by_file: Dict[str, str] = field(default_factory=dict)
    skipped_whitespace_only: List[str] = field(default_factory=list)


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
            "unit_key": self.unit_key, "intent": self.intent,
            "intent_reason": self.intent_reason, "importance": self.importance,
            "importance_reason": self.importance_reason,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "UnitAnalysis":
        return UnitAnalysis(
            unit_key=d.get("unit_key", ""), intent=d.get("intent", "modification"),
            intent_reason=d.get("intent_reason", ""), importance=d.get("importance", "Low"),
            importance_reason=d.get("importance_reason", ""),
        )


@dataclass
class ReasoningTrace:
    """Chain-of-thought produced by the LLM layer before writing a test."""

    steps: List[str] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)   # success/failure cases
    rationale: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"steps": list(self.steps), "scenarios": list(self.scenarios),
                "rationale": self.rationale}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "ReasoningTrace":
        return ReasoningTrace(
            steps=list(d.get("steps", [])), scenarios=list(d.get("scenarios", [])),
            rationale=d.get("rationale", ""),
        )


@dataclass
class CombinedAnalysis:
    """Response of the **single** LLM call.

    One request → one response carrying both deliverables:
    ``analyses`` ([의도/중요도 분석 근거]) and ``test_source`` ([테스트 코드]).
    """

    analyses: List[UnitAnalysis] = field(default_factory=list)
    reasoning: ReasoningTrace = field(default_factory=ReasoningTrace)
    test_source: str = ""
    llm_calls: int = 1          # kept for reporting: always 1 by design


# --------------------------------------------------------------------------- #
# Test artifacts / results
# --------------------------------------------------------------------------- #


@dataclass
class TestArtifact:
    """A generated test class ready to be compiled/run."""

    __test__ = False            # not a pytest test class despite the name

    class_name: str             # e.g. OrderServiceGeneratedTest
    package: str
    file_path: str              # absolute path where it was/will be written
    source: str                 # full Java source of the @SpringBootTest class
    reasoning: ReasoningTrace = field(default_factory=ReasoningTrace)
    covered_units: List[ChangeUnit] = field(default_factory=list)
    analyses: List[UnitAnalysis] = field(default_factory=list)
    contexts: List[MethodContext] = field(default_factory=list)
    flow: Optional[FlowSummary] = None
    llm_calls: int = 1          # how many API calls produced this artifact


@dataclass
class TestResult:
    """Outcome of executing a TestArtifact."""

    __test__ = False            # not a pytest test class despite the name

    passed: bool
    total: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    coverage_pct: Optional[float] = None      # JaCoCo instruction coverage
    branch_coverage_pct: Optional[float] = None
    executor: str = "simulated"               # "gradle" | "simulated"
    log: str = ""
    validity: str = "unknown"                 # valid | invalid | inconclusive
    validity_reason: str = ""


@dataclass
class ReportItem:
    """One row in the Terminal UI: a change + its test + its result."""

    unit: ChangeUnit
    artifact: TestArtifact
    result: Optional[TestResult] = None


@dataclass
class Report:
    command: str
    project_dir: str
    items: List[ReportItem] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
