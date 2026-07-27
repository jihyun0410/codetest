"""Feature stores for the discovered project inventory.

The spec's "Git Diff와 AST로 프로젝트 개요를 탐색하고 저장" step has two very
different lifetimes:

* **현재 CLI 세션** — the pipeline hands features/analysis between its stages.
  This never needs durability, so the default store is
  :class:`MemoryFeatureStore`: plain in-memory variables, no SQLite file, no
  round-trip. One store instance is created per invocation and passed down.
* **세션 간 보존** — only when the user opts in with ``--persist``, the same
  interface is served by :class:`SQLiteFeatureDB` so ``codetest features`` can
  inspect what earlier runs discovered.

Both implement :class:`FeatureStore`, so no pipeline stage knows which one it
is talking to.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol

from .ast_analyzer import ClassInfo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS features (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path    TEXT NOT NULL,
    package      TEXT,
    class_name   TEXT NOT NULL,
    method_name  TEXT,
    signature    TEXT,
    modifiers    TEXT,
    start_line   INTEGER,
    end_line     INTEGER,
    updated_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(file_path, class_name, method_name)
);

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    command    TEXT NOT NULL,
    summary    TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def _rows_for(file_path: str, classes: Iterable[ClassInfo]) -> List[Dict[str, Any]]:
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


class FeatureStore(Protocol):
    """What the pipeline needs from a store, regardless of backing."""

    kind: str

    def upsert_features(self, file_path: str, classes: Iterable[ClassInfo]) -> int: ...
    def record_run(self, command: str, summary: str) -> None: ...
    def all_features(self) -> List[Dict[str, Any]]: ...
    def feature_count(self) -> int: ...


class MemoryFeatureStore:
    """Session-scoped store: everything stays in Python variables.

    Used for every normal ``codetest run/generate/test`` invocation — the
    pipeline stages exchange data directly, so no DB is touched.
    """

    kind = "memory"

    def __init__(self) -> None:
        self._features: Dict[tuple, Dict[str, Any]] = {}
        self.runs: List[Dict[str, Any]] = []

    def upsert_features(self, file_path: str, classes: Iterable[ClassInfo]) -> int:
        count = 0
        for row in _rows_for(file_path, classes):
            row["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._features[(row["file_path"], row["class_name"], row["method_name"])] = row
            count += 1
        return count

    def record_run(self, command: str, summary: str) -> None:
        self.runs.append({
            "command": command, "summary": summary,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        })

    def all_features(self) -> List[Dict[str, Any]]:
        return sorted(
            self._features.values(),
            key=lambda r: (r["class_name"] or "", r["method_name"] or ""),
        )

    def feature_count(self) -> int:
        return len(self._features)


class SQLiteFeatureDB:
    """Opt-in durable store (``--persist``), inspected by ``codetest features``."""

    kind = "sqlite"

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def upsert_features(self, file_path: str, classes: Iterable[ClassInfo]) -> int:
        rows = _rows_for(file_path, classes)
        with self._conn() as c:
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
        with self._conn() as c:
            c.execute("INSERT INTO runs (command, summary) VALUES (?,?)",
                      (command, summary))

    def all_features(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in
                    c.execute("SELECT * FROM features ORDER BY class_name, method_name")]

    def feature_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM features").fetchone()["n"]


# Backwards-compatible alias.
FeatureDB = SQLiteFeatureDB


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
