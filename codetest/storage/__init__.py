"""[계층 4] Storage — 세션 메모리 저장소, 옵트인 SQLite, AST 캐시.

The default path never touches a file: :class:`MemoryFeatureStore` holds the
session's data as plain variables. SQLite is opt-in (``--persist``) and serves
the same :class:`FeatureStore` interface, so no other layer changes.
"""
from __future__ import annotations

from .base_store import FeatureStore, rows_for
from .cache_service import AstCache, PersistentAstCache, default_cache
from .db_manager import SQLiteFeatureDB, get_store
from .memory_store import MemoryFeatureStore
from .schema import SCHEMA

__all__ = [
    "FeatureStore",
    "MemoryFeatureStore",
    "SQLiteFeatureDB",
    "get_store",
    "rows_for",
    "AstCache",
    "PersistentAstCache",
    "default_cache",
    "SCHEMA",
]
