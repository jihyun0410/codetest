"""High-level orchestration used by the CLI commands.

Implements the workflow from the spec:

  1. detect changed source (git diff)
  2. analyze content / impact / methods (AST + intent) and store to DB
  3. reason (chain-of-thought) and generate @SpringBootTest code
  4. (optionally) execute with JaCoCo and build a report
"""
from __future__ import annotations

from typing import List, Optional

from . import generator
from .analyzer import analyze_changes
from .config import Config
from .db import FeatureDB
from .llm import get_client
from .models import ChangeUnit, Report, ReportItem
from .runner import run_tests


def _bundle(units: List[ChangeUnit]) -> List[List[ChangeUnit]]:
    """Bundle changed units into business flows.

    Per the spec, a change spanning multiple files becomes ONE test. We group
    by top-level feature: everything under the same immediate package/dir is
    treated as one flow. (For the MVP, all changed units form a single bundle
    when >1 file changed; otherwise one bundle per file.)
    """
    if not units:
        return []
    files = {u.file_path for u in units}
    if len(files) > 1:
        return [units]                      # single cross-file business-flow test
    return [units]                          # single file → still one cohesive test


def build_report(cfg: Config, mode: str, command: str, run_tests_flag: bool) -> Report:
    cfg.ensure_dirs()
    report = Report(command=command, project_dir=str(cfg.project_dir))

    units, diffs_by_file, package = analyze_changes(cfg, mode)
    if not units:
        report.notes.append("변경된 Java 파일을 찾지 못했습니다 (git diff 결과 없음).")
        FeatureDB(cfg.db_path).record_run(command, "no changes")
        return report

    llm = get_client(cfg.llm_backend)
    feature_summary = _feature_summary(cfg)

    for bundle in _bundle(units):
        artifact = generator.generate_test(
            cfg, llm, bundle, diffs_by_file, package, feature_summary, write=True
        )
        result = run_tests(cfg, artifact) if run_tests_flag else None
        # One report row per change unit, all sharing the bundle's artifact/result.
        for unit in bundle:
            report.items.append(ReportItem(unit=unit, artifact=artifact, result=result))

    # Highest importance first for the Terminal UI.
    from .config import IMPORTANCE_ORDER
    report.items.sort(key=lambda it: IMPORTANCE_ORDER.get(it.unit.importance, 0),
                      reverse=True)

    FeatureDB(cfg.db_path).record_run(
        command, f"{len(units)} change unit(s), {len({i.artifact.file_path for i in report.items})} test file(s)"
    )
    return report


def _feature_summary(cfg: Config) -> str:
    db = FeatureDB(cfg.db_path)
    return f"project has {db.feature_count()} known feature(s) in DB"
