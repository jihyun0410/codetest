"""Storage contract shared by the memory and SQLite backends.

No pipeline stage may know which backend it holds — that is what keeps the
default session in memory while ``--persist`` transparently mirrors to SQLite.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List, Protocol

from ..models import ClassInfo


class FeatureStore(Protocol):
    """What the pipeline needs from a store, regardless of backing."""

    kind: str

    def upsert_features(self, file_path: str, classes: Iterable[ClassInfo]) -> int: ...
    def record_run(self, command: str, summary: str) -> None: ...
    def all_features(self) -> List[Dict[str, Any]]: ...
    def feature_count(self) -> int: ...


def rows_for(file_path: str, classes: Iterable[ClassInfo]) -> List[Dict[str, Any]]:
    """Flatten parsed classes into one row per class/method."""
    rows: List[Dict[str, Any]] = []
    for cls in classes:
        if not cls.methods:
            rows.append({
                "file_path": file_path, "package": cls.package, "class_name": cls.name,
                "method_name": None, "signature": None, "modifiers": None,
                "start_line": None, "end_line": None,
            })
            continue
        for m in cls.methods:
            rows.append({
                "file_path": file_path, "package": cls.package, "class_name": cls.name,
                "method_name": m.name, "signature": m.signature,
                "modifiers": ",".join(m.modifiers),
                "start_line": m.start_line, "end_line": m.end_line,
            })
    return rows


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
