"""
LLM 판단 전담 계층 (Agent).

정의서에서 **LLM 이 판단해야 하는 것**만 여기서 처리한다.
  · (2) "변경된 코드 내용이 기능 추가/ 조건 변경/ 성능 개선 등의 의도 파악을 해야함"
  · (3) "변경된 코드의 의도에 따라 정상 케이스, 실패 케이스를 판단"
  · (3) "여러 파일이 수정된 경우 변경된 비즈니스 흐름 전체를 묶어 하나의 테스트로"
  · [상세 2] "사고의 사슬 유도 기술을 사용하여 생각하는 과정을 먼저 적는다"
  · [상세 3] "적어둔 생각 과정과 프로젝트 개요를 사용하여 Test Code 생성"
  · [UI 4] 기능 중요도 High / Mid / Low
  · [UI 3] 결과의 적절성 여부 판단과 근거

코드로 확정되는 사실(변경 단위, 영향도, 실행 결과)은 MCP 가 만들어 준 것을
프롬프트 입력으로 쓸 뿐, 여기서 다시 계산하지 않는다.
"""

from __future__ import annotations

import re

from app.llm import llm_client, split_sections
from app.schemas import GenerateResponse, ReportResponse

#: MCP 영향도 등급 → 기능 중요도 표기 (정의서 UI (4))
_RISK_TO_IMPORTANCE = {"HIGH": "HIGH", "MEDIUM": "MID", "LOW": "LOW"}

#: 정의서가 예시로 든 의도 분류
_INTENT_KINDS = ("기능 추가", "조건 변경", "성능 개선", "버그 수정", "리팩터링")


_GENERATE_SYSTEM = """당신은 Spring Boot 프로젝트를 담당하는 시니어 테스트 엔지니어입니다.
변경된 코드를 분석해 **의도를 파악**하고, 그 의도에 맞는 **@SpringBootTest 테스트 코드**를 작성합니다.

# 반드시 지킬 순서 (사고의 사슬)
먼저 ## THINKING 에 생각하는 과정을 적습니다. 결론을 먼저 쓰지 마십시오.
THINKING 에는 다음을 순서대로 적습니다.
  1. Diff 에서 무엇이 바뀌었는가 (추가/삭제/조건 변경된 지점)
  2. AST 가 식별한 변경 단위와 영향 범위가 의미하는 것
  3. 이 변경으로 새로 생긴 실행 경로와 경계값
  4. 그래서 무엇을 검증해야 하는가
그 다음에만 나머지 섹션을 채웁니다.

# 의도 파악
변경 의도를 다음 중에서 고릅니다: 기능 추가 | 조건 변경 | 성능 개선 | 버그 수정 | 리팩터링
근거는 Diff 의 구체적인 코드를 인용해 제시합니다.

# 테스트 작성 규칙
- 반드시 **Spring Boot @SpringBootTest 통합 테스트**로 작성합니다.
- JUnit 5(org.junit.jupiter.api.Test)와 @Autowired 주입을 사용합니다.
- 변경 의도에 따라 **정상 케이스와 실패 케이스를 모두** 만듭니다.
  조건 변경이면 경계값(임계값 바로 아래/같음/위)을 반드시 포함합니다.
- **여러 파일이 수정된 경우, 파일별로 나누지 말고 변경된 비즈니스 흐름 전체를
  묶어 하나의 테스트 클래스로** 만듭니다.
- 프로젝트에 실제로 존재하는 클래스/메서드만 호출합니다. 없는 API 를 지어내지 않습니다.
- package 선언은 테스트 대상과 같은 패키지로 맞춥니다.

아래 섹션 구조로만 출력합니다. 다른 문장은 쓰지 않습니다.

## THINKING
- 생각하는 과정 (한 줄에 하나)

## INTENT
기능 추가 | 조건 변경 | 성능 개선 | 버그 수정 | 리팩터링 중 하나

## INTENT_RATIONALE
- 의도를 그렇게 판단한 근거 (한 줄에 하나, 변경된 코드를 인용)

## IMPORTANCE
HIGH | MID | LOW 중 하나

## IMPORTANCE_RATIONALE
- 중요도 판단 근거 (한 줄에 하나)

## TEST_CASES
- [정상] 케이스 설명
- [실패] 케이스 설명

## TEST_CODE
```java
@SpringBootTest 를 사용한 실행 가능한 Java 테스트 클래스 전문
```

## TEST_RATIONALE
- 이 테스트를 이렇게 작성한 근거 (한 줄에 하나)
"""

