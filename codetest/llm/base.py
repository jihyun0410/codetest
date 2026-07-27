"""LLM client interface.

The analysis and the generation stages were merged into **one** API call:
:meth:`LLMClient.analyze_and_generate` sends a single request and gets back
both [의도/중요도 분석 근거] and [테스트 코드] in one response
(:class:`~codetest.models.CombinedAnalysis`).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List

from ..models import ChangeUnit, CombinedAnalysis, MethodContext


@dataclass
class TestGenRequest:
    """Everything the single LLM call needs.

    ``units`` are bundled together so that, per the spec, a change spanning
    several files is covered by a single business-flow test.

    ``contexts`` is what the AST MCP server forwarded — the changed method's
    signature, its dependency bean names and a call-order summary. The full
    source/AST is intentionally *not* part of the request.
    """

    __test__ = False   # not a pytest test class despite the name

    units: List[ChangeUnit]
    project_package: str
    contexts: List[MethodContext] = field(default_factory=list)
    feature_summary: str = ""

    def prompt_context(self) -> str:
        """The filtered AST block embedded in the request."""
        if not self.contexts:
            return "(AST MCP 컨텍스트 없음)"
        return "\n".join(c.as_prompt_block() for c in self.contexts)

    def changed_lines_excerpt(self, limit: int = 40) -> str:
        """Changed lines only — never the whole file."""
        out: List[str] = []
        for u in self.units:
            for line in u.added_lines[:limit]:
                out.append(f"+ {line}")
            for line in u.removed_lines[:limit]:
                out.append(f"- {line}")
        return "\n".join(out)


class LLMClient(ABC):
    """Abstract LLM. Implementations must be deterministic-friendly."""

    name: str = "abstract"

    @abstractmethod
    def analyze_and_generate(self, req: TestGenRequest) -> CombinedAnalysis:
        """One request → 의도/중요도 분석 근거 + @SpringBootTest 코드."""
