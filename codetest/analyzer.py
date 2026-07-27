"""Stage 1-2 of the workflow: detect changes and build ChangeUnits.

Combines Git Diff + AST + intent classification and persists discovered
features to the DB.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Tuple

from . import ast_analyzer, intent
from .config import Config
from .db import FeatureDB
from .git_analyzer import FileDiff, get_file_diffs
from .models import ChangeUnit


def analyze_changes(cfg: Config, mode: str) -> Tuple[List[ChangeUnit], Dict[str, str], str]:
    """Return (change_units, diff_text_by_file, project_package).

    Also upserts every analyzed file's classes/methods into the feature DB.
    """
    diffs: List[FileDiff] = get_file_diffs(cfg.project_dir, mode)
    db = FeatureDB(cfg.db_path)

    units: List[ChangeUnit] = []
    diffs_by_file: Dict[str, str] = {}
    package = cfg.default_test_package

    for d in diffs:
        diffs_by_file[d.path] = d.diff_text
        abs_path = cfg.project_dir / d.path
        classes = ast_analyzer.analyze_file(abs_path) if abs_path.exists() else []
        if classes and classes[0].package:
            package = classes[0].package

        # Persist the feature inventory for this file.
        db.upsert_features(d.path, classes)

        hits = ast_analyzer.find_method_for_lines(classes, d.changed_new_lines)
        if not hits:
            # File changed but no method matched (e.g. field/import change) —
            # still record a class-level change unit so it is not lost.
            class_name = classes[0].name if classes else Path(d.path).stem
            unit = ChangeUnit(
                file_path=d.path, class_name=class_name, method=None,
                changed_lines=d.changed_new_lines,
                added_lines=d.added_lines, removed_lines=d.removed_lines,
            )
            _label(unit, d, is_new_method=d.is_new_file)
            units.append(unit)
            continue

        for class_name, method in hits:
            is_new_method = all(
                ln in d.changed_new_lines
                for ln in range(method.start_line, method.end_line + 1)
            ) or d.is_new_file
            unit = ChangeUnit(
                file_path=d.path, class_name=class_name, method=method,
                changed_lines=[ln for ln in d.changed_new_lines
                               if method.start_line <= ln <= method.end_line],
                added_lines=d.added_lines, removed_lines=d.removed_lines,
            )
            _label(unit, d, is_new_method=is_new_method)
            units.append(unit)

    return units, diffs_by_file, package


def _label(unit: ChangeUnit, diff: FileDiff, is_new_method: bool) -> None:
    kind, reason = intent.classify_intent(diff, is_new_method)
    unit.intent = kind
    unit.intent_reason = reason
    unit.importance = intent.score_importance(unit, diff)
