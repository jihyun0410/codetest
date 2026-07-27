"""[계층 1] CLI end-to-end — 4계층을 실제로 관통하는 시나리오."""
from __future__ import annotations

from pathlib import Path

from conftest import CONTROLLER, SERVICE, git, init_repo
from typer.testing import CliRunner

from codetest.agent import pipeline
from codetest.cli.cli_parser import app
from codetest.config import Config

runner = CliRunner()


def _changed_repo(tmp_path: Path) -> Path:
    """Repo where OrderService really changed and OrderController only got reformatted."""
    repo, java = init_repo(tmp_path, SERVICE.replace("qty > 10", "qty > 1000"))
    controller = repo / "src" / "main" / "java" / "com" / "example" / "OrderController.java"
    controller.write_text(CONTROLLER, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "add controller")

    java.write_text(SERVICE, encoding="utf-8")                       # 실제 변경
    controller.write_text(
        "\n".join("    " + l if l.startswith("    ") else l
                  for l in CONTROLLER.splitlines()) + "\n", encoding="utf-8")   # 공백만
    return repo


# --------------------------------------------------------------------------- #
# pipeline end-to-end
def test_generate_runs_in_memory_with_one_call(tmp_path: Path):
    repo = _changed_repo(tmp_path)
    cfg = Config.resolve(repo)

    report = pipeline.build_report(cfg, "working", "generate", run_tests_flag=False)

    assert report.items
    item = report.items[0]
    assert item.artifact.llm_calls == 1
    assert item.unit.intent_reason and item.unit.importance_reason
    assert item.result is None                        # generate 전용
    assert Path(item.artifact.file_path).exists()
    assert not cfg.db_path.exists()                   # DB 미경유
    assert any("LLM 호출: 1회" in n for n in report.notes)
    assert any("OrderController.java" in n for n in report.notes)   # 공백 변경 제외 안내


def test_run_executes_and_judges_validity(tmp_path: Path):
    repo = _changed_repo(tmp_path)
    report = pipeline.build_report(Config.resolve(repo), "working", "run",
                                   run_tests_flag=True)

    result = report.items[0].result
    assert result is not None
    assert result.executor in ("simulated", "gradle")
    assert result.validity in ("valid", "invalid", "inconclusive")
    assert result.validity_reason


def test_no_changes_reports_cleanly(tmp_path: Path):
    repo, _ = init_repo(tmp_path)
    report = pipeline.build_report(Config.resolve(repo), "working", "generate", False)
    assert report.items == []
    assert any("찾지 못했습니다" in n for n in report.notes)


def test_stdio_transport_produces_the_same_report(tmp_path: Path):
    repo = _changed_repo(tmp_path)

    local = pipeline.build_report(Config.resolve(repo), "working", "generate", False)
    remote = pipeline.build_report(Config.resolve(repo, mcp_transport="stdio"),
                                   "working", "generate", False)

    assert [i.unit.display_name for i in local.items] == \
           [i.unit.display_name for i in remote.items]
    assert local.items[0].artifact.source == remote.items[0].artifact.source


def test_provided_test_txt_is_executed(tmp_path: Path):
    repo, _ = init_repo(tmp_path)
    txt = repo / "src" / "test" / "test.txt"
    txt.parent.mkdir(parents=True, exist_ok=True)
    txt.write_text("package com.example;\npublic class ProvidedOrderTest {\n"
                   "  @Test void ok() {}\n}\n", encoding="utf-8")

    report = pipeline.build_report_from_txt(Config.resolve(repo), "test")

    assert len(report.items) == 1
    artifact = report.items[0].artifact
    assert artifact.llm_calls == 0                    # 사용자 코드 — 호출 없음
    assert "@SpringBootTest" in artifact.source
    assert Path(artifact.file_path).exists()


def test_missing_test_txt_is_reported(tmp_path: Path):
    repo, _ = init_repo(tmp_path)
    report = pipeline.build_report_from_txt(Config.resolve(repo), "test")
    assert report.items == []
    assert any("테스트 파일이 없습니다" in n for n in report.notes)


# --------------------------------------------------------------------------- #
# CLI 명령
def test_generate_command_exits_cleanly(tmp_path: Path):
    repo = _changed_repo(tmp_path)
    result = runner.invoke(app, ["generate", "-p", str(repo), "--no-interactive"])
    assert result.exit_code == 0, result.output
    assert "변경 요약" in result.output


def test_features_command_without_persist_explains_memory_mode(tmp_path: Path):
    repo, _ = init_repo(tmp_path)
    result = runner.invoke(app, ["features", "-p", str(repo)])
    assert result.exit_code == 0
    assert "--persist" in result.output


def test_persist_then_features_lists_rows(tmp_path: Path):
    repo = _changed_repo(tmp_path)
    assert runner.invoke(app, ["generate", "-p", str(repo), "--no-interactive",
                               "--persist"]).exit_code == 0
    result = runner.invoke(app, ["features", "-p", str(repo)])
    assert result.exit_code == 0
    assert "OrderService" in result.output


def test_run_outside_a_git_repo_fails_with_a_message(tmp_path: Path):
    result = runner.invoke(app, ["generate", "-p", str(tmp_path), "--no-interactive"])
    assert result.exit_code == 1
    assert "[git]" in result.output


def test_mcp_serve_rejects_an_unknown_server():
    result = runner.invoke(app, ["mcp-serve", "nope"])
    assert result.exit_code == 1
    assert "unknown MCP server" in result.output
