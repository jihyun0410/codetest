"""Runtime configuration for the Code Test agent."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .git_analyzer import DiffOptions


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    """Resolved configuration for a single agent invocation.

    Attributes:
        project_dir: Root of the target Spring Boot project to analyze/test.
        db_path: SQLite file used only when ``persist`` is enabled.
        test_source_dir: Where generated JUnit test files are written.
        test_txt_path: Location of the plain-text test used by `codetest test`.
        llm_backend: Which LLM implementation to use ("mock" | "claude").
        persist: False (default) → the session pipeline stays in memory and
            never touches SQLite. True → also mirror features/runs into the DB.
        ignore_whitespace: Ignore whitespace-only diff noise.
        ignore_blank_lines: Ignore meaningless blank-line additions/removals.
        ast_mcp_transport: How to reach the AST MCP server ("inprocess"|"stdio").
    """

    project_dir: Path
    db_path: Path
    test_source_dir: Path
    test_txt_path: Path
    llm_backend: str = "mock"
    persist: bool = False
    ignore_whitespace: bool = True
    ignore_blank_lines: bool = True
    ast_mcp_transport: str = "inprocess"

    # Package used for generated tests when the source package is unknown.
    default_test_package: str = "com.example.demo"

    @staticmethod
    def resolve(project_dir: str | os.PathLike | None = None,
                llm_backend: str | None = None,
                persist: Optional[bool] = None,
                ignore_whitespace: Optional[bool] = None,
                ignore_blank_lines: Optional[bool] = None,
                ast_mcp_transport: str | None = None) -> "Config":
        root = Path(project_dir or os.getcwd()).resolve()
        agent_dir = root / ".codetest"
        return Config(
            project_dir=root,
            db_path=agent_dir / "features.db",
            test_source_dir=root / "src" / "test" / "java",
            test_txt_path=root / "src" / "test" / "test.txt",
            llm_backend=(llm_backend or os.environ.get("CODETEST_LLM", "mock")),
            persist=(_env_flag("CODETEST_PERSIST", False)
                     if persist is None else persist),
            ignore_whitespace=(_env_flag("CODETEST_IGNORE_WHITESPACE", True)
                               if ignore_whitespace is None else ignore_whitespace),
            ignore_blank_lines=(_env_flag("CODETEST_IGNORE_BLANK_LINES", True)
                                if ignore_blank_lines is None else ignore_blank_lines),
            ast_mcp_transport=(ast_mcp_transport
                               or os.environ.get("CODETEST_AST_MCP", "inprocess")),
        )

    def diff_options(self) -> DiffOptions:
        return DiffOptions(
            ignore_whitespace=self.ignore_whitespace,
            ignore_blank_lines=self.ignore_blank_lines,
        )

    def ensure_dirs(self) -> None:
        # The DB directory is only created when persistence was requested;
        # a normal session leaves no files behind but the generated test.
        if self.persist:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.test_source_dir.mkdir(parents=True, exist_ok=True)


# Ordering used across the UI/report. Higher = more important.
IMPORTANCE_ORDER = {"High": 3, "Mid": 2, "Low": 1}
