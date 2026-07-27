"""`codetest` CLI (Typer).

Commands (per the spec):

    codetest run             generate + test for local (unstaged) changes, report
    codetest run --stage     generate + test for staged changes, report
    codetest generate        generate test code only (Git Working Tree changes)
    codetest test            run the test in <project>/src/test/test.txt, report
    codetest features        inspect the AST feature DB
"""
from __future__ import annotations

import sys

# Ensure Unicode (Korean/box-drawing) renders on legacy Windows consoles.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from pathlib import Path
from typing import Optional

import typer

from . import pipeline, test_txt, ui
from .config import Config
from .db import FeatureDB
from .git_analyzer import GitError

app = typer.Typer(
    add_completion=False,
    help="Code Test AI Agent — generate & run Spring Boot tests for changed code.",
    no_args_is_help=True,
)


def _cfg(project: Optional[str], llm: Optional[str]) -> Config:
    return Config.resolve(project, llm)


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


@app.command()
def run(
    stage: bool = typer.Option(False, "--stage", "-stage",
                               help="Target staged changes instead of the working tree."),
    project: Optional[str] = _ProjectOpt,
    llm: Optional[str] = _LlmOpt,
    show: Optional[str] = _ShowOpt,
    no_interactive: bool = _NoInteractiveOpt,
):
    """Generate tests for changed files, run them, and report."""
    cfg = _cfg(project, llm)
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
):
    """Generate test code only (no execution) from Git Working Tree changes."""
    cfg = _cfg(project, llm)
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
):
    """Run the test stored in <project>/src/test/test.txt and report."""
    cfg = _cfg(project, llm)
    report = test_txt.build_report_from_txt(cfg, "test")
    _finish(report, show, no_interactive)


@app.command()
def features(
    project: Optional[str] = _ProjectOpt,
):
    """List the AST-discovered features stored in the DB."""
    cfg = _cfg(project, None)
    db = FeatureDB(cfg.db_path)
    rows = db.all_features()
    if not rows:
        typer.echo("저장된 feature가 없습니다. 먼저 run/generate 를 실행하세요.")
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


if __name__ == "__main__":
    app()
