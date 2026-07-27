"""`codetest test` support: run a hand-written test from src/test/test.txt.

Reads the Java test source stored at ``<project>/src/test/test.txt``, ensures
it is an ``@SpringBootTest`` class, writes it into the test source tree and
executes it, producing a one-item report.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import Config
from .db import FeatureDB
from .models import (ChangeUnit, MethodInfo, ReasoningTrace, Report, ReportItem,
                     TestArtifact)
from .runner import run_tests


def _extract(pattern: str, text: str, default: str) -> str:
    m = re.search(pattern, text)
    return m.group(1) if m else default


def build_report_from_txt(cfg: Config, command: str) -> Report:
    cfg.ensure_dirs()
    report = Report(command=command, project_dir=str(cfg.project_dir))

    if not cfg.test_txt_path.exists():
        report.notes.append(f"테스트 파일이 없습니다: {cfg.test_txt_path}")
        return report

    source = cfg.test_txt_path.read_text(encoding="utf-8", errors="replace")
    package = _extract(r"package\s+([\w.]+)\s*;", source, cfg.default_test_package)
    class_name = _extract(r"\b(?:class|interface)\s+(\w+)", source, "ProvidedTest")

    # Ensure @SpringBootTest is present (spec: 생성/제공된 테스트는 @SpringBootTest로 실행).
    if "@SpringBootTest" not in source:
        source = _inject_springboot_test(source)

    pkg_dir = cfg.test_source_dir / Path(*package.split("."))
    pkg_dir.mkdir(parents=True, exist_ok=True)
    file_path = pkg_dir / f"{class_name}.java"
    file_path.write_text(source, encoding="utf-8")

    reasoning = ReasoningTrace(
        steps=["사용자가 제공한 src/test/test.txt의 테스트 코드를 로드",
               "@SpringBootTest 보장 후 테스트 소스 트리에 배치",
               "JaCoCo와 함께 테스트 실행"],
        scenarios=[f"{class_name} 내부에 정의된 사용자 작성 시나리오"],
        rationale="사용자가 직접 작성/검토한 테스트 코드를 그대로 실행하여 검증함.",
    )
    unit = ChangeUnit(
        file_path="src/test/test.txt",
        class_name=class_name,
        method=MethodInfo(name="(provided)", signature="(provided)",
                          start_line=0, end_line=0),
        changed_lines=[], added_lines=[], removed_lines=[],
        intent="modification",
        intent_reason="사용자 제공 테스트(test.txt)",
        importance="Mid",
    )
    artifact = TestArtifact(
        class_name=class_name, package=package, file_path=str(file_path),
        source=source, reasoning=reasoning, covered_units=[unit],
    )
    result = run_tests(cfg, artifact)
    report.items.append(ReportItem(unit=unit, artifact=artifact, result=result))
    FeatureDB(cfg.db_path).record_run(command, f"ran provided test {class_name}")
    return report


def _inject_springboot_test(source: str) -> str:
    lines = source.splitlines()
    out = []
    injected = False
    import_added = False
    for line in lines:
        if not import_added and line.startswith("package "):
            out.append(line)
            out.append("import org.springframework.boot.test.context.SpringBootTest;")
            import_added = True
            continue
        if not injected and re.match(r"\s*(public\s+)?(class|interface)\s+\w+", line):
            out.append("@SpringBootTest")
            injected = True
        out.append(line)
    if not injected:
        out.insert(0, "@SpringBootTest")
    return "\n".join(out) + "\n"
