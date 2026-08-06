"""Agent REST 요청/응답 스키마 (Local Client 의 api_client.py 와 1:1 대응)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SourceFilePayload(BaseModel):
    path: str
    content: str


# ---------------------------------------------------------------------------
#  POST /projects — MCP 로 위임 (개요 수집은 코드 기반 작업)
# ---------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200, description="프로젝트 명")
    git_url: str = Field(..., description="대상 저장소 Git URL")
    owner: str = Field(..., min_length=1, max_length=100, description="담당자")
    github_token: str | None = Field(default=None, description="Github API Token")
    default_branch: str = Field(default="main", description="기준 브랜치")


class ProjectRead(BaseModel):
    """MCP 응답을 그대로 전달한다."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    git_url: str
    owner: str
    default_branch: str
    ingest_status: str
    ingest_error: str | None = None
    last_indexed_at: datetime | None = None
    frameworks: list[str] = Field(default_factory=list)
    language_stats: dict = Field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    has_github_token: bool = False


# ---------------------------------------------------------------------------
#  Test Code 생성 — 정의서 (2) 의도 파악 + (3) 생성 + [상세] 2·3 CoT
# ---------------------------------------------------------------------------
class GenerateRequest(BaseModel):
    project_id: str
    #: 변경분 unified diff
    diff: str
    #: 변경 파일 본문 (테스트 대상 코드)
    sources: list[SourceFilePayload] = Field(default_factory=list)
    #: staged / unstaged / worktree — 어떤 범위인지 표기용
    scope: str = "worktree"


class GenerateResponse(BaseModel):
    #: [상세 2] 사고의 사슬 — 생각하는 과정 (Test Code 작성 근거의 일부)
    thinking: str = ""
    #: (2) 파악한 변경 의도 — 기능 추가 / 조건 변경 / 성능 개선 …
    intent: str = ""
    intent_rationale: str = ""
    #: [UI 4] 기능 중요도 — HIGH / MID / LOW
    importance: str = "LOW"
    importance_rationale: str = ""
    #: (3) 정상 케이스 / 실패 케이스 판단 결과
    test_cases: str = ""
    #: @SpringBootTest 테스트 코드
    test_code: str = ""
    rationale: str = ""
    #: 테스트 대상 코드 (TUI "Test Code 보기")
    target_code: str = ""
    #: MCP 가 추론한 기준 패키지 — 실행 시 그대로 넘긴다
    base_package: str | None = None
    #: MCP 개요 수집 완료 여부 / 경고
    graph_ready: bool = True
    analysis_warnings: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
#  테스트 실행 + 판정 — 정의서 (1) 실행, [상세 4] JaCoCo, [UI 3] 적절성
# ---------------------------------------------------------------------------
class ExecuteRequest(BaseModel):
    """`codetest test` — src/test/test.txt 의 Test Code 를 실행한다."""

    project_id: str
    test_code: str
    #: 실행 전 작업 사본에 덮어쓸 변경 파일
    sources: list[SourceFilePayload] = Field(default_factory=list)
    base_package: str | None = None
    #: 이전에 생성했을 때 파악한 의도 (결과값에 함께 표시하기 위함)
    intent: str = ""
    intent_rationale: str = ""


class ReportResponse(BaseModel):
    #: PASS / FAIL (gradle exit code 가 사실)
    result: str = "FAIL"
    #: [UI 3] 결과가 적절한지 (적절 / 부적절)
    verdict: str = ""
    verdict_rationale: str = ""
    details: str = ""

    #: (2) "파악한 의도와 근거를 <Test Result 보기>의 결과값에 넣는다"
    intent: str = ""
    intent_rationale: str = ""

    # --- MCP 가 확정한 실행 사실 ---
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    total: int = 0
    failures: list[str] = Field(default_factory=list)
    #: [상세 4] JaCoCo 커버리지
    coverage: dict | None = None
    jacoco_enabled: bool = False
    #: (1) "생성된 Test Code를 @SpringBootTest 에 넣고 실행"
    springboot_applied: bool = False
    applied: list[str] = Field(default_factory=list)
    test_file_path: str = ""
    exit_code: int = 0
    output: str = ""


class RunRequest(GenerateRequest):
    """`codetest run` / `run --stage` — 생성 + 실행 + 판정을 한 번에."""


class RunResponse(BaseModel):
    """생성 결과와 판정 결과를 함께 돌려준다."""

    generated: GenerateResponse
    report: ReportResponse
