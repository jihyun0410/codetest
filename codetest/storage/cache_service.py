"""AST cache + test-result history.

Parsing every changed file on every invocation is wasted work when the file
did not move since the last parse. The cache is keyed by a cheap fingerprint
(size + mtime), so a stale entry is impossible without the file changing.

Deliberately **memory-first**: the default session keeps the cache in process
and disappears with it, matching the "no DB round-trip in a session" rule. A
SQLite-backed instance is only used when the caller opts into persistence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from ..models import ClassInfo


def fingerprint(path: Path) -> str:
    """Cheap change token for a file: size + mtime."""
    try:
        st = path.stat()
    except OSError:
        return "missing"
    return f"{st.st_size}:{int(st.st_mtime_ns)}"


class AstCache:
    """Per-process cache of parsed classes, keyed by path + fingerprint."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._entries: Dict[str, tuple[str, List[ClassInfo]]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, path: Path) -> Optional[List[ClassInfo]]:
        if not self.enabled:
            return None
        entry = self._entries.get(str(path))
        if entry and entry[0] == fingerprint(path):
            self.hits += 1
            return entry[1]
        self.misses += 1
        return None

    def put(self, path: Path, classes: List[ClassInfo]) -> None:
        if self.enabled:
            self._entries[str(path)] = (fingerprint(path), classes)

    def clear(self) -> None:
        self._entries.clear()

    @property
    def stats(self) -> str:
        return f"hit={self.hits}, miss={self.misses}"


class PersistentAstCache(AstCache):
    """AST cache that survives between sessions (used with ``--persist``)."""

    def __init__(self, db, enabled: bool = True):
        super().__init__(enabled)
        self._db = db

    def get(self, path: Path) -> Optional[List[ClassInfo]]:
        cached = super().get(path)
        if cached is not None:
            return cached
        if not self.enabled:
            return None
        with self._db.connect() as c:
            row = c.execute(
                "SELECT fingerprint, payload FROM ast_cache WHERE file_path = ?",
                (str(path),),
            ).fetchone()
        if not row or row["fingerprint"] != fingerprint(path):
            return None
        classes = [ClassInfo.from_dict(d) for d in json.loads(row["payload"])]
        super().put(path, classes)
        self.hits += 1
        self.misses -= 1          # the in-memory miss was served from disk
        return classes

    def put(self, path: Path, classes: List[ClassInfo]) -> None:
        super().put(path, classes)
        if not self.enabled:
            return
        payload = json.dumps([c.to_dict() for c in classes], ensure_ascii=False)
        with self._db.connect() as c:
            c.execute(
                """INSERT OR REPLACE INTO ast_cache
                   (file_path, fingerprint, payload, updated_at)
                   VALUES (?,?,?, CURRENT_TIMESTAMP)""",
                (str(path), fingerprint(path), payload),
            )


# Process-wide default used by the AST MCP server.
_DEFAULT = AstCache()


def default_cache() -> AstCache:
    return _DEFAULT
