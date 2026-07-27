"""Session-scoped store: everything stays in Python variables.

This is the default for every ``codetest run/generate/test`` invocation — the
pipeline stages exchange data directly, so no DB is touched and no file is
created.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

from ..models import ClassInfo
from .base_store import now, rows_for


class MemoryFeatureStore:
    kind = "memory"

    def __init__(self) -> None:
        self._features: Dict[tuple, Dict[str, Any]] = {}
        self.runs: List[Dict[str, Any]] = []

    def upsert_features(self, file_path: str, classes: Iterable[ClassInfo]) -> int:
        count = 0
        for row in rows_for(file_path, classes):
            row["updated_at"] = now()
            self._features[(row["file_path"], row["class_name"], row["method_name"])] = row
            count += 1
        return count

    def record_run(self, command: str, summary: str) -> None:
        self.runs.append({"command": command, "summary": summary, "created_at": now()})

    def all_features(self) -> List[Dict[str, Any]]:
        return sorted(
            self._features.values(),
            key=lambda r: (r["class_name"] or "", r["method_name"] or ""),
        )

    def feature_count(self) -> int:
        return len(self._features)
