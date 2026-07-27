"""Unit tests for the agent's core stages (no Java toolchain required)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codetest import ast_analyzer, intent
from codetest.config import Config
from codetest.git_analyzer import FileDiff, get_file_diffs
from codetest.llm import get_client
from codetest.llm.base import TestGenRequest
from codetest.models import ChangeUnit, MethodInfo


SERVICE = """package com.example.demo.service;
import org.springframework.stereotype.Service;
@Service
public class OrderService {
    public double calculateTotal(int qty, double price) {
        if (qty > 10) {
            return qty * price * 0.9;
        }
        return qty * price;
    }
}
"""


def test_ast_finds_methods():
    classes = ast_analyzer.analyze_source(SERVICE)
    assert classes
    names = {m.name for c in classes for m in c.methods}
    assert "calculateTotal" in names


def test_intent_condition_detected():
    diff = FileDiff(
        path="src/main/java/com/example/demo/service/OrderService.java",
        diff_text="", added_lines=["if (qty > 10) {", "return qty * price * 0.9;"],
        removed_lines=["return qty * price;"], changed_new_lines=[5, 6], is_new_file=False,
    )
    kind, reason = intent.classify_intent(diff, is_new_method=False)
    assert kind in ("condition", "performance")
    assert reason


def test_importance_service_is_at_least_mid():
    diff = FileDiff(
        path="src/main/java/com/example/demo/service/OrderService.java",
        diff_text="", added_lines=["if (x) {}"] * 8, removed_lines=[],
        changed_new_lines=list(range(1, 9)), is_new_file=False,
    )
    unit = ChangeUnit(file_path=diff.path, class_name="OrderService",
                      method=MethodInfo("calculateTotal", "calculateTotal()", 4, 9,
                                        ["public"], "double"),
                      changed_lines=[5], added_lines=diff.added_lines,
                      removed_lines=[], intent="condition")
    assert intent.score_importance(unit, diff) in ("Mid", "High")


def test_mock_llm_generates_springboot_test():
    unit = ChangeUnit(
        file_path="src/main/java/com/example/demo/service/OrderService.java",
        class_name="OrderService",
        method=MethodInfo("calculateTotal", "calculateTotal()", 4, 9, ["public"], "double"),
        changed_lines=[5], added_lines=["if (x) {}"], removed_lines=[],
        intent="condition", intent_reason="cond", importance="High",
    )
    llm = get_client("mock")
    req = TestGenRequest(units=[unit], project_package="com.example.demo")
    reasoning = llm.reason(req)
    source = llm.generate_test(req, reasoning)
    assert "@SpringBootTest" in source
    assert "class OrderServiceGeneratedTest" in source
    assert "@Test" in source
    assert reasoning.scenarios


def test_git_analyzer_detects_working_change(tmp_path: Path):
    repo = tmp_path / "repo"
    src = repo / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    java = src / "Foo.java"
    java.write_text("package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n")

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True,
                       capture_output=True, text=True)

    git("init")
    git("config", "user.email", "t@t.com")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-m", "base")
    java.write_text("package com.example;\npublic class Foo {\n"
                    "  public int a(){return 2;}\n  public int b(){return 3;}\n}\n")

    diffs = get_file_diffs(repo, "working")
    assert any(d.path.endswith("Foo.java") for d in diffs)
