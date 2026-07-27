"""SQLite table DDL.

Kept apart from :mod:`codetest.models`: those dataclasses are the runtime
contract between layers, this is only how the optional durable store lays
its tables out.
"""
from __future__ import annotations

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS ast_cache (
    file_path   TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    payload     TEXT NOT NULL,
    updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS test_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    test_class   TEXT NOT NULL,
    passed       INTEGER NOT NULL,
    total        INTEGER,
    failures     INTEGER,
    coverage_pct REAL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
"""
