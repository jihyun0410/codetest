"""Stage 3 of the workflow: analyze intent *and* generate the test — in one call.

Per the spec, when several files change they are bundled into a single
business-flow test. This module groups the change units, makes **one** LLM
request that returns both [의도/중요도 분석 근거] and [테스트 코드], applies the
analysis back onto the units, and writes the Java test file into the project's
test source tree.

The request payload is deliberately small: the AST MCP server has already
filtered each target down to its signature, dependency bean names and call
order, so no full source or full diff is sent.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence

from . import intent
from .config import Config
from .llm import LLMClient, TestGenRequest
from .models import ChangeUnit, MethodContext, TestArtifact


def _primary_target(units: List[ChangeUnit]) -> str:
    for u in units:
        if "service" in u.file_path.lower():
            return u.class_name
    return units[0].class_name


def generate_test(
    cfg: Config,
    llm: LLMClient,
    units: List[ChangeUnit],
    package: str,
    contexts: Optional[Sequence[MethodContext]] = None,
    feature_summary: str = "",
    write: bool = True,
) -> TestArtifact:
    """Analyze + generate one test class for the bundle with a single API call."""
    if contexts is None:
        contexts = [u.context for u in units if u.context]

    req = TestGenRequest(
        units=units,
        project_package=package,
        contexts=list(contexts),
        feature_summary=feature_summary,
    )

    # ── the one and only LLM round trip ──────────────────────────────────────
    combined = llm.analyze_and_generate(req)

    # The analysis half of that response labels the units for the report.
    analyses = intent.apply_analyses(units, combined.analyses)

    target = _primary_target(units)
    test_class = f"{target}GeneratedTest"
    pkg_dir = cfg.test_source_dir / Path(*package.split("."))
    file_path = pkg_dir / f"{test_class}.java"

    if write:
        pkg_dir.mkdir(parents=True, exist_ok=True)
        file_path.write_text(combined.test_source, encoding="utf-8")

    return TestArtifact(
        class_name=test_class,
        package=package,
        file_path=str(file_path),
        source=combined.test_source,
        reasoning=combined.reasoning,
        covered_units=units,
        analyses=analyses,
        contexts=list(contexts),
        llm_calls=combined.llm_calls,
    )
