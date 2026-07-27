"""LLM client interface used by the reasoning + generation stages."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List

from ..models import ChangeUnit, ReasoningTrace


@dataclass
class TestGenRequest:
    """Everything the LLM needs to reason about and generate a test.

    ``units`` are bundled together so that, per the spec, a change spanning
    several files is covered by a single business-flow test.
    """

    __test__ = False   # not a pytest test class despite the name

    units: List[ChangeUnit]
    project_package: str
    diffs_by_file: dict = field(default_factory=dict)   # path -> diff text
    feature_summary: str = ""


class LLMClient(ABC):
    """Abstract LLM. Implementations must be deterministic-friendly."""

    name: str = "abstract"

    @abstractmethod
    def reason(self, req: TestGenRequest) -> ReasoningTrace:
        """Produce a chain-of-thought (steps + scenarios + rationale)."""

    @abstractmethod
    def generate_test(self, req: TestGenRequest, reasoning: ReasoningTrace) -> str:
        """Return full Java source for a @SpringBootTest class."""
