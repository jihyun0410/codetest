"""Shared fixtures/helpers for the layer-by-layer test suite."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Tuple

import pytest

from codetest.models import ChangeUnit, MethodInfo

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

CONTROLLER = """package com.example.demo.controller;
import org.springframework.web.bind.annotation.RestController;
@RestController
public class OrderController {

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    public double total(int qty, double price) {
        return orderService.calculateTotal(qty, price);
    }
}
"""

BASELINE_JAVA = "package com.example;\npublic class Foo {\n  public int a(){return 1;}\n}\n"


def make_unit(**overrides) -> ChangeUnit:
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


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def init_repo(tmp_path: Path, content: str = BASELINE_JAVA) -> Tuple[Path, Path]:
    """Create a git repo with one committed Java file. Returns (repo, file)."""
    repo = tmp_path / "repo"
    src = repo / "src" / "main" / "java" / "com" / "example"
    src.mkdir(parents=True)
    java = src / "Foo.java"
    java.write_text(content, encoding="utf-8")

    git(repo, "init")
    git(repo, "config", "user.email", "t@t.com")
    git(repo, "config", "user.name", "t")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo, java


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return init_repo(tmp_path)[0]


@pytest.fixture(autouse=True)
def _clear_ast_cache():
    """The AST cache is process-wide; tests must not leak entries into each other."""
    from codetest.storage import default_cache

    default_cache().clear()
    yield
    default_cache().clear()
