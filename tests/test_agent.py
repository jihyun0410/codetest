"""[계층 2] Agent Core — 단일 호출, 프롬프트 계약, 의도 규칙, 변경 분석."""
from __future__ import annotations

from pathlib import Path

from conftest import SERVICE, init_repo, make_unit

from codetest.agent import intent_rules, pipeline, prompt_engine
from codetest.agent.change_analyzer import ChangeAnalyzer, analyze_changes
from codetest.agent.llm import get_client
from codetest.agent.llm.base_client import TestGenRequest
from codetest.config import Config
from codetest.models import (CombinedAnalysis, MethodContext, ReasoningTrace,
                             TestArtifact, TestResult, UnitAnalysis)
from codetest.storage import MemoryFeatureStore


# --------------------------------------------------------------------------- #
# intent_rules — 결정론적 baseline
def test_condition_change_is_detected():
    unit = make_unit(added_lines=["if (qty > 10) {", "return qty * price * 0.9;"],
                     removed_lines=["return qty * price;"])
    kind, reason = intent_rules.classify_intent(unit)
    assert kind in ("condition", "performance")
    assert reason


def test_new_method_is_a_feature():
    kind, _ = intent_rules.classify_intent(make_unit(is_new_method=True))
    assert kind == "feature"


def test_service_change_is_at_least_mid():
    unit = make_unit(added_lines=["if (x) {}"] * 8, intent="condition")
    level, reason = intent_rules.score_importance(unit, "condition")
    assert level in ("Mid", "High")
    assert "점수" in reason


def test_analyze_unit_returns_both_labels_with_reasons():
    analysis = intent_rules.analyze_unit(make_unit())
    assert analysis.unit_key == "OrderService#calculateTotal"
    assert analysis.intent in intent_rules.VALID_INTENTS
    assert analysis.importance in intent_rules.VALID_IMPORTANCE
    assert analysis.intent_reason and analysis.importance_reason


def test_apply_analyses_falls_back_for_missing_units():
    units = [make_unit()]
    applied = intent_rules.apply_analyses(units, [])     # model returned nothing
    assert len(applied) == 1
    assert units[0].intent in intent_rules.VALID_INTENTS
    assert units[0].importance_reason


def test_apply_analyses_clamps_invalid_labels():
    units = [make_unit()]
    intent_rules.apply_analyses(units, [UnitAnalysis(
        unit_key="OrderService#calculateTotal", intent="nonsense",
        intent_reason="r", importance="Critical")])
    assert units[0].intent == "modification"
    assert units[0].importance in intent_rules.VALID_IMPORTANCE


# --------------------------------------------------------------------------- #
# prompt_engine — One-Shot 계약
def test_prompt_carries_pruned_context_not_source():
    ctx = MethodContext(
        file_path="f.java", class_name="OrderService", method_name="calculateTotal",
        method_signature="public double calculateTotal(int qty, double price)",
        dependency_beans=["OrderRepository"], call_flow="OrderRepository.save()")
    req = TestGenRequest(units=[make_unit()], project_package="com.example.demo",
                         contexts=[ctx])

    prompt = prompt_engine.build_prompt(req)
    assert "OrderRepository" in prompt and "calculateTotal" in prompt
    assert "test_code" in prompt                 # 응답 스키마가 요청에 포함됨
    assert "import org.springframework" not in prompt   # 전체 소스는 미포함


def test_parse_response_reads_both_halves():
    combined = prompt_engine.parse_response("""```json
    {"analyses": [{"unit_key": "OrderService#calculateTotal", "intent": "condition",
                   "intent_reason": "분기 추가", "importance": "High",
                   "importance_reason": "서비스 계층"}],
     "reasoning": {"steps": ["s"], "scenarios": ["성공: ..."], "rationale": "r"},
     "test_code": "package x;\\n@SpringBootTest class T {}"}
    ```""", [make_unit()])

    assert combined.llm_calls == 1
    assert combined.analyses[0].intent == "condition"
    assert combined.reasoning.scenarios == ["성공: ..."]
    assert "@SpringBootTest" in combined.test_source


def test_malformed_response_falls_back_to_baseline():
    combined = prompt_engine.parse_response("모델이 JSON을 안 줬음", [make_unit()])
    assert combined.analyses                     # baseline이 채워짐
    assert combined.test_source == ""


# --------------------------------------------------------------------------- #
# 단일 API 호출
def test_single_call_returns_analysis_and_test_code():
    req = TestGenRequest(units=[make_unit()], project_package="com.example.demo")
    combined = get_client("mock").analyze_and_generate(req)

    assert combined.llm_calls == 1
    assert combined.analyses and combined.analyses[0].intent_reason
    assert combined.analyses[0].importance in intent_rules.VALID_IMPORTANCE
    assert "@SpringBootTest" in combined.test_source
    assert "class OrderServiceGeneratedTest" in combined.test_source
    assert combined.reasoning.scenarios


