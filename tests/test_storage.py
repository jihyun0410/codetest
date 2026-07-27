"""[계층 4] Storage — 메모리 기본값, 옵트인 SQLite, AST 캐시."""
from __future__ import annotations

from pathlib import Path

from conftest import SERVICE

from codetest.config import Config
from codetest.mcp.ast_flow import ast_tool
from codetest.storage import (AstCache, MemoryFeatureStore, PersistentAstCache,
                              SQLiteFeatureDB, get_store)
from codetest.storage.cache_service import fingerprint


def _classes():
    return ast_tool.analyze_source(SERVICE)


# --------------------------------------------------------------------------- #
# 세션은 메모리에서만
def test_default_store_is_memory_and_writes_no_db(tmp_path: Path):
    cfg = Config.resolve(tmp_path)
    assert cfg.persist is False

    store = get_store(cfg.persist, cfg.db_path)
    assert isinstance(store, MemoryFeatureStore)
    assert store.kind == "memory"

    store.upsert_features("Foo.java", _classes())
    store.record_run("run", "ok")
    assert store.feature_count() > 0
    assert store.all_features()[0]["class_name"] == "OrderService"
    assert store.runs[0]["command"] == "run"
    assert not cfg.db_path.exists()

    cfg.ensure_dirs()
    assert not cfg.db_path.parent.exists()   # no .codetest dir without --persist


def test_memory_store_upsert_is_idempotent():
    store = MemoryFeatureStore()
    store.upsert_features("Foo.java", _classes())
    first = store.feature_count()
    store.upsert_features("Foo.java", _classes())
    assert store.feature_count() == first


# --------------------------------------------------------------------------- #
# --persist 시에만 SQLite
def test_persist_flag_switches_to_sqlite(tmp_path: Path):
    cfg = Config.resolve(tmp_path, persist=True)
    store = get_store(cfg.persist, cfg.db_path)

    assert store.kind == "sqlite"
    store.upsert_features("Foo.java", _classes())
    assert cfg.db_path.exists()
    assert store.feature_count() > 0
    assert store.all_features()[0]["class_name"] == "OrderService"


def test_both_backends_expose_the_same_shape(tmp_path: Path):
    memory = MemoryFeatureStore()
    sqlite = SQLiteFeatureDB(tmp_path / "f.db")
    memory.upsert_features("Foo.java", _classes())
    sqlite.upsert_features("Foo.java", _classes())

    keys = {"file_path", "package", "class_name", "method_name", "signature",
            "modifiers", "start_line", "end_line"}
    assert keys <= set(memory.all_features()[0])
    assert keys <= set(sqlite.all_features()[0])


def test_test_history_is_recorded(tmp_path: Path):
    db = SQLiteFeatureDB(tmp_path / "f.db")
    db.record_test_result("OrderServiceGeneratedTest", True, 4, 0, 82.5)
    with db.connect() as c:
        row = c.execute("SELECT * FROM test_history").fetchone()
    assert row["test_class"] == "OrderServiceGeneratedTest"
    assert row["coverage_pct"] == 82.5


# --------------------------------------------------------------------------- #
# AST 캐시
def test_cache_hits_until_the_file_changes(tmp_path: Path):
    java = tmp_path / "Foo.java"
    java.write_text(SERVICE, encoding="utf-8")
    cache = AstCache()

    assert cache.get(java) is None
    cache.put(java, _classes())
    assert cache.get(java) is not None

    before = fingerprint(java)
    java.write_text(SERVICE + "\n// touched\n", encoding="utf-8")
    assert fingerprint(java) != before
    assert cache.get(java) is None


def test_disabled_cache_never_serves(tmp_path: Path):
    java = tmp_path / "Foo.java"
    java.write_text(SERVICE, encoding="utf-8")
    cache = AstCache(enabled=False)
    cache.put(java, _classes())
    assert cache.get(java) is None


def test_missing_file_has_a_stable_fingerprint(tmp_path: Path):
    assert fingerprint(tmp_path / "nope.java") == "missing"


def test_persistent_cache_survives_a_new_instance(tmp_path: Path):
    java = tmp_path / "Foo.java"
    java.write_text(SERVICE, encoding="utf-8")
    db = SQLiteFeatureDB(tmp_path / "f.db")

    PersistentAstCache(db).put(java, _classes())
    restored = PersistentAstCache(db).get(java)     # cold in-memory, warm on disk

    assert restored is not None
    assert restored[0].name == "OrderService"
