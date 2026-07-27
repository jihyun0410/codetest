"""① Git & File MCP Server."""
from __future__ import annotations

from pathlib import Path

from conftest import BASELINE_JAVA, SERVICE, init_repo

from codetest.mcp import get_client
from codetest.mcp.git_file import file_tool, git_tool
from codetest.mcp.git_file.server import build_server
from codetest.models import DiffOptions


# --------------------------------------------------------------------------- #
# git_tool — 노이즈 필터링
def test_detects_working_change(tmp_path: Path):
    repo, java = init_repo(tmp_path)
    java.write_text("package com.example;\npublic class Foo {\n"
                    "  public int a(){return 2;}\n  public int b(){return 3;}\n}\n",
                    encoding="utf-8")

    diffs = git_tool.get_file_diffs(repo, "working")
    assert any(d.path.endswith("Foo.java") for d in diffs)


def test_whitespace_only_change_is_ignored(tmp_path: Path):
    repo, java = init_repo(tmp_path)
    # reindent + stray blank lines, no semantic change
    java.write_text("package com.example;\n\npublic class Foo {\n\n"
                    "      public int a(){return 1;}\n\n}\n", encoding="utf-8")

    scan = git_tool.scan_changes(repo, "working", DiffOptions())
    assert scan.diffs == []
    assert any(p.endswith("Foo.java") for p in scan.skipped_whitespace_only)


def test_whitespace_change_is_kept_when_option_disabled(tmp_path: Path):
    repo, java = init_repo(tmp_path)
    java.write_text("package com.example;\n\npublic class Foo {\n\n"
                    "      public int a(){return 1;}\n\n}\n", encoding="utf-8")

    scan = git_tool.scan_changes(
        repo, "working", DiffOptions(ignore_whitespace=False, ignore_blank_lines=False))
    assert any(d.path.endswith("Foo.java") for d in scan.diffs)


def test_real_change_survives_whitespace_filter(tmp_path: Path):
    repo, java = init_repo(tmp_path)
    # reformatting *and* a real edit
    java.write_text("package com.example;\n\npublic class Foo {\n\n"
                    "      public int a(){return 42;}\n\n}\n", encoding="utf-8")

    scan = git_tool.scan_changes(repo, "working", DiffOptions())
    assert len(scan.diffs) == 1
    assert all(line.strip() for line in scan.diffs[0].added_lines)
    assert any("42" in line for line in scan.diffs[0].added_lines)


def test_untracked_file_is_not_confused_with_whitespace_change(tmp_path: Path):
    repo, _ = init_repo(tmp_path)
    new = repo / "src" / "main" / "java" / "com" / "example" / "Bar.java"
    new.write_text("package com.example;\n\npublic class Bar {\n\n"
                   "  public int b(){return 1;}\n}\n", encoding="utf-8")

    scan = git_tool.scan_changes(repo, "working", DiffOptions())
    bar = next(d for d in scan.diffs if d.path.endswith("Bar.java"))
    assert bar.is_new_file
    assert all(line.strip() for line in bar.added_lines)


def test_staged_mode_sees_only_staged_changes(tmp_path: Path):
    from conftest import git

    repo, java = init_repo(tmp_path)
    java.write_text(BASELINE_JAVA.replace("return 1", "return 7"), encoding="utf-8")
    assert git_tool.scan_changes(repo, "staged").diffs == []

    git(repo, "add", "-A")
    assert any(d.path.endswith("Foo.java")
               for d in git_tool.scan_changes(repo, "staged").diffs)


# --------------------------------------------------------------------------- #
# file_tool
def test_write_test_file_lands_in_package_dir(tmp_path: Path):
    path = file_tool.write_test_file(tmp_path / "src" / "test" / "java",
                                     "com.example.demo", "FooTest", "class FooTest {}")
    assert path.exists()
    assert path.parts[-4:] == ("com", "example", "demo", "FooTest.java")
    assert file_tool.test_file_path(tmp_path / "src" / "test" / "java",
                                    "com.example.demo", "FooTest") == path


def test_ensure_springboot_annotation_is_idempotent():
    src = "package com.example;\npublic class FooTest {\n}\n"
    once = file_tool.ensure_springboot_annotation(src)
    assert "@SpringBootTest" in once
    assert once.count("@SpringBootTest") == 1
    assert file_tool.ensure_springboot_annotation(once) == once


def test_parse_java_identity():
    assert file_tool.parse_java_identity(SERVICE) == ("com.example.demo.service",
                                                      "OrderService")
    assert file_tool.parse_java_identity("class X {}", "d.p", "Fallback") == ("d.p", "X")


# --------------------------------------------------------------------------- #
# server / client
def test_server_exposes_its_tools():
    assert set(build_server().registry.tools) == {
        "git_scan_changes", "file_read_source", "file_write_test"}


def test_inprocess_client_scans(tmp_path: Path):
    repo, java = init_repo(tmp_path)
    java.write_text(BASELINE_JAVA.replace("return 1", "return 9"), encoding="utf-8")

    payload = get_client("git_file").call("git_scan_changes",
                                          {"project_dir": str(repo), "mode": "working"})
    assert payload["diffs"]


def test_stdio_client_matches_inprocess(tmp_path: Path):
    repo, java = init_repo(tmp_path)
    java.write_text(BASELINE_JAVA.replace("return 1", "return 9"), encoding="utf-8")
    args = {"project_dir": str(repo), "mode": "working"}

    local = get_client("git_file", "inprocess").call("git_scan_changes", args)
    remote = get_client("git_file", "stdio").call("git_scan_changes", args)
    assert [d["path"] for d in local["diffs"]] == [d["path"] for d in remote["diffs"]]