def test_generate_artifact_makes_exactly_one_llm_call(tmp_path: Path):
    from codetest.models import ChangeAnalysis

    class CountingLLM:
        name = "counting"

        def __init__(self):
            self.calls = 0
            self._inner = get_client("mock")

        def analyze_and_generate(self, req) -> CombinedAnalysis:
            self.calls += 1
            return self._inner.analyze_and_generate(req)

    cfg = Config.resolve(tmp_path)
    llm = CountingLLM()
    units = [make_unit(),
             make_unit(class_name="OrderController",
                       file_path="src/main/java/com/example/demo/controller/OrderController.java")]
    analysis = ChangeAnalysis(units=units, package="com.example.demo")

    artifact = pipeline.generate_artifact(cfg, llm, units, analysis, write=False)

    assert llm.calls == 1
    assert artifact.llm_calls == 1
    assert len(artifact.analyses) == len(units)
    assert all(u.intent_reason for u in units)


def test_generated_test_injects_dependency_beans():
    ctx = MethodContext(
        file_path="f.java", class_name="OrderService", method_name="calculateTotal",
        method_signature="public double calculateTotal(int q, double p)",
        dependency_beans=["DiscountPolicy", "OrderRepository"],
        call_flow="DiscountPolicy.apply() → OrderRepository.save()")
    unit = make_unit(context=ctx)
    req = TestGenRequest(units=[unit], project_package="com.example.demo", contexts=[ctx])

    source = get_client("mock").analyze_and_generate(req).test_source
    assert "private DiscountPolicy discountPolicy;" in source
    assert "private OrderRepository orderRepository;" in source


# --------------------------------------------------------------------------- #
# change_analyzer — MCP 오케스트레이션
def test_change_analyzer_builds_units_and_contexts(tmp_path: Path):
    repo, java = init_repo(tmp_path, SERVICE.replace("qty > 10", "qty > 1000"))
    java.write_text(SERVICE, encoding="utf-8")

    cfg = Config.resolve(repo)
    store = MemoryFeatureStore()
    analysis = ChangeAnalyzer(cfg, store=store).analyze("working")

    assert analysis.units
    assert analysis.package == "com.example.demo.service"
    assert analysis.units[0].context is not None
    assert analysis.units[0].context.dependency_beans
    assert store.feature_count() > 0
    assert not cfg.db_path.exists()      # 세션은 메모리로만 흐른다


def test_change_analyzer_reports_whitespace_skips(tmp_path: Path):
    repo, java = init_repo(tmp_path)
    java.write_text("package com.example;\n\npublic class Foo {\n\n"
                    "      public int a(){return 1;}\n\n}\n", encoding="utf-8")

    analysis = analyze_changes(Config.resolve(repo), "working")
    assert analysis.units == []
    assert analysis.skipped_whitespace_only


def test_flow_is_built_only_for_multi_file_changes(tmp_path: Path):
    from conftest import CONTROLLER, git

    repo, java = init_repo(tmp_path, SERVICE.replace("qty > 10", "qty > 1000"))
    controller = repo / "src" / "main" / "java" / "com" / "example" / "OrderController.java"
    controller.write_text(CONTROLLER.replace("qty, price", "qty, price"), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "add controller")

    java.write_text(SERVICE, encoding="utf-8")
    controller.write_text(CONTROLLER.replace("return orderService",
                                             "// changed\n        return orderService"),
                          encoding="utf-8")

    analysis = analyze_changes(Config.resolve(repo), "working")
    assert len(analysis.contexts) >= 2
    assert analysis.flow is not None
    assert analysis.flow.entry_point == "OrderController"


# --------------------------------------------------------------------------- #
# validity 판정은 에이전트의 몫
def test_validity_is_inconclusive_when_simulated():
    artifact = TestArtifact(class_name="T", package="p", file_path="T.java", source="",
                            reasoning=ReasoningTrace(scenarios=["성공: a"]))
    result = TestResult(passed=True, total=2, executor="simulated")
    pipeline.judge_validity(result, artifact)
    assert result.validity == "inconclusive"
    assert "시뮬레이션" in result.validity_reason


def test_validity_valid_mentions_branch_coverage():
    artifact = TestArtifact(class_name="T", package="p", file_path="T.java", source="",
                            reasoning=ReasoningTrace(scenarios=["성공: a"]))
    result = TestResult(passed=True, total=3, executor="gradle",
                        coverage_pct=80.0, branch_coverage_pct=75.0)
    pipeline.judge_validity(result, artifact)
    assert result.validity == "valid"
    assert "분기=75.0%" in result.validity_reason


def test_validity_invalid_on_failures():
    artifact = TestArtifact(class_name="T", package="p", file_path="T.java", source="",
                            reasoning=ReasoningTrace(scenarios=["성공: a"]))
    result = TestResult(passed=False, total=3, failures=2, executor="gradle")
    pipeline.judge_validity(result, artifact)
    assert result.validity == "invalid"
