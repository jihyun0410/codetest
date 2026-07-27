"""Deterministic mock LLM.

Emulates what a real Claude client does in **one** round trip: the request
carries the change units plus the AST MCP context (시그니처 / 의존 Bean / 호출
순서), and the single response carries both deliverables:

1. ``analyses``    - 의도/중요도 분석 근거 for every change unit
2. ``test_source`` - a compilable ``@SpringBootTest`` JUnit 5 class

Everything is derived from the analyzed change so output is stable and
explainable — no randomness, no network.
"""
from __future__ import annotations

import re
from typing import Dict, List

from .. import intent
from ..models import ChangeUnit, CombinedAnalysis, ReasoningTrace, UnitAnalysis
from .base import LLMClient, TestGenRequest


def _camel_to_field(name: str) -> str:
    return name[0].lower() + name[1:] if name else name


def _primary_class(units: List[ChangeUnit]) -> str:
    # Prefer a service class as the unit-under-test, else the first changed class.
    for u in units:
        if "service" in u.file_path.lower():
            return u.class_name
    return units[0].class_name


class MockLLMClient(LLMClient):
    name = "mock"

    # ---- the single call ---------------------------------------------------
    def analyze_and_generate(self, req: TestGenRequest) -> CombinedAnalysis:
        """One call: analyze intent/importance *and* write the test."""
        analyses = intent.baseline_analyses(req.units)
        by_key = {a.unit_key: a for a in analyses}
        reasoning = self._reason(req, by_key)
        source = self._generate_test(req, reasoning, by_key)
        return CombinedAnalysis(
            analyses=analyses, reasoning=reasoning, test_source=source, llm_calls=1
        )

    # ---- part 1 of the response: reasoning + 분석 근거 ----------------------
    def _reason(self, req: TestGenRequest,
                analyses: Dict[str, UnitAnalysis]) -> ReasoningTrace:
        steps: List[str] = []
        scenarios: List[str] = []

        steps.append(
            f"변경된 유닛 {len(req.units)}개를 하나의 비즈니스 흐름으로 묶어 "
            "단일 요청에서 분석·생성한다."
        )
        for ctx in req.contexts:
            beans = ", ".join(ctx.dependency_beans) or "없음"
            steps.append(
                f"[AST MCP] {ctx.target} 시그니처='{ctx.method_signature}', "
                f"의존 Bean=[{beans}], 호출 순서='{ctx.call_flow or '없음'}'"
            )
        for u in req.units:
            a = analyses.get(u.display_name)
            if a:
                steps.append(
                    f"[{u.display_name}] 의도='{a.intent}' → {a.intent_reason} / "
                    f"중요도='{a.importance}' → {a.importance_reason}"
                )
            scenarios.extend(self._scenarios_for(u, a))

        steps.append(
            "각 의도에 대해 정상(성공) 케이스와 예외(실패) 케이스를 최소 1개씩 도출한다."
        )
        steps.append(
            "@SpringBootTest 컨텍스트에서 대상 빈과 의존 Bean을 주입하고, 도출한 "
            "케이스를 JUnit5 assertion으로 검증하는 테스트를 작성한다."
        )

        kinds = sorted({a.intent for a in analyses.values()}) or ["modification"]
        rationale = (
            f"'{_primary_class(req.units)}'의 변경 의도(" + ", ".join(kinds) + ")와 "
            "AST MCP가 전달한 호출 순서에 근거하여, 변경으로 새로 보장되어야 하는 "
            "동작을 성공/실패 케이스로 나누어 검증하도록 테스트를 구성함. "
            "(의도·중요도 분석과 테스트 코드 생성을 1회 호출로 동시 수행)"
        )
        return ReasoningTrace(steps=steps, scenarios=scenarios, rationale=rationale)

    def _scenarios_for(self, u: ChangeUnit, a: UnitAnalysis | None) -> List[str]:
        m = u.method.name if u.method else "changedBehavior"
        kind = a.intent if a else u.intent
        flow = u.context.call_flow if u.context else ""
        flow_note = f" [호출 순서: {flow}]" if flow else ""

        if kind == "feature":
            return [
                f"성공: {m} 이(가) 정상 입력에 대해 기대 결과를 반환한다.{flow_note}",
                f"실패: {m} 이(가) 잘못된 입력에 대해 예외/오류를 처리한다.",
            ]
        if kind == "condition":
            return [
                f"성공: 변경된 조건의 참(true) 분기가 올바르게 동작한다 ({m}).{flow_note}",
                f"성공: 변경된 조건의 거짓(false) 분기가 올바르게 동작한다 ({m}).",
            ]
        if kind == "performance":
            return [
                f"성공: {m} 이(가) 최적화 이후에도 기존과 동일한 결과를 반환한다(동치성).{flow_note}",
                f"성공: {m} 이(가) 반복 호출 시 예외 없이 완료된다.",
            ]
        return [
            f"성공: {m} 의 기존 계약(정상 동작)이 회귀 없이 유지된다.{flow_note}",
            f"실패: {m} 의 경계/예외 입력이 안전하게 처리된다.",
        ]

    # ---- part 2 of the response: test source -------------------------------
    def _generate_test(self, req: TestGenRequest, reasoning: ReasoningTrace,
                       analyses: Dict[str, UnitAnalysis]) -> str:
        pkg = req.project_package
        target = _primary_class(req.units)
        field = _camel_to_field(target)
        test_class = f"{target}GeneratedTest"

        # One @Test method per scenario, named deterministically.
        methods = []
        for i, sc in enumerate(reasoning.scenarios, start=1):
            kind = "success" if sc.startswith("성공") else "failure"
            methods.append(self._render_test_method(i, kind, sc, field, target))

        methods_src = "\n\n".join(methods)
        scenario_doc = "\n".join(f"     *   - {s}" for s in reasoning.scenarios)
        units_doc = ", ".join(u.display_name for u in req.units)
        analysis_doc = "\n".join(
            f"     *   - {a.unit_key}: intent={a.intent} ({a.intent_reason}) / "
            f"importance={a.importance}"
            for a in analyses.values()
        ) or "     *   - (분석 결과 없음)"
        context_doc = "\n".join(
            f"     *   - {c.target}: {c.method_signature}"
            f" | beans=[{', '.join(c.dependency_beans) or '없음'}]"
            f" | flow={c.call_flow or '없음'}"
            for c in req.contexts
        ) or "     *   - (AST MCP 컨텍스트 없음)"

        # Inject the collaborators the AST MCP server reported.
        beans = [b for c in req.contexts for b in c.dependency_beans]
        collaborators = "\n".join(
            f"    @Autowired(required = false)\n"
            f"    private {b} {_camel_to_field(b)};\n"
            for b in dict.fromkeys(beans) if b != target
        )

        return f"""package {pkg};

import org.junit.jupiter.api.DisplayName;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;

import static org.junit.jupiter.api.Assertions.*;

/**
 * AUTO-GENERATED by codetest (mock LLM backend, single-call analyze+generate).
 *
 * Covered change units: {units_doc}
 *
 * Intent / importance analysis:
{analysis_doc}
 *
 * AST MCP context (signature | dependency beans | call order):
{context_doc}
 *
 * Reasoning (chain-of-thought) scenarios:
{scenario_doc}
 *
 * {reasoning.rationale}
 */
@SpringBootTest
class {test_class} {{

    @Autowired(required = false)
    private {target} {field};

{collaborators}
    @Test
    @DisplayName("Spring context loads and target bean is available")
    void contextAndBeanLoads() {{
        // A generated smoke test: verifies the @SpringBootTest context wires up.
        assertNotNull({field}, "{target} bean should be injectable in the test context");
    }}

{methods_src}

    private void assumeBeanPresent() {{
        org.junit.jupiter.api.Assumptions.assumeTrue({field} != null);
    }}
}}
"""

    def _render_test_method(self, idx: int, kind: str, scenario: str,
                            field: str, target: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9]", "", scenario.split(":", 1)[-1].strip().title())[:40]
        method_name = f"scenario{idx}_{kind}_{safe or 'behavior'}"
        if kind == "success":
            body = (
                f"        // {scenario}\n"
                f"        assumeBeanPresent();\n"
                f"        assertDoesNotThrow(() -> {{\n"
                f"            // TODO: invoke the changed behavior on `{field}` and\n"
                f"            //       assert the expected result for this scenario.\n"
                f"        }});"
            )
        else:
            body = (
                f"        // {scenario}\n"
                f"        assumeBeanPresent();\n"
                f"        // TODO: drive the failure path and assert the thrown exception,\n"
                f"        //       e.g. assertThrows(IllegalArgumentException.class, () -> ...);\n"
                f"        assertTrue(true, \"failure-path placeholder for: {scenario}\");"
            )
        display = scenario.replace('"', "'")
        return (
            f"    @Test\n"
            f'    @DisplayName("{display}")\n'
            f"    void {method_name}() {{\n"
            f"{body}\n"
            f"    }}"
        )
