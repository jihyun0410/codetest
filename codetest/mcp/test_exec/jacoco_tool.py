"""JaCoCo Tool — jacocoTestReport.xml / JUnit XML 파싱.

Extracts the numbers the report needs: pass/fail counts, instruction coverage
and **branch coverage** (the metric that matters most for a condition change),
plus the failure messages worth surfacing in the log panel.
"""
from __future__ import annotations

import glob
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional


def parse_junit(project_dir: Path) -> Dict[str, int]:
    """Aggregate JUnit XML results under build/test-results/test."""
    totals = {"total": 0, "failures": 0, "errors": 0, "skipped": 0}
    pattern = str(project_dir / "build" / "test-results" / "test" / "*.xml")
    for xml_file in glob.glob(pattern):
        try:
            root = ET.parse(xml_file).getroot()
        except ET.ParseError:
            continue
        totals["total"] += int(root.get("tests", 0))
        totals["failures"] += int(root.get("failures", 0))
        totals["errors"] += int(root.get("errors", 0))
        totals["skipped"] += int(root.get("skipped", 0))
    return totals


def failure_messages(project_dir: Path, limit: int = 10) -> List[str]:
    """Pull failure/error messages so the report can explain an invalid result."""
    out: List[str] = []
    pattern = str(project_dir / "build" / "test-results" / "test" / "*.xml")
    for xml_file in glob.glob(pattern):
        try:
            root = ET.parse(xml_file).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):
            for bad in list(case.findall("failure")) + list(case.findall("error")):
                name = f"{case.get('classname', '')}.{case.get('name', '')}".strip(".")
                out.append(f"{name}: {bad.get('message', '').strip()}")
                if len(out) >= limit:
                    return out
    return out


def _counter_pct(root: ET.Element, counter_type: str) -> Optional[float]:
    for counter in root.findall("counter"):
        if counter.get("type") == counter_type:
            covered = int(counter.get("covered", 0))
            missed = int(counter.get("missed", 0))
            denom = covered + missed
            return round(100.0 * covered / denom, 1) if denom else None
    return None


def parse_jacoco(project_dir: Path) -> Dict[str, Optional[float]]:
    """Return instruction + branch coverage percentages (None when unavailable)."""
    xml_path = (project_dir / "build" / "reports" / "jacoco" / "test"
                / "jacocoTestReport.xml")
    empty = {"coverage_pct": None, "branch_coverage_pct": None}
    if not xml_path.exists():
        return empty
    try:
        root = ET.parse(str(xml_path)).getroot()
    except ET.ParseError:
        return empty
    return {
        "coverage_pct": _counter_pct(root, "INSTRUCTION"),
        "branch_coverage_pct": _counter_pct(root, "BRANCH"),
    }


# --------------------------------------------------------------------------- #
# MCP tool
# --------------------------------------------------------------------------- #

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "project_dir": {"type": "string"},
        "include_failures": {"type": "boolean", "description": "실패 메시지 포함 (기본 true)"},
    },
    "required": ["project_dir"],
}


def tool_coverage_report(args: dict) -> dict:
    project_dir = Path(args["project_dir"])
    payload: Dict[str, object] = {**parse_junit(project_dir), **parse_jacoco(project_dir)}
    if args.get("include_failures", True):
        payload["failures_detail"] = failure_messages(project_dir)
    return payload


def register(registry) -> None:
    registry.register(
        "coverage_report",
        "JUnit XML과 jacocoTestReport.xml을 파싱해 집계(통과/실패/스킵), 명령어·분기 "
        "커버리지, 실패 메시지를 반환합니다.",
        REPORT_SCHEMA, tool_coverage_report,
    )
