"""Stage 1-2 of the workflow: detect changes and build ChangeUnits.

Combines Git Diff (whitespace-noise filtered) + the AST MCP server's filtered
context, and hands the result back as plain in-memory objects. Nothing is
written to disk here: the feature store passed in is the session store, which
is an in-memory one unless the user asked for persistence.

Intent/importance are **not** decided here anymore — they come back from the
single LLM call in :mod:`codetest.generator`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from . import ast_analyzer
from .config import Config
from .db import FeatureStore, MemoryFeatureStore
from .git_analyzer import DiffScan, scan_changes
from .mcp import AstMcpClient, get_ast_client
from .models import ChangeUnit, MethodContext


@dataclass
class ChangeAnalysis:
    """Everything stage 3 needs, passed as variables (no DB round-trip)."""

    units: List[ChangeUnit] = field(default_factory=list)
    contexts: List[MethodContext] = field(default_factory=list)
    package: str = ""
    diffs_by_file: Dict[str, str] = field(default_factory=dict)
    skipped_whitespace_only: List[str] = field(default_factory=list)


def analyze_changes(
    cfg: Config,
    mode: str,
    store: Optional[FeatureStore] = None,
    ast_client: Optional[AstMcpClient] = None,
) -> ChangeAnalysis:
    """Detect changes, map them to methods, and fetch the MCP context."""
    store = store or MemoryFeatureStore()
    ast_client = ast_client or get_ast_client(cfg.ast_mcp_transport)

    scan: DiffScan = scan_changes(cfg.project_dir, mode, cfg.diff_options())
    analysis = ChangeAnalysis(
        package=cfg.default_test_package,
        skipped_whitespace_only=list(scan.skipped_whitespace_only),
    )

    for d in scan.diffs:
        analysis.diffs_by_file[d.path] = d.diff_text
        abs_path = cfg.project_dir / d.path
        classes = ast_analyzer.analyze_file(abs_path) if abs_path.exists() else []
        if classes and classes[0].package:
            analysis.package = classes[0].package

        # Feature inventory for this file (session store; DB only with --persist).
        store.upsert_features(d.path, classes)

        hits = ast_analyzer.find_method_for_lines(classes, d.changed_new_lines)
        if not hits:
            # File changed but no method matched (e.g. field/import change) —
            # still record a class-level change unit so it is not lost.
            class_name = classes[0].name if classes else Path(d.path).stem
            analysis.units.append(ChangeUnit(
                file_path=d.path, class_name=class_name, method=None,
                changed_lines=d.changed_new_lines,
                added_lines=d.added_lines, removed_lines=d.removed_lines,
                is_new_file=d.is_new_file, is_new_method=d.is_new_file,
            ))
            continue

        for class_name, method in hits:
            is_new_method = d.is_new_file or all(
                ln in d.changed_new_lines
                for ln in range(method.start_line, method.end_line + 1)
            )
            analysis.units.append(ChangeUnit(
                file_path=d.path, class_name=class_name, method=method,
                changed_lines=[ln for ln in d.changed_new_lines
                               if method.start_line <= ln <= method.end_line],
                added_lines=d.added_lines, removed_lines=d.removed_lines,
                is_new_file=d.is_new_file, is_new_method=is_new_method,
            ))

    # AST MCP server: 시그니처 / 의존 Bean 목록 / 호출 순서 요약만 받아온다.
    analysis.contexts = ast_client.attach_contexts(cfg.project_dir, analysis.units)
    return analysis
