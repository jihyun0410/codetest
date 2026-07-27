"""Shared data models passed between pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MethodInfo:
    """A method discovered by AST analysis."""

    name: str
    signature: str
    start_line: int
    end_line: int
    modifiers: List[str] = field(default_factory=list)
    return_type: str = "void"


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

    @property
    def display_name(self) -> str:
        if self.method:
            return f"{self.class_name}#{self.method.name}"
        return self.class_name


@dataclass
class ReasoningTrace:
    """Chain-of-thought produced by the LLM layer before writing a test."""

    steps: List[str]
    scenarios: List[str]        # success/failure cases the test should cover
    rationale: str              # summary of why the test is written this way


@dataclass
class TestArtifact:
    """A generated test class ready to be compiled/run."""

    class_name: str             # e.g. OrderServiceGeneratedTest
    package: str
    file_path: str              # absolute path where it was/will be written
    source: str                 # full Java source of the @SpringBootTest class
    reasoning: ReasoningTrace
    covered_units: List[ChangeUnit] = field(default_factory=list)


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
