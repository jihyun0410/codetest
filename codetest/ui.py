"""Terminal UI (Rich) implementing the spec's [결과 예시] layout.

Each result shows the feature importance (High/Mid/Low) and two expandable
entries: <Test Code 보기> (the generated code + why it was written) and
<Test Result 보기> (execution outcome + validity judgment + rationale).
"""
from __future__ import annotations

from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .models import Report, ReportItem

console = Console()

_IMPORTANCE_STYLE = {"High": "bold white on red", "Mid": "bold black on yellow",
                     "Low": "bold white on grey37"}
_INTENT_KO = {"feature": "기능 추가", "condition": "조건 변경",
              "performance": "성능 개선", "modification": "일반 수정"}


def render_report(report: Report, show: Optional[str] = None) -> None:
    """Render the whole report. ``show`` = None|code|result|all."""
    console.print()
    console.rule(f"[bold]codetest {report.command}[/]  ·  {report.project_dir}")

    if report.notes:
        for n in report.notes:
            console.print(f"[yellow]•[/] {n}")
    if not report.items:
        console.print("[dim]표시할 결과가 없습니다.[/]")
        return

    _summary_table(report)

    for idx, item in enumerate(report.items, start=1):
        _result_example(idx, item)
        if show in ("code", "all"):
            show_test_code(item)
        if show in ("result", "all"):
            show_test_result(item)


def _summary_table(report: Report) -> None:
    table = Table(title="변경 요약 (중요도 내림차순)", title_style="bold",
                  show_lines=False, expand=False)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("변경 유닛")
    table.add_column("의도")
    table.add_column("중요도", justify="center")
    table.add_column("테스트 결과", justify="center")
    for idx, item in enumerate(report.items, start=1):
        r = item.result
        if r is None:
            res_cell = Text("생성만", style="dim")
        elif r.executor == "simulated":
            res_cell = Text("SIMULATED", style="magenta")
        else:
            res_cell = Text("PASS" if r.passed else "FAIL",
                            style="green" if r.passed else "red")
        table.add_row(
            str(idx),
            item.unit.display_name,
            _INTENT_KO.get(item.unit.intent, item.unit.intent),
            Text(f" {item.unit.importance} ",
                 style=_IMPORTANCE_STYLE.get(item.unit.importance, "")),
            res_cell,
        )
    console.print(table)


def _result_example(idx: int, item: ReportItem) -> None:
    imp = item.unit.importance
    body = Text()
    body.append("기능 중요도 : ", style="bold")
    body.append(f" {imp} \n\n", style=_IMPORTANCE_STYLE.get(imp, ""))
    body.append("  <Test Code 보기>", style="bold cyan")
    body.append("      (근거 = 코드/테스트 작성 이유)\n", style="dim")
    body.append("  <Test Result 보기>", style="bold cyan")
    body.append("    (결과값 · 정합성 판단 · 근거)", style="dim")
    console.print(
        Panel(body, title=f"[결과 예시 #{idx}]  {item.unit.display_name}",
              border_style="blue", expand=False)
    )


