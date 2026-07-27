"""변경 소스 → ChangeUnit 조립 (MCP Tool 호출 오케스트레이션).

This is agent work, not an MCP tool: it decides *which* tools to call and how
to stitch their answers together. The tools themselves stay ignorant of
ChangeUnits and of each other.

    git_scan_changes  →  ast_parse_file  →  ast_change_context  →  flow_summary
         (diff)            (메서드 매핑)        (가지치기)          (호출 순서)

Everything comes back as plain objects; nothing is written to disk.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from ..config import Config
from ..mcp import McpClient, get_client
from ..mcp.ast_flow import ast_tool
from ..models import (ChangeAnalysis, ChangeUnit, ClassInfo, DiffScan, FileDiff,
                      FlowSummary, MethodContext, MethodInfo)
from ..storage import FeatureStore, MemoryFeatureStore


class ChangeAnalyzer:
    """Drives the Git/AST/Flow MCP tools for one invocation."""

    def __init__(self, cfg: Config,
                 git_client: Optional[McpClient] = None,
                 ast_client: Optional[McpClient] = None,
                 store: Optional[FeatureStore] = None):
        self.cfg = cfg
        self.git = git_client or get_client("git_file", cfg.mcp_transport)
        self.ast = ast_client or get_client("ast_flow", cfg.mcp_transport)
        self.store = store or MemoryFeatureStore()

    # -- step 1: diff ------------------------------------------------------ #
    def scan(self, mode: str) -> DiffScan:
        options = self.cfg.diff_options()
        payload = self.git.call("git_scan_changes", {
            "project_dir": str(self.cfg.project_dir),
            "mode": mode,
            **options.to_dict(),
        })
        return DiffScan.from_dict(payload)

    # -- step 2: AST → change units ---------------------------------------- #
    def _parse(self, diff: FileDiff) -> tuple[List[ClassInfo], List[tuple[str, MethodInfo]]]:
        payload = self.ast.call("ast_parse_file", {
            "project_dir": str(self.cfg.project_dir),
            "file_path": diff.path,
            "changed_lines": diff.changed_new_lines,
        })
        classes = [ClassInfo.from_dict(c) for c in payload.get("classes", [])]
        hits = [(h["class_name"], MethodInfo.from_dict(h["method"]))
                for h in payload.get("changed_methods", [])]
        return classes, hits

    def _units_for(self, diff: FileDiff, classes: List[ClassInfo],
                   hits: List[tuple[str, MethodInfo]]) -> List[ChangeUnit]:
        if not hits:
            # File changed but no method matched (e.g. field/import change) —
            # still record a class-level change unit so it is not lost.
            class_name = classes[0].name if classes else Path(diff.path).stem
            return [ChangeUnit(
                file_path=diff.path, class_name=class_name, method=None,
                changed_lines=diff.changed_new_lines,
                added_lines=diff.added_lines, removed_lines=diff.removed_lines,
                is_new_file=diff.is_new_file, is_new_method=diff.is_new_file,
            )]

        units: List[ChangeUnit] = []
        for class_name, method in hits:
            is_new_method = diff.is_new_file or all(
                ln in diff.changed_new_lines
                for ln in range(method.start_line, method.end_line + 1)
            )
            units.append(ChangeUnit(
                file_path=diff.path, class_name=class_name, method=method,
                changed_lines=[ln for ln in diff.changed_new_lines
                               if method.start_line <= ln <= method.end_line],
                added_lines=diff.added_lines, removed_lines=diff.removed_lines,
                is_new_file=diff.is_new_file, is_new_method=is_new_method,
            ))
        return units

    # -- step 3: pruned context + call flow -------------------------------- #
    def _attach_contexts(self, units: List[ChangeUnit]) -> List[MethodContext]:
        if not units:
            return []
        payload = self.ast.call("ast_change_context", {
            "project_dir": str(self.cfg.project_dir),
            "targets": [{"file_path": u.file_path, "class_name": u.class_name,
                         "method_name": u.method.name if u.method else ""}
                        for u in units],
        })
        contexts = [MethodContext.from_dict(d) for d in payload.get("contexts", [])]

        by_target = {(c.class_name, c.method_name): c for c in contexts}
        attached: List[MethodContext] = []
        for u in units:
            ctx = by_target.get((u.class_name, u.method.name if u.method else ""))
            if ctx is not None:
                u.context = ctx
                attached.append(ctx)
        return attached

    def _build_flow(self, contexts: List[MethodContext]) -> Optional[FlowSummary]:
        if len(contexts) < 2:
            return None            # 단일 대상이면 호출 순서 정리가 의미 없음
        payload = self.ast.call("flow_summary",
                                {"contexts": [c.to_dict() for c in contexts]})
        return FlowSummary.from_dict(payload)

    # -- entry point ------------------------------------------------------- #
    def analyze(self, mode: str) -> ChangeAnalysis:
        scan = self.scan(mode)
        analysis = ChangeAnalysis(
            package=self.cfg.default_test_package,
            skipped_whitespace_only=list(scan.skipped_whitespace_only),
        )

        for diff in scan.diffs:
            analysis.diffs_by_file[diff.path] = diff.diff_text
            classes, hits = self._parse(diff)
            if classes and classes[0].package:
                analysis.package = classes[0].package
            # Feature inventory (session store; DB only with --persist).
            self.store.upsert_features(diff.path, classes)
            analysis.units.extend(self._units_for(diff, classes, hits))

        analysis.contexts = self._attach_contexts(analysis.units)
        analysis.flow = self._build_flow(analysis.contexts)
        return analysis


def analyze_changes(cfg: Config, mode: str,
                    store: Optional[FeatureStore] = None,
                    git_client: Optional[McpClient] = None,
                    ast_client: Optional[McpClient] = None) -> ChangeAnalysis:
    """Convenience wrapper around :class:`ChangeAnalyzer`."""
    return ChangeAnalyzer(cfg, git_client, ast_client, store).analyze(mode)
