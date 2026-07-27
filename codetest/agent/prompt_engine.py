"""One-Shot CoT 프롬프트 빌더 + 응답 파서.

This is where the "두 번 묻지 않는다" rule is enforced. A single prompt asks
for the intent/importance analysis *and* the test code, and a single response
carries both. :func:`parse_response` turns that response into
:class:`~codetest.models.CombinedAnalysis`.

Every backend — mock or real — goes through the same two functions, so the
response contract is exercised on every run rather than only in production.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence

from ..models import (ChangeUnit, CombinedAnalysis, ReasoningTrace, UnitAnalysis)
from . import intent_rules
from .llm.base_client import TestGenRequest

SYSTEM_PROMPT = """\
당신은 Spring Boot 프로젝트의 변경 사항을 분석해 @SpringBootTest 통합 테스트를
작성하는 시니어 테스트 엔지니어입니다.

반드시 한 번의 응답에서 다음 두 가지를 **동시에** 산출하세요.
  1) 변경 유닛별 의도(feature/condition/performance/modification)와
     중요도(High/Mid/Low), 그리고 각각의 판단 근거
  2) 위 분석에 기반한 컴파일 가능한 @SpringBootTest JUnit5 테스트 클래스 1개

응답은 아래 JSON 스키마 하나만 출력합니다(설명 문장 금지).
"""

RESPONSE_SCHEMA = """\
{
  "analyses": [
    {"unit_key": "<Class#method>", "intent": "feature|condition|performance|modification",
     "intent_reason": "<판단 근거>", "importance": "High|Mid|Low",
     "importance_reason": "<판단 근거>"}
  ],
  "reasoning": {
    "steps": ["<사고 과정 1>", "..."],
    "scenarios": ["성공: ...", "실패: ..."],
    "rationale": "<테스트를 이렇게 작성한 이유>"
  },
  "test_code": "package ...;\\n@SpringBootTest\\nclass XxxGeneratedTest { ... }"
}
"""


def build_prompt(req: TestGenRequest) -> str:
    """Assemble the single user prompt.

    Note what is *not* here: no full file source, no raw diff blob. Only the
    pruned MCP context and the changed lines, so prompt size tracks the size of
    the change rather than the size of the project.
    """
    units_block = "\n".join(
        f"- {u.display_name} ({u.file_path})"
        f"{' [새 파일]' if u.is_new_file else ''}"
        f"{' [새 메서드]' if u.is_new_method and not u.is_new_file else ''}"
        for u in req.units
    )
    flow_block = (
        f"\n[호출 흐름(다중 파일)]\n{req.flow.summary}\n"
        f"외부 의존 Bean: {', '.join(req.flow.external_beans) or '없음'}\n"
        if req.flow and req.flow.steps else ""
    )

    return f"""\
[변경 유닛]
{units_block}

[AST MCP 컨텍스트 — 시그니처 / 의존 Bean / 호출 순서]
{req.prompt_context()}
{flow_block}
[변경 라인(diff 발췌)]
{req.changed_lines_excerpt() or '(변경 라인 없음)'}

[프로젝트 정보]
- 테스트 패키지: {req.project_package}
- {req.feature_summary or '추가 정보 없음'}

[요구 사항]
- 변경 유닛 전체를 하나의 비즈니스 흐름 테스트 클래스로 묶을 것
- 의도별로 성공 케이스와 실패 케이스를 최소 1개씩 도출할 것
- 의존 Bean은 @Autowired로 주입하고, 호출 순서를 반영해 검증할 것

[응답 형식]
{RESPONSE_SCHEMA}"""


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Pull the JSON object out of a response, tolerating ``` fences."""
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start:end + 1] if start != -1 and end > start else None
    if candidate is None:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_response(text: str, units: Sequence[ChangeUnit]) -> CombinedAnalysis:
    """Turn one raw LLM response into a :class:`CombinedAnalysis`.

    A malformed response degrades instead of crashing the run: the analysis
    falls back to the deterministic baseline and the caller sees an empty
    ``test_source`` it can report on.
    """
    payload = _extract_json(text) or {}

    analyses: List[UnitAnalysis] = [
        UnitAnalysis.from_dict(d) for d in payload.get("analyses", [])
        if isinstance(d, dict)
    ]
    if not analyses:
        analyses = intent_rules.baseline_analyses(units)

    reasoning = ReasoningTrace.from_dict(payload.get("reasoning") or {})
    return CombinedAnalysis(
        analyses=analyses,
        reasoning=reasoning,
        test_source=payload.get("test_code", "") or "",
        llm_calls=1,
    )


def render_response(analyses: Sequence[UnitAnalysis], reasoning: ReasoningTrace,
                    test_code: str) -> str:
    """Serialize a response in the contract format (used by the mock backend)."""
    return json.dumps({
        "analyses": [a.to_dict() for a in analyses],
        "reasoning": reasoning.to_dict(),
        "test_code": test_code,
    }, ensure_ascii=False)