_REPORT_SYSTEM = """당신은 Spring Boot 프로젝트를 담당하는 시니어 테스트 엔지니어입니다.
@SpringBootTest 실행 결과를 보고 **결과가 적절한지** 판단합니다.

- Gradle exit code, JUnit 집계(성공/실패 수), 실패 메시지를 근거로 삼습니다.
- 통과했더라도 변경 의도를 검증하지 못했다면 '부적절' 로 판단합니다.
- JaCoCo 커버리지가 있으면 변경 지점이 실제로 실행되었는지 판단에 함께 씁니다.
- 실패했다면 원인을 실행 출력에서 인용해 설명합니다.
- 컴파일 실패/컨텍스트 로딩 실패는 테스트 실패와 구분해 설명합니다.

아래 섹션 구조로만 출력합니다.

## VERDICT
적절 | 부적절

## VERDICT_RATIONALE
- 판단 근거 (한 줄에 하나)

## DETAILS
- 결과 값 요약 (실패 원인, 검증된 항목, 커버리지 등)
"""


# ---------------------------------------------------------------------------
#  생성 (정의서 (2) 의도 파악 + (3) 테스트 생성 + [상세] 2·3 CoT)
# ---------------------------------------------------------------------------
def generate(
    analysis: dict, sources: list[tuple[str, str]], scope: str, project_name: str
) -> GenerateResponse:
    """
    MCP 가 식별한 변경 사실 + 프로젝트 개요를 근거로 Test Code 를 생성한다.

    :param analysis: MCP `/analysis/changes` 응답 (변경 단위·영향도·개요)
    :param sources:  변경 파일 [(경로, 본문)]
    :param scope:    staged / unstaged / worktree
    """
    prior_importance = _RISK_TO_IMPORTANCE.get(analysis.get("risk", "LOW"), "LOW")
    prior_reason = " / ".join(analysis.get("risk_reasons") or [])
    target_code = "\n\n".join(f"### {path}\n```java\n{body}\n```" for path, body in sources)

    user_prompt = "\n\n".join([
        _project_section(analysis, project_name, scope),
        _changed_units_section(analysis),
        _impact_section(analysis),
        f"# 변경 Diff\n```diff\n{_clip(_diff_of(analysis), 20000)}\n```",
        f"# 테스트 대상 코드\n{_clip(target_code, 40000)}",
        "위 변경에 대해 생각하는 과정을 먼저 적고, 그 결과로 @SpringBootTest 테스트를 작성하십시오.",
    ])

    sections = split_sections(llm_client.complete(_GENERATE_SYSTEM, user_prompt).text)

    return GenerateResponse(
        thinking=(sections.get("THINKING") or "").strip(),
        intent=_normalize_intent(sections.get("INTENT")),
        intent_rationale=(sections.get("INTENT_RATIONALE") or "").strip(),
        importance=_normalize_importance(sections.get("IMPORTANCE"), prior_importance),
        importance_rationale=(sections.get("IMPORTANCE_RATIONALE") or prior_reason).strip(),
        test_cases=(sections.get("TEST_CASES") or "").strip(),
        test_code=_extract_code(sections.get("TEST_CODE") or ""),
        rationale=(sections.get("TEST_RATIONALE") or "").strip(),
        target_code=target_code,
        base_package=analysis.get("base_package"),
        graph_ready=bool(analysis.get("graph_ready", True)),
        analysis_warnings=list(analysis.get("warnings") or []),
    )


