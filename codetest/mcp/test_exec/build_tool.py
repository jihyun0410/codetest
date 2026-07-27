"""Build Tool — ``./gradlew test`` 1회 실행 트리거.

If a Gradle wrapper (or ``gradle``) and a JDK are available, the real test task
runs with JaCoCo. Otherwise execution falls back to a clearly-labelled
*simulated* run so the pipeline is demonstrable on machines without a Java
toolchain — the ``executor`` field always states which path was taken, so a
result is never silently faked.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional


def has_java() -> bool:
    return shutil.which("java") is not None


def gradle_cmd(project_dir: Path) -> Optional[List[str]]:
    wrapper = project_dir / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if wrapper.exists():
        return [str(wrapper)]
    gradle = shutil.which("gradle")
    return [gradle] if gradle else None


def can_run_real(project_dir: Path) -> bool:
    return has_java() and gradle_cmd(project_dir) is not None


def run_gradle_test(project_dir: Path, fqcn: str) -> dict:
    """Run the generated test class once, together with the JaCoCo report task."""
    cmd = gradle_cmd(project_dir)
    if cmd is None:
        raise RuntimeError("gradle not available")
    args = [*cmd, "test", "--tests", fqcn, "jacocoTestReport", "--no-daemon"]

    start = time.time()
    proc = subprocess.run(args, cwd=str(project_dir), capture_output=True, text=True)
    duration = time.time() - start

    return {
        "executor": "gradle",
        "return_code": proc.returncode,
        "duration_s": round(duration, 2),
        "log": (proc.stdout + "\n" + proc.stderr).strip(),
        "command": " ".join(args),
    }


def simulate(project_dir: Path, fqcn: str, source: str) -> dict:
    """Count @Test methods so simulated totals reflect the generated file."""
    return {
        "executor": "simulated",
        "return_code": 0,
        "duration_s": 0.0,
        "test_count": len(re.findall(r"@Test\b", source)),
        "log": (
            "Java/Gradle toolchain not found on this machine — executed in "
            "SIMULATED mode. The generated @SpringBootTest was NOT compiled or "
            "run. Install a JDK 17+ and use the Gradle wrapper to run for real:\n"
            f"    cd {project_dir}\n"
            f"    ./gradlew test --tests {fqcn} jacocoTestReport"
        ),
        "command": f"./gradlew test --tests {fqcn} jacocoTestReport",
    }


# --------------------------------------------------------------------------- #
# MCP tool
# --------------------------------------------------------------------------- #

RUN_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string"},
        "fqcn": {"type": "string", "description": "예: com.example.demo.OrderServiceGeneratedTest"},
        "source": {"type": "string", "description": "시뮬레이션 시 @Test 개수 산출용"},
        "allow_simulation": {"type": "boolean", "description": "기본 true"},
    },
    "required": ["project_dir", "fqcn"],
}


def tool_run_tests(args: dict) -> dict:
    project_dir = Path(args["project_dir"])
    fqcn = args["fqcn"]
    if can_run_real(project_dir):
        return run_gradle_test(project_dir, fqcn)
    if args.get("allow_simulation", True):
        return simulate(project_dir, fqcn, args.get("source", ""))
    raise RuntimeError("no Java/Gradle toolchain and simulation is disabled")


def register(registry) -> None:
    registry.register(
        "test_run",
        "생성된 @SpringBootTest 클래스를 Gradle로 1회 실행합니다. 툴체인이 없으면 "
        "SIMULATED 결과를 반환합니다.",
        RUN_SCHEMA, tool_run_tests,
    )
