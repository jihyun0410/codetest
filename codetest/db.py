"""SQLite persistence for discovered project features.

Per the spec: "Git Diff와 AST로 프로젝트 개요를 탐색하고 DB에 저장" — the agent
records the classes/methods it has seen so later reasoning can reference the
project's feature inventory.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, List

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


class FeatureDB:
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
        count = 0
        with self._conn() as c:
            for cls in classes:
                if not cls.methods:
                    c.execute(
                        """INSERT OR REPLACE INTO features
                           (file_path, package, class_name, method_name, signature,
                            modifiers, start_line, end_line, updated_at)
                           VALUES (?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)""",
                        (file_path, cls.package, cls.name, None, None, None, None, None),
                    )
                    count += 1
                for m in cls.methods:
                    c.execute(
                        """INSERT OR REPLACE INTO features
                           (file_path, package, class_name, method_name, signature,
                            modifiers, start_line, end_line, updated_at)
                           VALUES (?,?,?,?,?,?,?,?, CURRENT_TIMESTAMP)""",
                        (file_path, cls.package, cls.name, m.name, m.signature,
                         ",".join(m.modifiers), m.start_line, m.end_line),
                    )
                    count += 1
        return count

    def record_run(self, command: str, summary: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO runs (command, summary) VALUES (?,?)",
                      (command, summary))

    def all_features(self) -> List[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM features ORDER BY class_name, method_name"))

    def feature_count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM features").fetchone()["n"]
