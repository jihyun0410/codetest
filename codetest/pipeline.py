"""High-level orchestration used by the CLI commands.

Implements the workflow from the spec:

  1. detect changed source (git diff, whitespace/blank-line noise filtered)
  2. analyze methods and pull the AST MCP server's filtered context
  3. one LLM call → 의도/중요도 분석 근거 + @SpringBootTest 코드
  4. (optionally) execute with JaCoCo and build a report

The whole run is stitched together in memory: a single session store is
created here and passed down, and no stage reads state back out of SQLite.
"""
from __future__ import annotations

from typing import List

from . import generator
from .analyzer import ChangeAnalysis, analyze_changes
from .config import IMPORTANCE_ORDER, Config
from .db import FeatureStore, get_store
from .llm import get_client
from .mcp import get_ast_client
from .models import ChangeUnit, Report, ReportItem
from .runner import run_tests


def _bundle(units: List[ChangeUnit]) -> List[List[ChangeUnit]]:
    """Bundle changed units into business flows.

    Per the spec, a change spanning multiple files becomes ONE test — which is
    also what keeps the run at a single API call.
    """
    if not units:
        return []
    return [units]


def build_report(cfg: Config, mode: str, command: str, run_tests_flag: bool) -> Report:
    cfg.ensure_dirs()
    report = Report(command=command, project_dir=str(cfg.project_dir))

    # One store for this invocation; memory-backed unless --persist was given.
    store: FeatureStore = get_store(cfg.persist, cfg.db_path)
    ast_client = get_ast_client(cfg.ast_mcp_transport)

    analysis: ChangeAnalysis = analyze_changes(cfg, mode, store=store, ast_client=ast_client)

    if analysis.skipped_whitespace_only:
        report.notes.append(
            "공백/줄바꿈만 변경되어 제외한 파일: "
            + ", ".join(analysis.skipped_whitespace_only)
        )
    if not analysis.units:
        report.notes.append("변경된 Java 파일을 찾지 못했습니다 (git diff 결과 없음).")
        store.record_run(command, "no changes")
        return report

    llm = get_client(cfg.llm_backend)
    feature_summary = f"session store({store.kind})에 {store.feature_count()}개 feature 탐색됨"

    for bundle in _bundle(analysis.units):
        # Single API call per bundle: analysis + test code together.
        artifact = generator.generate_test(
            cfg, llm, bundle, analysis.package,
            contexts=[u.context for u in bundle if u.context],
            feature_summary=feature_summary, write=True,
        )
        result = run_tests(cfg, artifact) if run_tests_flag else None
        # One report row per change unit, all sharing the bundle's artifact/result.
        for unit in bundle:
            report.items.append(ReportItem(unit=unit, artifact=artifact, result=result))

    # Highest importance first for the Terminal UI.
    report.items.sort(key=lambda it: IMPORTANCE_ORDER.get(it.unit.importance, 0),
                      reverse=True)

    calls = sum({id(i.artifact): i.artifact.llm_calls for i in report.items}.values())
    files = len({i.artifact.file_path for i in report.items})
    report.notes.append(
        f"세션 데이터 전달: {store.kind} (DB 경유 {'O' if cfg.persist else 'X'}) · "
        f"AST MCP: {ast_client.transport} · LLM 호출: {calls}회"
    )
    store.record_run(
        command, f"{len(analysis.units)} change unit(s), {files} test file(s), {calls} LLM call(s)"
    )
    return report