# ---------------------------------------------------------------------------
#  판정 (정의서 UI (3) + (2) 의도를 결과값에 포함)
# ---------------------------------------------------------------------------
def report(
    execution: dict,
    test_code: str,
    intent: str = "",
    intent_rationale: str = "",
) -> ReportResponse:
    """
    MCP 실행 결과를 받아 적절성을 판단한다.

    정의서 (2): "파악한 의도와 근거에 대한 내용을 <Test Result 보기>의 결과값에 넣는다"
    → 판정 응답에 intent / intent_rationale 을 그대로 실어 보낸다.
    """
    exit_code = int(execution.get("exit_code", 1))
    user_prompt = "\n\n".join([
        f"# 파악한 변경 의도\n{intent or '-'}\n근거:\n{intent_rationale or '-'}",
        f"# 실행한 테스트 코드\n```java\n{_clip(test_code, 20000)}\n```",
        _execution_section(execution),
        f"# 실행 출력\n```\n{_clip(execution.get('output') or '', 20000)}\n```",
    ])
    sections = split_sections(llm_client.complete(_REPORT_SYSTEM, user_prompt).text)

    return ReportResponse(
        # exit code 와 JUnit 집계는 사실이므로 LLM 판정보다 우선한다.
        result="PASS" if exit_code == 0 else "FAIL",
        verdict=_first_line(sections.get("VERDICT")),
        verdict_rationale=(sections.get("VERDICT_RATIONALE") or "").strip(),
        details=(sections.get("DETAILS") or "").strip(),
        intent=intent,
        intent_rationale=intent_rationale,
        passed=int(execution.get("passed", 0)),
        failed=int(execution.get("failed", 0)),
        skipped=int(execution.get("skipped", 0)),
        total=int(execution.get("total", 0)),
        failures=list(execution.get("failures") or []),
        coverage=execution.get("coverage"),
        jacoco_enabled=bool(execution.get("jacoco_enabled", False)),
        springboot_applied=bool(execution.get("springboot_applied", False)),
        applied=list(execution.get("applied") or []),
        test_file_path=execution.get("test_file_path") or "",
        exit_code=exit_code,
        output=execution.get("output") or "",
    )


# ---------------------------------------------------------------------------
#  프롬프트 조립 — MCP 가 준 사실을 근거로 넣는다
# ---------------------------------------------------------------------------
def _project_section(analysis: dict, project_name: str, scope: str) -> str:
    frameworks = ", ".join(analysis.get("frameworks") or []) or "-"
    lines = [
        "# 프로젝트 개요 (MCP 가 AST 로 수집)",
        f"- 이름: {project_name} (변경 범위: {scope})",
        f"- 프레임워크: {frameworks}",
        f"- 기준 패키지: {analysis.get('base_package') or '-'}",
    ]
    if not analysis.get("graph_ready", True):
        lines.append("- 주의: 개요 수집이 완료되지 않아 AST 정보가 부족할 수 있음")
    return "\n".join(lines)


def _changed_units_section(analysis: dict) -> str:
    """MCP 가 Diff+AST 로 확정한 변경 코드 단위."""
    units = analysis.get("changed_units") or []
    if not units:
        return "# 변경된 코드 단위 (AST)\n- (식별된 단위 없음 — Diff 를 직접 해석하십시오)"

    lines = ["# 변경된 코드 단위 (Git Diff + AST 로 확정)"]
    for unit in units[:40]:
        location = f"{unit.get('file_path')}:{unit.get('start_line')}-{unit.get('end_line')}"
        entry = ""
        if unit.get("entrypoint"):
            entry = f" [진입점 {unit.get('http_method') or ''} {unit.get('route') or ''}".rstrip() + "]"
        lines.append(f"- {unit.get('node_type')} {unit.get('qualified_name')}{entry}  ({location})")
        if unit.get("signature"):
            lines.append(f"    시그니처: {unit['signature']}")
    return "\n".join(lines)


