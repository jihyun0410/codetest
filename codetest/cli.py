"""`codetest` CLI (Typer).

Commands (per the spec):

    codetest run             generate + test for local (unstaged) changes, report
    codetest run --stage     generate + test for staged changes, report
    codetest generate        generate test code only (Git Working Tree changes)
    codetest test            run the test in <project>/src/test/test.txt, report
    codetest features        inspect the AST feature DB (persisted runs only)
    codetest mcp-serve       run the AST MCP server on stdio
"""
from __future__ import annotations

import sys

# Ensure Unicode (Korean/box-drawing) renders on legacy Windows consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from typing import Optional

import typer

from . import pipeline, test_txt, ui
from .config import Config
from .db import SQLiteFeatureDB
from .git_analyzer import GitError

app = typer.Typer(
    add_completion=False,
    help="Code Test AI Agent — generate & run Spring Boot tests for changed code.",
    no_args_is_help=True,
)


def _cfg(project: Optional[str], llm: Optional[str], persist: bool = False,
         ignore_whitespace: bool = True, ast_mcp: Optional[str] = None) -> Config:
    return Config.resolve(project, llm, persist=persist,
                          ignore_whitespace=ignore_whitespace,
                          ignore_blank_lines=ignore_whitespace,
                          ast_mcp_transport=ast_mcp)


def _finish(report, show: Optional[str], no_interactive: bool) -> None:
    ui.render_report(report, show=show)
    if not no_interactive and show is None and report.items:
        ui.interactive(report)


# Shared options
_ProjectOpt = typer.Option(None, "--project", "-p",
                           help="Target Spring Boot project (default: cwd).")
_LlmOpt = typer.Option(None, "--llm", help="LLM backend: mock (default) | claude.")
_ShowOpt = typer.Option(None, "--show",
                        help="Non-interactively expand: code | result | all.")
_NoInteractiveOpt = typer.Option(False, "--no-interactive",
                                 help="Disable the interactive expand prompt.")
_PersistOpt = typer.Option(False, "--persist",
                           help="Also store features/runs in SQLite. "
                                "Default: 세션 파이프라인은 메모리에서만 처리.")
_WhitespaceOpt = typer.Option(True, "--ignore-whitespace/--no-ignore-whitespace",
                              help="공백/줄바꿈만 바뀐 변경을 diff에서 무시 (기본: 무시).")
_AstMcpOpt = typer.Option(None, "--ast-mcp",
                          help="AST MCP transport: inprocess (default) | stdio.")


@app.command()
def run(
    stage: bool = typer.Option(False, "--stage", "-stage",
                               help="Target staged changes instead of the working tree."),
    project: Optional[str] = _ProjectOpt,
    llm: Optional[str] = _LlmOpt,
    show: Optional[str] = _ShowOpt,
    no_interactive: bool = _NoInteractiveOpt,
    persist: bool = _PersistOpt,
    ignore_whitespace: bool = _WhitespaceOpt,
    ast_mcp: Optional[str] = _AstMcpOpt,
):
    """Generate tests for changed files, run them, and report."""
    cfg = _cfg(project, llm, persist, ignore_whitespace, ast_mcp)
    mode = "staged" if stage else "working"
    cmd = "run --stage" if stage else "run"
    try:
        report = pipeline.build_report(cfg, mode, cmd, run_tests_flag=True)
    except GitError as e:
        typer.secho(f"[git] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    _finish(report, show, no_interactive)


@app.command()
def generate(
    stage: bool = typer.Option(False, "--stage", "-stage",
                               help="Target staged changes instead of the working tree."),
    project: Optional[str] = _ProjectOpt,
    llm: Optional[str] = _LlmOpt,
    show: Optional[str] = _ShowOpt,
    no_interactive: bool = _NoInteractiveOpt,
    persist: bool = _PersistOpt,
    ignore_whitespace: bool = _WhitespaceOpt,
    ast_mcp: Optional[str] = _AstMcpOpt,
):
    """Generate test code only (no execution) from Git Working Tree changes."""
    cfg = _cfg(project, llm, persist, ignore_whitespace, ast_mcp)
    mode = "staged" if stage else "working"
    try:
        report = pipeline.build_report(cfg, mode, "generate", run_tests_flag=False)
    except GitError as e:
        typer.secho(f"[git] {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    _finish(report, show, no_interactive)


@app.command()
def test(
    project: Optional[str] = _ProjectOpt,
    llm: Optional[str] = _LlmOpt,
    show: Optional[str] = _ShowOpt,
    no_interactive: bool = _NoInteractiveOpt,
    persist: bool = _PersistOpt,
):
    """Run the test stored in <project>/src/test/test.txt and report."""
    cfg = _cfg(project, llm, persist)
    report = test_txt.build_report_from_txt(cfg, "test")
    _finish(report, show, no_interactive)


@app.command()
def features(
    project: Optional[str] = _ProjectOpt,
):
    """List the AST-discovered features persisted by earlier `--persist` runs."""
    cfg = _cfg(project, None)
    if not cfg.db_path.exists():
        typer.echo(
            f"저장된 feature DB가 없습니다: {cfg.db_path}\n"
            "기본 실행은 메모리에서만 처리되므로, 보존하려면 "
            "`codetest run --persist` 로 실행하세요."
        )
        raise typer.Exit()
    rows = SQLiteFeatureDB(cfg.db_path).all_features()
    if not rows:
        typer.echo("저장된 feature가 없습니다. `codetest run --persist` 를 실행하세요.")
        raise typer.Exit()
    from rich.console import Console
    from rich.table import Table
    table = Table(title=f"Feature DB ({len(rows)}개)  ·  {cfg.db_path}")
    for col in ("class", "method", "signature", "mods", "file"):
        table.add_column(col)
    for r in rows:
        table.add_row(r["class_name"], r["method_name"] or "-",
                      r["signature"] or "-", r["modifiers"] or "-", r["file_path"])
    Console().print(table)


@app.command("mcp-serve")
def mcp_serve():
    """Run the AST MCP server on stdio (for an external MCP host)."""
    from .mcp.server import main as serve_main
    serve_main()


if __name__ == "__main__":
    app()
