"""Unit tests for the agent's core stages (no Java toolchain required)."""
from __future__ import annotations

import subprocess
from pathlib import Path

from codetest import ast_analyzer, ast_context, intent
from codetest.config import Config
from codetest.db import MemoryFeatureStore, get_store
from codetest.git_analyzer import DiffOptions, get_file_diffs, scan_changes
from codetest.llm import get_client
from codetest.llm.base import TestGenRequest
from codetest.mcp import call_tool, get_ast_client
from codetest.models import ChangeUnit, MethodContext, MethodInfo


SERVICE = """package com.example.demo.service;
import org.springframework.stereotype.Service;
@Service
public class OrderService {

    private final OrderRepository orderRepository;
    private final DiscountPolicy discountPolicy;

    public OrderService(OrderRepository orderRepository, DiscountPolicy discountPolicy) {
        this.orderRepository = orderRepository;
        this.discountPolicy = discountPolicy;
    }

    public double calculateTotal(int qty, double price) {
        double subtotal = qty * price;
        if (qty > 10) {
            subtotal = discountPolicy.apply(subtotal);
        }
        orderRepository.save(subtotal);
        return subtotal;
    }
}
"""


def _unit(**overrides) -> ChangeUnit:
    base = dict(
        file_path="src/main/java/com/example/demo/service/OrderService.java",
        class_name="OrderService",
        method=MethodInfo("calculateTotal", "calculateTotal(int qty, double price)",
                          4, 9, ["public"], "double"),
        changed_lines=[5],
        added_lines=["if (qty > 10) {"],
        removed_lines=[],
    )
    base.update(overrides)
    return ChangeUnit(**base)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _repo_with_baseline(tmp_path: Path, content: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    src = repo / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    java = src / "Foo.java"
    java.write_text(content, encoding="utf-8")
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    return repo, java


# --------------------------------------------------------------------------- #
# AST
def test_ast_finds_methods():
    classes = ast_analyzer.analyze_source(SERVICE)
    assert classes
    names = {m.name for c in classes for m in c.methods}
    assert "calculateTotal" in names


# --------------------------------------------------------------------------- #
# intent (baseline analysis used inside the single LLM call)
def test_intent_condition_detected():
    unit = _unit(added_lines=["if (qty > 10) {", "return qty * price * 0.9;"],
                 removed_lines=["return qty * price;"])
    kind, reason = intent.classify_intent(unit)
    assert kind in ("condition", "performance")
    assert reason


def test_importance_service_is_at_least_mid():
    unit = _unit(added_lines=["if (x) {}"] * 8, intent="condition")
    level, reason = intent.score_importance(unit, "condition")
    assert level in ("Mid", "High")
    assert "점수" in reason


def test_analyze_unit_returns_both_intent_and_importance():
    analysis = intent.analyze_unit(_unit())
    assert analysis.unit_key == "OrderService#calculateTotal"
    assert analysis.intent in intent.VALID_INTENTS
    assert analysis.importance in intent.VALID_IMPORTANCE
    assert analysis.intent_reason and analysis.importance_reason


def test_apply_analyses_falls_back_for_missing_units():
    units = [_unit()]
    applied = intent.apply_analyses(units, [])     # model returned nothing
    assert len(applied) == 1
    assert units[0].intent in intent.VALID_INTENTS
    assert units[0].importance_reason


# --------------------------------------------------------------------------- #
# 1. single API call: analysis + test code in one response
def test_single_call_returns_analysis_and_test_code():
    unit = _unit()
    llm = get_client("mock")
    req = TestGenRequest(units=[unit], project_package="com.example.demo")

    combined = llm.analyze_and_generate(req)

    assert combined.llm_calls == 1
    assert combined.analyses and combined.analyses[0].intent_reason
    assert combined.analyses[0].importance in intent.VALID_IMPORTANCE
    assert "@SpringBootTest" in combined.test_source
    assert "class OrderServiceGeneratedTest" in combined.test_source
    assert "@Test" in combined.test_source
    assert combined.reasoning.scenarios


def test_generator_makes_exactly_one_llm_call(tmp_path: Path):
    from codetest import generator

    class CountingLLM:
        name = "counting"

        def __init__(self):
            self.calls = 0
            self._inner = get_client("mock")

        def analyze_and_generate(self, req):
            self.calls += 1
            return self._inner.analyze_and_generate(req)

    cfg = Config.resolve(tmp_path)
    llm = CountingLLM()
    units = [_unit(), _unit(class_name="OrderController",
                            file_path="src/main/java/com/example/demo/controller/OrderController.java")]

    artifact = generator.generate_test(cfg, llm, units, "com.example.demo", write=False)

    assert llm.calls == 1
    assert artifact.llm_calls == 1
    assert len(artifact.analyses) == len(units)
    assert all(u.intent_reason for u in units)


# --------------------------------------------------------------------------- #
# 2. AST MCP server hands over only the filtered context
def test_ast_context_filters_signature_beans_and_call_flow():
    classes = ast_analyzer.analyze_source(SERVICE)
    ctx = ast_context.build_method_context(
        SERVICE, classes, "src/.../OrderService.java", "OrderService", "calculateTotal"
    )
    assert "calculateTotal" in ctx.method_signature
    assert set(ctx.dependency_beans) == {"OrderRepository", "DiscountPolicy"}
    assert "DiscountPolicy.apply()" in ctx.call_flow
    assert "OrderRepository.save()" in ctx.call_flow
    # the raw source must never be part of the payload
    assert "import org.springframework" not in str(ctx.to_dict())


def test_mcp_tool_returns_only_the_three_fields(tmp_path: Path):
    java = tmp_path / "src" / "main" / "java" / "OrderService.java"
    java.parent.mkdir(parents=True)
    java.write_text(SERVICE, encoding="utf-8")

    payload = call_tool("ast_method_context", {
        "project_dir": str(tmp_path),
        "file_path": "src/main/java/OrderService.java",
        "class_name": "OrderService",
        "method_name": "calculateTotal",
    })
    assert set(payload) == {"file_path", "class_name", "method_name",
                            "method_signature", "dependency_beans", "call_flow"}
    assert payload["dependency_beans"]


def test_mcp_client_attaches_context_to_units(tmp_path: Path):
    java = tmp_path / "src" / "main" / "java" / "OrderService.java"
    java.parent.mkdir(parents=True)
    java.write_text(SERVICE, encoding="utf-8")

    unit = _unit(file_path="src/main/java/OrderService.java")
    contexts = get_ast_client("inprocess").attach_contexts(tmp_path, [unit])

    assert len(contexts) == 1
    assert unit.context is not None
    assert unit.context.dependency_beans == ["OrderRepository", "DiscountPolicy"]


def test_mcp_stdio_server_speaks_jsonrpc(tmp_path: Path):
    from codetest.mcp.server import handle_request

    java = tmp_path / "Foo.java"
    java.write_text(SERVICE, encoding="utf-8")

    init = handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "codetest-ast"

    listed = handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert {t["name"] for t in listed["result"]["tools"]} == {
        "ast_method_context", "ast_change_context"}

    called = handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "ast_change_context",
                   "arguments": {"project_dir": str(tmp_path),
                                 "targets": [{"file_path": "Foo.java",
                                              "class_name": "OrderService",
                                              "method_name": "calculateTotal"}]}},
    })
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"]["contexts"][0]["dependency_beans"]