def _impact_section(analysis: dict) -> str:
    """영향도 — 하나의 테스트로 묶어야 할 비즈니스 흐름의 근거."""
    impacted = analysis.get("impacted_units") or []
    lines = [
        "# 영향 범위 (그래프 추적)",
        f"- 등급: {analysis.get('risk', 'LOW')} (점수 {analysis.get('risk_score', 0)})",
    ]
    for reason in analysis.get("risk_reasons") or []:
        lines.append(f"- 근거: {reason}")
    for unit in impacted[:30]:
        lines.append(
            f"- {unit.get('depth')}-Depth {unit.get('qualified_name')} "
            f"({unit.get('file_path')}, via {unit.get('via')})"
        )
    files = analysis.get("affected_files") or []
    if len(files) > 1:
        lines.append(
            f"- 여러 파일({len(files)}개)이 얽혀 있으므로 비즈니스 흐름 전체를 "
            "하나의 테스트로 묶으십시오."
        )
    return "\n".join(lines)


def _execution_section(execution: dict) -> str:
    lines = [
        "# 실행 결과 (MCP 가 Gradle + JaCoCo 로 실행한 사실)",
        f"- gradle exit code: {execution.get('exit_code')}",
        f"- @SpringBootTest 적용: {execution.get('springboot_applied')}",
        f"- 테스트 총 {execution.get('total', 0)}건 / 성공 {execution.get('passed', 0)} "
        f"/ 실패 {execution.get('failed', 0)} / 건너뜀 {execution.get('skipped', 0)}",
    ]
    for failure in (execution.get("failures") or [])[:20]:
        lines.append(f"- 실패: {failure}")

    coverage = execution.get("coverage")
    if coverage:
        lines.append(
            f"- JaCoCo 커버리지: 라인 {coverage.get('line_rate')}% "
            f"({coverage.get('line_covered')}/{coverage.get('line_covered', 0) + coverage.get('line_missed', 0)}), "
            f"분기 {coverage.get('branch_rate')}%"
        )
    elif not execution.get("jacoco_enabled"):
        lines.append("- JaCoCo: 프로젝트 build 설정에 적용되어 있지 않아 커버리지 없음")
    return "\n".join(lines)


def _diff_of(analysis: dict) -> str:
    """분석 응답에 실린 원본 diff (없으면 변경 라인 요약으로 대체)."""
    diff = analysis.get("diff")
    if diff:
        return diff
    ranges = analysis.get("changed_ranges") or {}
    return "\n".join(
        f"{path}: " + ", ".join(f"{start}-{end}" for start, end in spans)
        for path, spans in ranges.items()
    )


# ---------------------------------------------------------------------------
#  응답 정규화
# ---------------------------------------------------------------------------
def _clip(text: str, limit: int) -> str:
    """토큰 폭주 방지용 단순 절단."""
    return text if len(text) <= limit else text[:limit] + "\n… (이하 생략)"


def _first_line(value: str | None) -> str:
    stripped = (value or "").strip()
    return stripped.splitlines()[0] if stripped else ""


def _normalize_intent(value: str | None) -> str:
    """정의서가 예시로 든 분류 중 하나로 맞춘다. 못 맞추면 응답 첫 줄을 그대로 쓴다."""
    text = (value or "").strip()
    for kind in _INTENT_KINDS:
        if kind in text:
            return kind
    return _first_line(text)


def _normalize_importance(value: str | None, fallback: str) -> str:
    text = (value or "").upper()
    for level in ("HIGH", "MID", "LOW"):
        if level in text:
            return level
    return "MID" if "MEDIUM" in text else fallback


def _extract_code(block: str) -> str:
    """```fence``` 안의 코드만 뽑는다. 펜스가 없으면 원문 그대로."""
    match = re.search(r"```[a-zA-Z0-9_+-]*\n(.*?)```", block, re.DOTALL)
    return (match.group(1) if match else block).strip()
