"""Runtime configuration for the Code Test agent."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    """Resolved configuration for a single agent invocation.

    Attributes:
        project_dir: Root of the target Spring Boot project to analyze/test.
        db_path: SQLite file where discovered project features are stored.
        test_source_dir: Where generated JUnit test files are written.
        test_txt_path: Location of the plain-text test used by `codetest test`.
        llm_backend: Which LLM implementation to use ("mock" | "claude").
    """

    project_dir: Path
    db_path: Path
    test_source_dir: Path
    test_txt_path: Path
    llm_backend: str = "mock"

    # Package used for generated tests when the source package is unknown.
    default_test_package: str = "com.example.demo"

    @staticmethod
    def resolve(project_dir: str | os.PathLike | None = None,
                llm_backend: str | None = None) -> "Config":
        root = Path(project_dir or os.getcwd()).resolve()
        agent_dir = root / ".codetest"
        return Config(
            project_dir=root,
            db_path=agent_dir / "features.db",
            test_source_dir=root / "src" / "test" / "java",
            test_txt_path=root / "src" / "test" / "test.txt",
            llm_backend=(llm_backend or os.environ.get("CODETEST_LLM", "mock")),
        )

    def ensure_dirs(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.test_source_dir.mkdir(parents=True, exist_ok=True)


# Ordering used across the UI/report. Higher = more important.
IMPORTANCE_ORDER = {"High": 3, "Mid": 2, "Low": 1}
