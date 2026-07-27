"""SQLite connection management for the opt-in durable store.

Only reached when the user passes ``--persist``; ``codetest features`` then
inspects what earlier runs discovered.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..models import ClassInfo
from .base_store import FeatureStore, rows_for
from .memory_store import MemoryFeatureStore
from .schema import SCHEMA


class SQLiteFeatureDB:
    kind = "sqlite"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_features(self, file_path: str, classes: Iterable[ClassInfo]) -> int:
        rows = rows_for(file_path, classes)
        with self.connect() as c:
            for row in rows:
                c.execute(
                    """INSERT OR REPLACE INTO features
                       (file_path, package, class_name, method_name, signature,
                        modifiers, start_line, end_line, updated_at)
                       VALUES (?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)""",
                    (row["file_path"], row["package"], row["class_name"],
                     row["method_name"], row["signature"], row["modifiers"],
                     row["start_line"], row["end_line"]),
                )
        return len(rows)

    def record_run(self, command: str, summary: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO runs (command, summary) VALUES (?,?)",
                      (command, summary))

    def record_test_result(self, test_class: str, passed: bool, total: int,
                           failures: int, coverage_pct: Optional[float]) -> None:
        with self.connect() as c:
            c.execute(
                """INSERT INTO test_history
                   (test_class, passed, total, failures, coverage_pct)
                   VALUES (?,?,?,?,?)""",
                (test_class, int(passed), total, failures, coverage_pct),
            )

    def all_features(self) -> List[Dict[str, Any]]:
        with self.connect() as c:
            return [dict(r) for r in
                    c.execute("SELECT * FROM features ORDER BY class_name, method_name")]

    def feature_count(self) -> int:
        with self.connect() as c:
            return c.execute("SELECT COUNT(*) AS n FROM features").fetchone()["n"]


def get_store(persist: bool = False, db_path: Optional[Path] = None) -> FeatureStore:
    """Return the store for this session.

    Defaults to memory: the CLI session's pipeline passes data as variables and
    never touches SQLite unless persistence was explicitly requested.
    """
    if persist:
        if db_path is None:
            raise ValueError("db_path is required when persist=True")
        return SQLiteFeatureDB(db_path)
    return MemoryFeatureStore()
