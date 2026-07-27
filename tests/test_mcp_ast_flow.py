"""② AST & Flow MCP Server."""
from __future__ import annotations

from pathlib import Path

from conftest import CONTROLLER, SERVICE

from codetest.mcp import get_client
from codetest.mcp.ast_flow import ast_tool, flow_tool, pruner
from codetest.mcp.ast_flow.server import build_server
from codetest.models import MethodContext
from codetest.storage import AstCache


# --------------------------------------------------------------------------- #
# ast_tool
def test_ast_finds_methods():
    classes = ast_tool.analyze_source(SERVICE)
    assert classes
    assert "calculateTotal" in {m.name for c in classes for m in c.methods}


def test_regex_fallback_handles_unparseable_source():
    broken = "package com.example;\npublic class Broken {\n  public int a(){ return 1;\n"
    classes = ast_tool._parse_with_regex(broken)
    assert classes[0].name == "Broken"
    assert [m.name for m in classes[0].methods] == ["a"]


def test_find_method_for_lines_maps_changed_lines():
    classes = ast_tool.analyze_source(SERVICE)
    method = next(m for c in classes for m in c.methods if m.name == "calculateTotal")
    hits = ast_tool.find_method_for_lines(classes, [method.start_line + 1])
    assert [name for name, _ in hits] == ["OrderService"]


def test_ast_cache_avoids_reparsing(tmp_path: Path):
    java = tmp_path / "OrderService.java"
    java.write_text(SERVICE, encoding="utf-8")
    cache = AstCache()

    first = ast_tool.analyze_file(java, cache)
    second = ast_tool.analyze_file(java, cache)
    assert second is first          # same object → served from cache
    assert (cache.hits, cache.misses) == (1, 1)

    java.write_text(SERVICE.replace("qty > 10", "qty > 20"), encoding="utf-8")
    assert ast_tool.analyze_file(java, cache) is not first   # fingerprint changed


# --------------------------------------------------------------------------- #
# pruner — 가지치기
def test_pruner_keeps_only_signature_beans_and_flow():
    classes = ast_tool.analyze_source(SERVICE)
    ctx = pruner.build_method_context(
        SERVICE, classes, "src/.../OrderService.java", "OrderService", "calculateTotal")

    assert "calculateTotal" in ctx.method_signature
    assert set(ctx.dependency_beans) == {"OrderRepository", "DiscountPolicy"}
    assert "DiscountPolicy.apply()" in ctx.call_flow
    assert "OrderRepository.save()" in ctx.call_flow
    # the raw source must never be part of the payload
    assert "import org.springframework" not in str(ctx.to_dict())


def test_pruner_drops_dto_accessors_from_call_flow():
    source = SERVICE.replace("double subtotal = qty * price;",
                             "double subtotal = order.getQuantity() * order.getUnitPrice();")
    classes = ast_tool.analyze_source(source)
    ctx = pruner.build_method_context(source, classes, "f.java", "OrderService",
                                      "calculateTotal")
    assert "getQuantity" not in ctx.call_flow
    assert "DiscountPolicy.apply()" in ctx.call_flow


def test_pruner_class_level_change_summarizes_public_surface():
    classes = ast_tool.analyze_source(SERVICE)
    ctx = pruner.build_method_context(SERVICE, classes, "f.java", "OrderService")
    assert ctx.method_name == ""
    assert "class OrderService" in ctx.method_signature
    assert "calculateTotal" in ctx.method_signature


def test_local_variables_are_not_treated_as_beans():
    classes = ast_tool.analyze_source(SERVICE)
    beans, _ = pruner.collect_dependency_beans(SERVICE, classes[0])
    assert "String" not in beans and "Order" not in beans


# --------------------------------------------------------------------------- #
# flow_tool — 다중 파일 호출 순서
def _ctx(cls: str, method: str, beans, path: str) -> MethodContext:
    return MethodContext(file_path=path, class_name=cls, method_name=method,
                         method_signature=f"public void {method}()",
                         dependency_beans=list(beans))


