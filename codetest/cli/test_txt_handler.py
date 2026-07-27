"""``codetest test`` 명령 처리 모듈.

Thin by design: it validates the CLI-facing preconditions and delegates the
actual workflow to the agent. The Java parsing/writing it used to do now lives
in the File MCP tool, so this module never touches the filesystem itself —
that is what keeps the dependency direction cli → agent → mcp.
"""
from __future__ import annotations

from ..agent import pipeline
from ..config import Config
from ..models import Report


def handle(cfg: Config, command: str = "test") -> Report:
    """Run the user-provided test in ``<project>/src/test/test.txt``."""
    return pipeline.build_report_from_txt(cfg, command)


def describe_source(cfg: Config) -> str:
    """One-line description of where the provided test comes from."""
    return str(cfg.test_txt_path)