def show_test_code(item: ReportItem) -> None:
    """<Test Code 보기>: generation rationale + the generated source."""
    art = item.artifact
    r = art.reasoning
    meta = Text()
    meta.append("대상 파일 : ", style="bold"); meta.append(f"{item.unit.file_path}\n")
    meta.append("LLM 호출 : ", style="bold")
    meta.append(f"{art.llm_calls}회 (의도·중요도 분석 + 테스트 코드 동시 수신)\n")
    meta.append("변경 의도 : ", style="bold")
    meta.append(f"{_INTENT_KO.get(item.unit.intent, item.unit.intent)} — {item.unit.intent_reason}\n")
    meta.append("중요도 근거: ", style="bold")
    meta.append(f"{item.unit.importance_reason or '(근거 없음)'}\n")

    ctx = item.unit.context
    if ctx is not None:
        meta.append("\nAST MCP 컨텍스트(필터링된 전달 항목):\n", style="bold")
        meta.append(f"  • 시그니처  : {ctx.method_signature}\n")
        meta.append(f"  • 의존 Bean : {', '.join(ctx.dependency_beans) or '없음'}\n")
        meta.append(f"  • 호출 순서 : {ctx.call_flow or '없음'}\n")

    meta.append("\n사고 과정(chain-of-thought):\n", style="bold")
    for i, step in enumerate(r.steps, 1):
        meta.append(f"  {i}. {step}\n")
    meta.append("\n도출된 시나리오:\n", style="bold")
    for s in r.scenarios:
        meta.append(f"  • {s}\n")
    meta.append("\n작성 근거: ", style="bold"); meta.append(r.rationale + "\n")
    console.print(Panel(meta, title="[ <Test Code 보기> ] — 작성 근거",
                        border_style="cyan", expand=False))
    console.print(Panel(Syntax(art.source, "java", theme="ansi_dark",
                               line_numbers=True, word_wrap=False),
                        title=f"생성된 테스트 코드  ·  {art.file_path}",
                        border_style="cyan", expand=False))


def show_test_result(item: ReportItem) -> None:
    """<Test Result 보기>: outcome + validity judgment + rationale."""
    r = item.result
    if r is None:
        console.print(Panel("이 명령은 테스트를 실행하지 않았습니다 (generate 전용).",
                            title="[ <Test Result 보기> ]", border_style="magenta"))
        return

    body = Text()
    status = "PASS" if r.passed else "FAIL"
    if r.executor == "simulated":
        body.append("실행기 : ", style="bold")
        body.append("SIMULATED (Java/Gradle 미설치)\n", style="magenta")
    else:
        body.append("실행기 : ", style="bold"); body.append(f"{r.executor}\n")
        body.append("결과   : ", style="bold")
        body.append(f"{status}\n", style="green" if r.passed else "red")
    body.append("집계   : ", style="bold")
    body.append(f"total={r.total}, fail={r.failures}, error={r.errors}, "
                f"skip={r.skipped}, {r.duration_s}s\n")
    cov = f"{r.coverage_pct}%" if r.coverage_pct is not None else "N/A (JaCoCo)"
    body.append("커버리지: ", style="bold"); body.append(f"{cov}\n\n")

    v_style = {"valid": "green", "invalid": "red",
               "inconclusive": "yellow"}.get(r.validity, "white")
    body.append("정합성 판단 : ", style="bold")
    body.append(f"{r.validity}\n", style=v_style)
    body.append("판단 근거   : ", style="bold"); body.append(r.validity_reason + "\n")
    console.print(Panel(body, title="[ <Test Result 보기> ] — 결과 · 정합성",
                        border_style="magenta", expand=False))
    if r.log:
        console.print(Panel(Text(r.log, style="dim"), title="실행 로그",
                            border_style="grey37", expand=False))


def interactive(report: Report) -> None:
    """Interactive expand loop, per the spec's clickable entries."""
    if not report.items:
        return
    console.print("\n[bold]항목을 펼쳐보세요.[/] 예: [cyan]1c[/]=Test Code, "
                  "[cyan]1r[/]=Test Result, [cyan]a[/]=모두, [cyan]q[/]=종료")
    while True:
        try:
            choice = console.input("[bold green]codetest>[/] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if choice in ("q", "quit", "exit", ""):
            return
        if choice == "a":
            for item in report.items:
                show_test_code(item)
                show_test_result(item)
            continue
        m = choice[:-1]
        kind = choice[-1:]
        if m.isdigit() and 1 <= int(m) <= len(report.items) and kind in ("c", "r"):
            item = report.items[int(m) - 1]
            if kind == "c":
                show_test_code(item)
            else:
                show_test_result(item)
        else:
            console.print("[red]입력 형식: <번호>c / <번호>r / a / q[/]")
