"""Stage 3 of the workflow: generate @SpringBootTest test code.

Per the spec, when several files change they are bundled into a single
business-flow test. This module groups the change units, drives the LLM
layer (reason → generate), and writes the resulting Java test file into the
project's test source tree.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List

from .config import Config
from .llm import LLMClient, TestGenRequest
from .models import ChangeUnit, TestArtifact


def _primary_target(units: List[ChangeUnit]) -> str:
    for u in units:
        if "service" in u.file_path.lower():
            return u.class_name
    return units[0].class_name


def generate_test(
    cfg: Config,
    llm: LLMClient,
    units: List[ChangeUnit],
    diffs_by_file: Dict[str, str],
    package: str,
    feature_summary: str = "",
    write: bool = True,
) -> TestArtifact:
    """Reason about the bundled change and generate one test class."""
    req = TestGenRequest(
        units=units,
        project_package=package,
        diffs_by_file=diffs_by_file,
        feature_summary=feature_summary,
    )
    reasoning = llm.reason(req)
    source = llm.generate_test(req, reasoning)

    target = _primary_target(units)
    test_class = f"{target}GeneratedTest"
    pkg_dir = cfg.test_source_dir / Path(*package.split("."))
    file_path = pkg_dir / f"{test_class}.java"

    if write:
        pkg_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_text(source, encoding="utf-8")

    return TestArtifact(
        class_name=test_class,
        package=package,
        file_path=str(file_path),
        source=source,
        reasoning=reasoning,
        covered_units=units,
    )
