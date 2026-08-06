"""Test Code 생성 및 결과 판정.

기능 중요도는 그래프 영향도에서 나온 값을 1차 근거로 프롬프트에 넣고,
LLM 이 코드 내용을 보고 최종 판정한다. LLM 이 값을 안 주면 그래프 값이 그대로 쓰인다.
"""

from __future__ import annotations

import re
import shlex

from sqlalchemy.orm import Session

from app.db import Project, RiskLevel
from app.graph.impact import ImpactAnalyzer, parse_diff_ranges
from app.graph.store import GraphStore
from app.llm import llm_client, split_sections
from app.schemas import GenerateResponse, ReportResponse

#: 그래프 영향도 등급 → 기능 중요도 표기
_RISK_TO_IMPORTANCE = {
    RiskLevel.HIGH.value: "HIGH",
    RiskLevel.MEDIUM.value: "MID",
    RiskLevel.LOW.value: "LOW",
}

#: 언어별 기본 확장자 / 실행 명령
_RUNTIME = {
    "python": (".py", ["python", "-m", "pytest", "-q", "{file}"]),
    "javascript": (".test.js", ["npx", "--no-install", "vitest", "run", "{file}"]),
    "typescript": (".test.ts", ["npx", "--no-install", "vitest", "run", "{file}"]),
    "java": (".java", ["java", "{file}"]),
}

_GENERATE_SYSTEM = """당신은 시니어 테스트 엔지니어입니다.
주어진 변경 코드에 대해 **바로 실행 가능한 테스트 코드**를 작성합니다.

규칙
- 외부 네트워크·DB에 의존하지 않는 자기완결 테스트만 작성합니다. 필요하면 스텁/모의를 코드 안에 직접 둡니다.
- 테스트 대상이 import 불가능하면, 대상 로직을 테스트 파일 안에 재현하지 말고
  실제 동작을 검증할 수 있는 최소 범위로 좁힙니다.
- 기능 중요도는 사용자 노출 여부, 데이터 변경 여부, 파급 범위로 판단합니다.

아래 섹션 구조로만 출력합니다. 다른 문장은 쓰지 않습니다.

## IMPORTANCE
HIGH | MID | LOW 중 하나

## IMPORTANCE_RATIONALE
- 중요도 판단 근거 (한 줄에 하나)

## LANGUAGE
python | javascript | typescript | java 중 하나

## RUN_COMMAND
테스트 실행 명령 한 줄. 파일 경로 자리에 {file} 을 씁니다.

## TEST_CODE
```
실행 가능한 테스트 코드 전문
```

## TEST_RATIONALE
- 이 테스트를 이렇게 작성한 근거 (한 줄에 하나)
"""

_REPORT_SYSTEM = """당신은 시니어 테스트 엔지니어입니다.
테스트 실행 결과를 보고 결과가 적절한지 판단합니다.

- exit code 와 출력 내용을 근거로 PASS/FAIL 을 확정합니다.
- 통과했더라도 검증이 빈약하면 '부적절' 로 판단할 수 있습니다.
- 실패했다면 원인을 출력에서 인용해 설명합니다.

아래 섹션 구조로만 출력합니다.

## RESULT
PASS | FAIL

## VERDICT
적절 | 부적절

## VERDICT_RATIONALE
- 판단 근거 (한 줄에 하나)

## DETAILS
- 결과 값 요약 (실패 원인, 검증된 항목 등)
"""


def graph_importance(db: Session, project: Project, diff: str) -> tuple[str, str]:
    """그래프 영향도로 중요도 사전값을 계산한다 (LLM 미사용)."""
    report = ImpactAnalyzer(GraphStore(db, project.id)).analyze(parse_diff_ranges(diff))
    return _RISK_TO_IMPORTANCE.get(report.risk.value, "LOW"), " / ".join(report.reasons)


def generate(
    db: Session, project: Project, diff: str, sources: list[tuple[str, str]], scope: str
) -> GenerateResponse:
    """변경 파일에 대한 Test Code 를 생성한다."""
    prior_importance, prior_reason = graph_importance(db, project, diff)
    target_code = "\n\n".join(f"### {path}\n```\n{body}\n```" for path, body in sources)

    user_prompt = "\n\n".join([
        f"# 프로젝트\n{project.name} (변경 범위: {scope})",
        f"# 그래프 기반 사전 중요도\n{prior_importance}\n근거: {prior_reason or '-'}",
        f"# 변경 Diff\n```diff\n{_clip(diff, 20000)}\n```",
        f"# 테스트 대상 코드\n{_clip(target_code, 40000)}",
        "위 코드에 대한 테스트를 작성하십시오.",
    ])

    sections = split_sections(llm_client.complete(_GENERATE_SYSTEM, user_prompt).text)

    language = (sections.get("LANGUAGE") or "python").strip().lower().splitlines()[0]
    extension, default_command = _RUNTIME.get(language, _RUNTIME["python"])

    return GenerateResponse(
        importance=_normalize_importance(sections.get("IMPORTANCE"), prior_importance),
        importance_rationale=(sections.get("IMPORTANCE_RATIONALE") or prior_reason).strip(),
        language=language,
        file_extension=extension,
        run_command=_parse_command(sections.get("RUN_COMMAND")) or default_command,
        target_code=target_code,
        test_code=_extract_code(sections.get("TEST_CODE") or ""),
        rationale=(sections.get("TEST_RATIONALE") or "").strip(),
    )


def report(test_code: str, output: str, exit_code: int, language: str) -> ReportResponse:
    """테스트 실행 결과를 판정한다."""
    user_prompt = "\n\n".join([
        f"# 언어\n{language}",
        f"# 실행한 테스트 코드\n```\n{_clip(test_code, 20000)}\n```",
        f"# exit code\n{exit_code}",
        f"# 실행 출력\n```\n{_clip(output, 20000)}\n```",
    ])
    sections = split_sections(llm_client.complete(_REPORT_SYSTEM, user_prompt).text)

    return ReportResponse(
        # exit code 는 사실이므로 LLM 판정보다 우선한다.
        result="PASS" if exit_code == 0 else "FAIL",
        verdict=_first_line(sections.get("VERDICT")),
        verdict_rationale=(sections.get("VERDICT_RATIONALE") or "").strip(),
        details=(sections.get("DETAILS") or "").strip(),
    )


# ---------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    """토큰 폭주 방지용 단순 절단."""
    return text if len(text) <= limit else text[:limit] + "\n… (이하 생략)"


def _first_line(value: str | None) -> str:
    stripped = (value or "").strip()
    return stripped.splitlines()[0] if stripped else ""


def _normalize_importance(value: str | None, fallback: str) -> str:
    text = (value or "").upper()
    for level in ("HIGH", "MID", "LOW"):
        if level in text:
            return level
    return "MID" if "MEDIUM" in text else fallback


def _parse_command(value: str | None) -> list[str] | None:
    """RUN_COMMAND 한 줄을 인자 리스트로 분해한다."""
    line = _first_line(value).strip("`").strip()
    if not line:
        return None
    try:
        parts = shlex.split(line)
    except ValueError:
        return None
    return parts if "{file}" in parts else [*parts, "{file}"]


def _extract_code(block: str) -> str:
    """```fence``` 안의 코드만 뽑는다. 펜스가 없으면 원문 그대로."""
    match = re.search(r"```[a-zA-Z0-9_+-]*\n(.*?)```", block, re.DOTALL)
    return (match.group(1) if match else block).strip()
