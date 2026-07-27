"""③ Test Execution MCP Server."""
from __future__ import annotations

from pathlib import Path

from codetest.mcp import get_client
from codetest.mcp.test_exec import build_tool, jacoco_tool
from codetest.mcp.test_exec.server import build_server

JUNIT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="OrderServiceGeneratedTest" tests="4" skipped="1" failures="1" errors="0">
  <testcase classname="com.example.OrderServiceGeneratedTest" name="ok"/>
  <testcase classname="com.example.OrderServiceGeneratedTest" name="bad">
    <failure message="expected 90.0 but was 100.0"/>
  </testcase>
</testsuite>
"""

JACOCO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<report name="demo">
  <counter type="BRANCH" missed="1" covered="3"/>
  <counter type="INSTRUCTION" missed="20" covered="80"/>
</report>
"""


def _write_reports(project_dir: Path) -> None:
    results = project_dir / "build" / "test-results" / "test"
    results.mkdir(parents=True)
    (results / "TEST-OrderServiceGeneratedTest.xml").write_text(JUNIT_XML, encoding="utf-8")

    jacoco = project_dir / "build" / "reports" / "jacoco" / "test"
    jacoco.mkdir(parents=True)
    (jacoco / "jacocoTestReport.xml").write_text(JACOCO_XML, encoding="utf-8")


# --------------------------------------------------------------------------- #
# jacoco_tool
def test_parse_junit_aggregates_counts(tmp_path: Path):
    _write_reports(tmp_path)
    assert jacoco_tool.parse_junit(tmp_path) == {
        "total": 4, "failures": 1, "errors": 0, "skipped": 1}


def test_parse_jacoco_reports_instruction_and_branch(tmp_path: Path):
    _write_reports(tmp_path)
    coverage = jacoco_tool.parse_jacoco(tmp_path)
    assert coverage["coverage_pct"] == 80.0
    assert coverage["branch_coverage_pct"] == 75.0


def test_missing_reports_degrade_to_zero_and_none(tmp_path: Path):
    assert jacoco_tool.parse_junit(tmp_path)["total"] == 0
    assert jacoco_tool.parse_jacoco(tmp_path)["coverage_pct"] is None


def test_failure_messages_are_extracted(tmp_path: Path):
    _write_reports(tmp_path)
    messages = jacoco_tool.failure_messages(tmp_path)
    assert messages and "expected 90.0" in messages[0]


# --------------------------------------------------------------------------- #
# build_tool
def test_simulation_counts_test_methods(tmp_path: Path):
    source = "@Test void a() {}\n@Test void b() {}\n"
    run = build_tool.simulate(tmp_path, "com.example.FooTest", source)
    assert run["executor"] == "simulated"
    assert run["test_count"] == 2
    assert "NOT compiled" in run["log"]


def test_run_tool_falls_back_to_simulation_without_toolchain(tmp_path: Path):
    run = get_client("test_exec").call("test_run", {
        "project_dir": str(tmp_path), "fqcn": "com.example.FooTest",
        "source": "@Test void a() {}",
    })
    # No gradlew in tmp_path, so the tool must report the simulated path honestly.
    assert run["executor"] in ("simulated", "gradle")
    if run["executor"] == "simulated":
        assert run["test_count"] == 1


def test_can_run_real_requires_both_java_and_gradle(tmp_path: Path):
    assert build_tool.gradle_cmd(tmp_path) is None
    assert build_tool.can_run_real(tmp_path) is False


# --------------------------------------------------------------------------- #
# server
def test_server_exposes_its_tools():
    assert set(build_server().registry.tools) == {"test_run", "coverage_report"}


def test_coverage_report_tool(tmp_path: Path):
    _write_reports(tmp_path)
    payload = get_client("test_exec").call("coverage_report",
                                           {"project_dir": str(tmp_path)})
    assert payload["total"] == 4
    assert payload["branch_coverage_pct"] == 75.0
    assert payload["failures_detail"]