def test_request_payload_carries_context_not_source():
    unit = _unit()
    ctx = MethodContext(
        file_path=unit.file_path, class_name="OrderService", method_name="calculateTotal",
        method_signature="public double calculateTotal(int qty, double price)",
        dependency_beans=["OrderRepository"], call_flow="OrderRepository.save()",
    )
    req = TestGenRequest(units=[unit], project_package="com.example.demo", contexts=[ctx])
    block = req.prompt_context()
    assert "OrderRepository" in block and "calculateTotal" in block
    assert not hasattr(req, "diffs_by_file")


# --------------------------------------------------------------------------- #
# 3. session pipeline stays in memory
def test_default_store_is_memory_and_writes_no_db(tmp_path: Path):
    cfg = Config.resolve(tmp_path)
    assert cfg.persist is False

    store = get_store(cfg.persist, cfg.db_path)
    assert isinstance(store, MemoryFeatureStore)
    assert store.kind == "memory"

    store.upsert_features("Foo.java", ast_analyzer.analyze_source(SERVICE))
    store.record_run("run", "ok")
    assert store.feature_count() > 0
    assert store.all_features()[0]["class_name"] == "OrderService"
    assert not cfg.db_path.exists()

    cfg.ensure_dirs()
    assert not cfg.db_path.parent.exists()   # no .codetest dir without --persist


