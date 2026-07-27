"""4단계 워크플로우 오케스트레이터 (메모리 기반 데이터 스트림).

  1. 변경 소스 조회        — Git & File MCP (공백·줄바꿈 노이즈 제외)
  2. 변경 메서드 분석      — AST & Flow MCP (시그니처·의존 Bean·호출 순서만 전달)
  3. 단 1회 LLM 호출       — 의도/중요도 분석 근거 + @SpringBootTest 코드 동시 수신
  4. 실행 및 정합성 판단   — Test Execution MCP + 에이전트의 validity 판정

The whole run is stitched together in memory: one session store is created
here and passed down, and no stage reads state back out of SQLite. Judging a
result (valid / invalid / inconclusive) stays here rather than in the test
runner — it is a decision, not an execution fact.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from ..config import IMPORTANCE_ORDER, Config
from ..mcp import McpClient, get_client
from ..mcp.git_file import file_tool
from ..models import (ChangeAnalysis, ChangeUnit, ReasoningTrace, Report,
                      ReportItem, TestArtifact, TestResult)
from ..storage import FeatureStore, get_store
from . import intent_rules
from .change_analyzer import ChangeAnalyzer
from .llm import LLMClient, TestGenRequest, get_client as get_llm


def _bundle(units: List[ChangeUnit]) -> List[List[ChangeUnit]]:
    """Bundle changed units into business flows.

    Per the spec, a change spanning multiple files becomes ONE test — which is
    also what keeps the run at a single API call.
    """
    return [units] if units else []


def _primary_target(units: List[ChangeUnit]) -> str:
    for u in units:
        if "service" in u.file_path.lower():
            return u.class_name
    return units[0].class_name


# --------------------------------------------------------------------------- #
# stage 3: single LLM call → artifact
# --------------------------------------------------------------------------- #


def generate_artifact(cfg: Config, llm: LLMClient, units: List[ChangeUnit],
                      analysis: ChangeAnalysis, feature_summary: str = "",
                      write: bool = True) -> TestArtifact:
    """Analyze + generate one test class for the bundle with a single API call."""
    contexts = [u.context for u in units if u.context]
    req = TestGenRequest(
        units=units, project_package=analysis.package, contexts=contexts,
        flow=analysis.flow, feature_summary=feature_summary,
    )

    # ── the one and only LLM round trip ──────────────────────────────────────
    combined = llm.analyze_and_generate(req)

    # The analysis half of that response labels the units for the report.
    analyses = intent_rules.apply_analyses(units, combined.analyses)

    test_class = f"{_primary_target(units)}GeneratedTest"
    if write:
        path = file_tool.write_test_file(cfg.test_source_dir, analysis.package,
                                         test_class, combined.test_source)
    else:
        path = file_tool.test_file_path(cfg.test_source_dir, analysis.package, test_class)

    return TestArtifact(
        class_name=test_class, package=analysis.package, file_path=str(path),
        source=combined.test_source, reasoning=combined.reasoning,
        covered_units=units, analyses=analyses, contexts=contexts,
        flow=analysis.flow, llm_calls=combined.llm_calls,
    )


# --------------------------------------------------------------------------- #
# stage 4: execution + validity judgment
# --------------------------------------------------------------------------- #


def run_tests(cfg: Config, artifact: TestArtifact,
              exec_client: Optional[McpClient] = None) -> TestResult:
    """Execute the artifact through the Test Execution MCP server."""
    client = exec_client or get_client("test_exec", cfg.mcp_transport)
    fqcn = f"{artifact.package}.{artifact.class_name}"

    run = client.call("test_run", {
        "project_dir": str(cfg.project_dir), "fqcn": fqcn, "source": artifact.source,
    })

    if run.get("executor") == "gradle":
        report = client.call("coverage_report", {"project_dir": str(cfg.project_dir)})
        total = int(report.get("total", 0))
        failures = int(report.get("failures", 0))
        errors = int(report.get("errors", 0))
        skipped = int(report.get("skipped", 0))
        passed = run.get("return_code") == 0 and failures == 0 and errors == 0
        log = run.get("log", "")
        detail = report.get("failures_detail") or []
        if detail:
            log = log + "\n\n실패 상세:\n" + "\n".join(f"  - {d}" for d in detail)
        coverage = report.get("coverage_pct")
        branch = report.get("branch_coverage_pct")
    else:
        total = int(run.get("test_count", 0))
        failures = errors = skipped = 0
        passed = True
        log = run.get("log", "")
        coverage = branch = None

    result = TestResult(
        passed=passed, total=total, failures=failures, errors=errors, skipped=skipped,
        duration_s=float(run.get("duration_s", 0.0)), coverage_pct=coverage,
        branch_coverage_pct=branch, executor=run.get("executor", "simulated"), log=log,
    )
    judge_validity(result, artifact)
    return result


def judge_validity(result: TestResult, artifact: TestArtifact) -> None:
    """Populate the validity judgment surfaced under <Test Result 보기>."""
    n_scen = len(artifact.reasoning.scenarios)
    if result.executor == "simulated":
        result.validity = "inconclusive"
        result.validity_reason = (
            f"생성된 테스트는 {result.total}개의 @Test와 {n_scen}개의 시나리오를 "
            "포함하지만, 로컬에 Java/Gradle이 없어 실제 실행으로 검증되지 않음(시뮬레이션)."
        )
    elif result.passed:
        cov = result.coverage_pct if result.coverage_pct is not None else "N/A"
        branch = (f", 분기={result.branch_coverage_pct}%"
                  if result.branch_coverage_pct is not None else "")
        result.validity = "valid"
        result.validity_reason = (
            f"{result.total}개 테스트가 모두 통과했고, 변경 의도별 성공/실패 시나리오"
            f"({n_scen}개)를 커버함. 커버리지={cov}%{branch}."
        )
    else:
        result.validity = "invalid"
        result.validity_reason = (
            f"실패 {result.failures}건 / 오류 {result.errors}건 발생 — 변경 코드가 "
            "기대 동작을 만족하지 못하거나 테스트가 회귀를 감지함. 로그 확인 필요."
        )


# --------------------------------------------------------------------------- #
# entry points
# --------------------------------------------------------------------------- #


def build_report(cfg: Config, mode: str, command: str, run_tests_flag: bool) -> Report:
    """`codetest run` / `codetest generate`."""
    cfg.ensure_dirs()
    report = Report(command=command, project_dir=str(cfg.project_dir))

    # One store for this invocation; memory-backed unless --persist was given.
    store: FeatureStore = get_store(cfg.persist, cfg.db_path)
    analyzer = ChangeAnalyzer(cfg, store=store)
    analysis = analyzer.analyze(mode)

    if analysis.skipped_whitespace_only:
        report.notes.append(
            "공백/줄바꿈만 변경되어 제외한 파일: "
            + ", ".join(analysis.skipped_whitespace_only)
        )
    if not analysis.units:
        report.notes.append("변경된 Java 파일을 찾지 못했습니다 (git diff 결과 없음).")
        store.record_run(command, "no changes")
        return report

    llm = get_llm(cfg.llm_backend)
    feature_summary = f"session store({store.kind})에 {store.feature_count()}개 feature 탐색됨"
    exec_client = get_client("test_exec", cfg.mcp_transport) if run_tests_flag else None

    for bundle in _bundle(analysis.units):
        artifact = generate_artifact(cfg, llm, bundle, analysis, feature_summary, write=True)
        result = run_tests(cfg, artifact, exec_client) if run_tests_flag else None
        # One report row per change unit, all sharing the bundle's artifact/result.
        for unit in bundle:
            report.items.append(ReportItem(unit=unit, artifact=artifact, result=result))

    # Highest importance first for the Terminal UI.
    report.items.sort(key=lambda it: IMPORTANCE_ORDER.get(it.unit.importance, 0),
                      reverse=True)

    calls = sum({id(i.artifact): i.artifact.llm_calls for i in report.items}.values())
    files = len({i.artifact.file_path for i in report.items})
    if analysis.flow and analysis.flow.steps:
        report.notes.append("비즈니스 흐름: " + analysis.flow.summary)
    report.notes.append(
        f"세션 데이터 전달: {store.kind} (DB 경유 {'O' if cfg.persist else 'X'}) · "
        f"MCP: {analyzer.ast.transport} · LLM 호출: {calls}회"
    )
    store.record_run(
        command,
        f"{len(analysis.units)} change unit(s), {files} test file(s), {calls} LLM call(s)",
    )
    return report


def build_report_from_txt(cfg: Config, command: str) -> Report:
    """`codetest test` — run the user-provided test from src/test/test.txt."""
    from ..models import ChangeUnit, MethodInfo

    cfg.ensure_dirs()
    report = Report(command=command, project_dir=str(cfg.project_dir))

    if not cfg.test_txt_path.exists():
        report.notes.append(f"테스트 파일이 없습니다: {cfg.test_txt_path}")
        return report

    source = cfg.test_txt_path.read_text(encoding="utf-8", errors="replace")
    package, class_name = file_tool.parse_java_identity(
        source, cfg.default_test_package, "ProvidedTest")
    source = file_tool.ensure_springboot_annotation(source)
    path = file_tool.write_test_file(cfg.test_source_dir, package, class_name, source)

    reasoning = ReasoningTrace(
        steps=["사용자가 제공한 src/test/test.txt의 테스트 코드를 로드",
               "@SpringBootTest 보장 후 테스트 소스 트리에 배치",
               "JaCoCo와 함께 테스트 실행"],
        scenarios=[f"{class_name} 내부에 정의된 사용자 작성 시나리오"],
        rationale="사용자가 직접 작성/검토한 테스트 코드를 그대로 실행하여 검증함.",
    )
    unit = ChangeUnit(
        file_path="src/test/test.txt", class_name=class_name,
        method=MethodInfo(name="(provided)", signature="(provided)",
                          start_line=0, end_line=0),
        intent="modification", intent_reason="사용자 제공 테스트(test.txt)",
        importance="Mid",
        importance_reason="사용자가 직접 지정한 테스트이므로 기본 중요도(Mid)로 표기함.",
    )
    artifact = TestArtifact(
        class_name=class_name, package=package, file_path=str(path), source=source,
        reasoning=reasoning, covered_units=[unit],
        llm_calls=0,   # 사용자가 제공한 코드 — LLM 호출 없음
    )
    result = run_tests(cfg, artifact)
    report.items.append(ReportItem(unit=unit, artifact=artifact, result=result))

    get_store(cfg.persist, cfg.db_path).record_run(
        command, f"ran provided test {class_name}")
    return report