def test_flow_orders_controller_before_service():
    contexts = [
        _ctx("OrderService", "calculateTotal", ["OrderRepository"],
             "src/main/java/com/example/service/OrderService.java"),
        _ctx("OrderController", "total", ["OrderService"],
             "src/main/java/com/example/controller/OrderController.java"),
    ]
    flow = flow_tool.build_flow(contexts)

    assert flow.steps == ["OrderController#total()", "OrderService#calculateTotal()"]
    assert flow.entry_point == "OrderController"
    assert flow.external_beans == ["OrderRepository"]


def test_flow_is_empty_for_no_contexts():
    assert flow_tool.build_flow([]).steps == []


def test_flow_survives_a_dependency_cycle():
    contexts = [
        _ctx("AService", "a", ["BService"], "service/AService.java"),
        _ctx("BService", "b", ["AService"], "service/BService.java"),
    ]
    flow = flow_tool.build_flow(contexts)
    assert sorted(flow.steps) == ["AService#a()", "BService#b()"]


# --------------------------------------------------------------------------- #
# server / client
def test_server_exposes_its_tools():
    assert set(build_server().registry.tools) == {
        "ast_parse_file", "ast_method_context", "ast_change_context", "flow_summary"}


def test_method_context_tool_returns_only_three_fields(tmp_path: Path):
    java = tmp_path / "src" / "OrderService.java"
    java.parent.mkdir(parents=True)
    java.write_text(SERVICE, encoding="utf-8")

    payload = get_client("ast_flow").call("ast_method_context", {
        "project_dir": str(tmp_path), "file_path": "src/OrderService.java",
        "class_name": "OrderService", "method_name": "calculateTotal",
    })
    assert set(payload) == {"file_path", "class_name", "method_name",
                            "method_signature", "dependency_beans", "call_flow"}
    assert payload["dependency_beans"]


def test_change_context_tool_reports_missing_files(tmp_path: Path):
    payload = get_client("ast_flow").call("ast_change_context", {
        "project_dir": str(tmp_path), "targets": [{"file_path": "nope/Missing.java"}],
    })
    assert payload["contexts"] == []
    assert payload["errors"]


def test_stdio_transport_returns_the_same_payload(tmp_path: Path):
    java = tmp_path / "OrderService.java"
    java.write_text(SERVICE, encoding="utf-8")
    args = {"project_dir": str(tmp_path),
            "targets": [{"file_path": "OrderService.java",
                         "class_name": "OrderService", "method_name": "calculateTotal"}]}

    local = get_client("ast_flow", "inprocess").call("ast_change_context", args)
    remote = get_client("ast_flow", "stdio").call("ast_change_context", args)
    assert local == remote
    assert local["contexts"][0]["dependency_beans"]


def test_jsonrpc_handshake_and_tool_listing(tmp_path: Path):
    server = build_server()

    init = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "codetest-ast-flow"
    assert server.handle_request(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    listed = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "ast_change_context" in {t["name"] for t in listed["result"]["tools"]}

    unknown = server.handle_request({"jsonrpc": "2.0", "id": 3, "method": "nope"})
    assert unknown["error"]["code"] == -32601


def test_tool_error_is_reported_not_raised():
    called = build_server().call_tool("ast_method_context", {"project_dir": ".",
                                                            "file_path": "missing.java"})
    assert called["isError"] is True
    assert "error:" in called["content"][0]["text"]


def test_flow_summary_tool_roundtrips_contexts():
    contexts = [
        _ctx("OrderController", "total", ["OrderService"], "controller/OrderController.java"),
        _ctx("OrderService", "calculateTotal", [], "service/OrderService.java"),
    ]
    payload = get_client("ast_flow").call(
        "flow_summary", {"contexts": [c.to_dict() for c in contexts]})
    assert payload["entry_point"] == "OrderController"
    assert "→" in payload["summary"]