def test_persist_flag_switches_to_sqlite(tmp_path: Path):
    cfg = Config.resolve(tmp_path, persist=True)
    store = get_store(cfg.persist, cfg.db_path)
    assert store.kind == "sqlite"
    store.upsert_features("Foo.java", ast_analyzer.analyze_source(SERVICE))
    assert cfg.db_path.exists()
    assert store.feature_count() > 0


def test_analyze_changes_passes_data_in_memory(tmp_path: Path):
    from codetest.analyzer import analyze_changes

    repo, java = _repo_with_baseline(
        tmp_path,
        "package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n",
    )
    java.write_text("package com.example;\npublic class Foo {\n"
                    "  public int a(){return 2;}\n}\n", encoding="utf-8")

    cfg = Config.resolve(repo)
    store = MemoryFeatureStore()
    analysis = analyze_changes(cfg, "working", store=store)

    assert analysis.units                      # returned as variables, not via DB
    assert store.feature_count() > 0
    assert not cfg.db_path.exists()


# --------------------------------------------------------------------------- #
# 4. whitespace / blank-line noise is ignored
def test_git_analyzer_detects_working_change(tmp_path: Path):
    repo, java = _repo_with_baseline(
        tmp_path,
        "package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n",
    )
    java.write_text("package com.example;\npublic class Foo {\n"
                    "  public int a(){return 2;}\n  public int b(){return 3;}\n}\n",
                    encoding="utf-8")

    diffs = get_file_diffs(repo, "working")
    assert any(d.path.endswith("Foo.java") for d in diffs)


def test_whitespace_only_change_is_ignored(tmp_path: Path):
    repo, java = _repo_with_baseline(
        tmp_path,
        "package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n",
    )
    # reindent + stray blank lines, no semantic change
    java.write_text("package com.example;\n\npublic class Foo {\n\n"
                    "      public int a(){return 1;}\n\n}\n", encoding="utf-8")

    scan = scan_changes(repo, "working", DiffOptions())
    assert scan.diffs == []
    assert any(p.endswith("Foo.java") for p in scan.skipped_whitespace_only)


def test_whitespace_change_is_kept_when_option_disabled(tmp_path: Path):
    repo, java = _repo_with_baseline(
        tmp_path,
        "package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n",
    )
    java.write_text("package com.example;\n\npublic class Foo {\n\n"
                    "      public int a(){return 1;}\n\n}\n", encoding="utf-8")

    scan = scan_changes(repo, "working",
                        DiffOptions(ignore_whitespace=False, ignore_blank_lines=False))
    assert any(d.path.endswith("Foo.java") for d in scan.diffs)


def test_real_change_survives_whitespace_filter(tmp_path: Path):
    repo, java = _repo_with_baseline(
        tmp_path,
        "package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n",
    )
    # reformatting *and* a real edit
    java.write_text("package com.example;\n\npublic class Foo {\n\n"
                    "      public int a(){return 42;}\n\n}\n", encoding="utf-8")

    scan = scan_changes(repo, "working", DiffOptions())
    assert len(scan.diffs) == 1
    assert all(line.strip() for line in scan.diffs[0].added_lines)
    assert any("42" in line for line in scan.diffs[0].added_lines)


def test_untracked_file_is_not_confused_with_whitespace_change(tmp_path: Path):
    repo, _ = _repo_with_baseline(
        tmp_path,
        "package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n",
    )
    new = repo / "src" / "main" / "java" / "com" / "example" / "Bar.java"
    new.write_text("package com.example;\n\npublic class Bar {\n\n"
                   "  public int b(){return 1;}\n}\n", encoding="utf-8")

    scan = scan_changes(repo, "working", DiffOptions())
    bar = next(d for d in scan.diffs if d.path.endswith("Bar.java"))
    assert bar.is_new_file
    assert all(line.strip() for line in bar.added_lines)


# --------------------------------------------------------------------------- #
# end-to-end
def test_pipeline_end_to_end_in_memory(tmp_path: Path):
    from codetest import pipeline

    repo, java = _repo_with_baseline(tmp_path, SERVICE.replace("qty > 10", "qty > 1000"))
    java.write_text(SERVICE, encoding="utf-8")

    cfg = Config.resolve(repo)
    report = pipeline.build_report(cfg, "working", "generate", run_tests_flag=False)

    assert report.items
    item = report.items[0]
    assert item.artifact.llm_calls == 1
    assert item.unit.intent_reason and item.unit.importance_reason
    assert not cfg.db_path.exists()          # no DB round-trip in a normal session
    assert any("LLM 호출: 1회" in n for n in report.notes)
