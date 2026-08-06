"""REST 요청/응답 스키마 (api_client.py 와 1:1 대응)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
#  POST /projects
# ---------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="프로젝트 명")
    git_url: str = Field(..., description="대상 저장소 Git URL")
    owner: str = Field(..., min_length=1, max_length=100, description="담당자")
    github_token: str | None = Field(default=None, description="Github API Token")
    default_branch: str = Field(default="main", description="기준 브랜치")

    @field_validator("git_url")
    @classmethod
    def _validate_git_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://", "git@")):
            raise ValueError("git_url 은 http(s):// 또는 git@ 형식이어야 합니다.")
        return v.rstrip("/")


class ProjectRead(BaseModel):
    """github_token 은 보유 여부만 노출한다."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    git_url: str
    owner: str
    default_branch: str
    ingest_status: str
    ingest_error: str | None
    last_indexed_at: datetime | None
    frameworks: list[str]
    language_stats: dict
    created_at: datetime
    updated_at: datetime
    has_github_token: bool = False


# ---------------------------------------------------------------------------
#  POST /tests/generate
# ---------------------------------------------------------------------------
class SourceFilePayload(BaseModel):
    path: str
    content: str


class GenerateRequest(BaseModel):
    project_id: str
    #: 변경분 unified diff
    diff: str
    #: 변경 파일 본문 (테스트 대상 코드)
    sources: list[SourceFilePayload] = Field(default_factory=list)
    #: staged / unstaged / worktree — 어떤 범위인지 표기용
    scope: str = "worktree"


class GenerateResponse(BaseModel):
    #: 기능 중요도 — HIGH / MID / LOW
    importance: str = "LOW"
    importance_rationale: str = ""
    language: str = "python"
    #: 실행용 임시 파일에 쓸 확장자
    file_extension: str = ".py"
    #: "{file}" 이 실제 경로로 치환된다
    run_command: list[str] = Field(
        default_factory=lambda: ["python", "-m", "pytest", "-q", "{file}"]
    )
    #: 테스트 대상 코드 (TUI "보기")
    target_code: str = ""
    test_code: str = ""
    rationale: str = ""


# ---------------------------------------------------------------------------
#  POST /tests/report
# ---------------------------------------------------------------------------
class ReportRequest(BaseModel):
    project_id: str
    test_code: str
    #: 테스트 실행 표준출력 + 표준에러
    output: str = ""
    exit_code: int = 0
    language: str = "python"


class ReportResponse(BaseModel):
    #: PASS / FAIL
    result: str = "FAIL"
    #: 결과가 적절한지 (적절 / 부적절)
    verdict: str = ""
    verdict_rationale: str = ""
    details: str = ""
