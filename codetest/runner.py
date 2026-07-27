"""Stage 4 of the workflow: execute the generated @SpringBootTest.

If a Gradle wrapper (or ``gradle``) and a JDK are available, the real test
task is run with JaCoCo coverage. Otherwise the runner falls back to a
clearly-labelled *simulated* execution so the pipeline is demonstrable on
machines without a Java toolchain — the executor field always states which
path was taken, so results are never silently faked.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .config import Config
from .models import TestArtifact, TestResult


def _has_java() -> bool:
    return shutil.which("java") is not None


def _gradle_cmd(project_dir: Path) -> Optional[list[str]]:
    win = os.name == "nt"
    wrapper = project_dir / ("gradlew.bat" if win else "gradlew")
    if wrapper.exists():
        return [str(wrapper)]
    gradle = shutil.which("gradle")
    if gradle:
        return [gradle]
    return None


def can_run_real(project_dir: Path) -> bool:
    return _has_java() and _gradle_cmd(project_dir) is not None


# --------------------------------------------------------------------------- #
def run_tests(cfg: Config, artifact: TestArtifact) -> TestResult:
    if can_run_real(cfg.project_dir):
        return _run_gradle(cfg, artifact)
    return _simulate(cfg, artifact)


def _run_gradle(cfg: Config, artifact: TestArtifact) -> TestResult:
    cmd = _gradle_cmd(cfg.project_dir)
    fqcn = f"{artifact.package}.{artifact.class_name}"
    args = [*cmd, "test", "--tests", fqcn, "jacocoTestReport", "--no-daemon"]
    start = time.time()
    proc = subprocess.run(
        args, cwd=str(cfg.project_dir), capture_output=True, text=True
    )
    duration = time.time() - start
    log = (proc.stdout + "\n" + proc.stderr).strip()

    total, failures, errors, skipped = _parse_junit(cfg.project_dir)
    coverage = _parse_jacoco(cfg.project_dir)
    passed = proc.returncode == 0 and failures == 0 and errors == 0

    result = TestResult(
        passed=passed, total=total, failures=failures, errors=errors,
        skipped=skipped, duration_s=round(duration, 2), coverage_pct=coverage,
        executor="gradle", log=log,
    )
    _judge_validity(result, artifact)
    return result


def _parse_junit(project_dir: Path) -> tuple[int, int, int, int]:
    total = failures = errors = skipped = 0
    pattern = str(project_dir / "build" / "test-results" / "test" / "*.xml")
    for xml_file in glob.glob(pattern):
        try:
            root = ET.parse(xml_file).getroot()
        except ET.ParseError:
            continue
        total += int(root.get("tests", 0))
        failures += int(root.get("failures", 0))
        errors += int(root.get("errors", 0))
        skipped += int(root.get("skipped", 0))
    return total, failures, errors, skipped


def _parse_jacoco(project_dir: Path) -> Optional[float]:
    xml_path = project_dir / "build" / "reports" / "jacoco" / "test" / "jacocoTestReport.xml"
    if not xml_path.exists():
        return None
    try:
        root = ET.parse(str(xml_path)).getroot()
    except ET.ParseError:
        return None
    for counter in root.findall("counter"):
        if counter.get("type") == "INSTRUCTION":
            covered = int(counter.get("covered", 0))
            missed = int(counter.get("missed", 0))
            denom = covered + missed
            return round(100.0 * covered / denom, 1) if denom else None
    return None


def _simulate(cfg: Config, artifact: TestArtifact) -> TestResult:
    # Count @Test methods so the simulated totals reflect the generated file.
    test_count = len(re.findall(r"@Test\b", artifact.source))
    result = TestResult(
        passed=True,
        total=test_count,
        failures=0,
        errors=0,
        skipped=0,
        duration_s=0.0,
        coverage_pct=None,
        executor="simulated",
        log=(
            "Java/Gradle toolchain not found on this machine — executed in "
            "SIMULATED mode. The generated @SpringBootTest was NOT compiled or "
            "run. Install a JDK 17+ and use the Gradle wrapper to run for real:\n"
            f"    cd {cfg.project_dir}\n"
            f"    ./gradlew test --tests {artifact.package}.{artifact.class_name} "
            "jacocoTestReport"
        ),
    )
    _judge_validity(result, artifact)
    return result


def _judge_validity(result: TestResult, artifact: TestArtifact) -> None:
    """Populate the validity judgment surfaced under <Test Result 보기>."""
    n_scen = len(artifact.reasoning.scenarios)
    if result.executor == "simulated":
        result.validity = "inconclusive"
        result.validity_reason = (
            f"생성된 테스트는 {result.total}개의 @Test와 {n_scen}개의 시나리오를 "
            "포함하지만, 로컬에 Java/Gradle이 없어 실제 실행으로 검증되지 않음(시뮬레이션)."
        )
    elif result.passed:
        result.validity = "valid"
        result.validity_reason = (
            f"{result.total}개 테스트가 모두 통과했고, 변경 의도별 성공/실패 시나리오"
            f"({n_scen}개)를 커버함. 커버리지="
            f"{result.coverage_pct if result.coverage_pct is not None else 'N/A'}%."
        )
    else:
        result.validity = "invalid"
        result.validity_reason = (
            f"실패 {result.failures}건 / 오류 {result.errors}건 발생 — 변경 코드가 "
            "기대 동작을 만족하지 못하거나 테스트가 회귀를 감지함. 로그 확인 필요."
        )
