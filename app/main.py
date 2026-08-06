"""Agent Server 진입점 + REST API.

정의서:
  "**LLM을 사용하여 판단하는 부분은 Agent**, 코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현"

이 서버는 LLM 판단만 수행하고, 코드 기반 작업(Git clone·AST·개요 저장·
@SpringBootTest 주입·JaCoCo 실행)은 전부 MCP 서비스에 FastAPI 로 위임한다.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

Local Client 가 호출하는 엔드포인트:

  GET    /api/v1/health            연결 확인 (인증 불필요)
  POST   /api/v1/projects          프로젝트 등록      → MCP 위임
  DELETE /api/v1/projects/{id}     프로젝트 삭제      → MCP 위임
  POST   /api/v1/tests/generate    codetest generate  (생성만)
  POST   /api/v1/tests/run         codetest run       (생성 + 실행 + 판정)
  POST   /api/v1/tests/execute     codetest test      (실행 + 판정)
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app import testgen
from app.config import get_logger, settings, setup_logging, verify_api_key
from app.llm import LLMRefusalError, LLMUnavailableError
from app.mcp_client import McpError, mcp_client
from app.schemas import (
    ExecuteRequest,
    GenerateRequest,
    GenerateResponse,
    ProjectCreate,
    ProjectRead,
    ReportResponse,
    RunRequest,
    RunResponse,
)

setup_logging()
logger = get_logger(__name__)


# --- 의존성 / 헬퍼 -----------------------------------------------------------
def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
    """CODETEST_API_KEYS 가 비어 있으면 인증 비활성화(로컬 개발용)."""
    if not verify_api_key(x_api_key):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "유효하지 않은 API Key 입니다. X-API-Key 헤더를 확인하세요.",
        )


@contextmanager
def _llm_errors():
    """LLM 예외를 클라이언트가 이해할 HTTP 상태로 옮긴다."""
    try:
        yield
    except LLMUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except LLMRefusalError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


@contextmanager
def _mcp_errors():
    """MCP 오류를 그대로 전달한다 — 어느 쪽이 실패했는지 클라이언트가 알아야 한다."""
    try:
        yield
    except McpError as exc:
        # 404(프로젝트 없음) 등 클라이언트 잘못은 그대로, 나머지는 502 로 올린다.
        code = exc.status_code if exc.status_code and exc.status_code < 500 else 502
        raise HTTPException(code, str(exc)) from None


def _payload_sources(items) -> list[dict]:
    return [{"path": item.path, "content": item.content} for item in items]


def _analyze(payload: GenerateRequest) -> dict:
    """MCP 에 Git Diff + AST 변경 단위 식별을 요청하고 diff 를 함께 실어 둔다."""
    with _mcp_errors():
        analysis = mcp_client.analyze_changes(
            payload.project_id, payload.diff, _payload_sources(payload.sources)
        )
    analysis["diff"] = payload.diff
    return analysis


def _generate(payload: GenerateRequest, analysis: dict) -> GenerateResponse:
    with _mcp_errors():
        overview = mcp_client.overview(payload.project_id)
    with _llm_errors():
        return testgen.generate(
            analysis,
            sources=[(item.path, item.content) for item in payload.sources],
            scope=payload.scope,
            project_name=overview.get("name", payload.project_id),
        )


def _execute_and_report(
    project_id: str,
    test_code: str,
    sources: list[dict],
    base_package: str | None,
    intent: str,
    intent_rationale: str,
) -> ReportResponse:
    """MCP 로 @SpringBootTest 를 실행하고 LLM 으로 적절성을 판단한다."""
    if not test_code.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "실행할 Test Code 가 비어 있습니다."
        )

    with _mcp_errors():
        execution = mcp_client.execute_tests(project_id, test_code, sources, base_package)
    with _llm_errors():
        return testgen.report(execution, test_code, intent, intent_rationale)


# --- 라우터 ------------------------------------------------------------------
router = APIRouter(prefix="/api/v1")


@router.get("/health", tags=["health"], summary="헬스체크")
def health() -> dict:
    """연결 확인용 (인증 불필요). MCP 연결 상태도 함께 알린다."""
    mcp_status = "ok"
    try:
        mcp_client.health()
    except McpError as exc:
        mcp_status = f"unreachable: {exc}"
    return {
        "status": "ok",
        "app": settings.app_name,
        "role": "llm-based",
        "mcp": {"base_url": settings.mcp_base_url, "status": mcp_status},
    }


# --- 프로젝트 (MCP 위임) ------------------------------------------------------
projects = APIRouter(
    prefix="/projects", tags=["projects"], dependencies=[Depends(require_api_key)]
)


@projects.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED,
               summary="프로젝트 등록")
def create_project(payload: ProjectCreate) -> ProjectRead:
    """개요 수집(clone + AST + DB 저장)은 코드 기반 작업이므로 MCP 가 수행한다."""
    with _mcp_errors():
        created = mcp_client.create_project(**payload.model_dump())
    return ProjectRead.model_validate(created)


@projects.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT,
                 summary="프로젝트 삭제")
def delete_project(project_id: str) -> None:
    with _mcp_errors():
        mcp_client.delete_project(project_id)


# --- Test Code ---------------------------------------------------------------
tests = APIRouter(
    prefix="/tests", tags=["tests"], dependencies=[Depends(require_api_key)]
)


@tests.post("/generate", response_model=GenerateResponse,
            summary="Test Code 생성 (codetest generate)")
def generate_tests(payload: GenerateRequest) -> GenerateResponse:
    """
    Git Working Tree 변경분에 대해 의도를 파악하고 @SpringBootTest 를 생성한다.
    실행은 하지 않는다.
    """
    return _generate(payload, _analyze(payload))


@tests.post("/run", response_model=RunResponse,
            summary="생성 + 실행 + 판정 (codetest run)")
def run_tests(payload: RunRequest) -> RunResponse:
    """정의서 흐름 3~5 를 한 번에 수행한다 (분석 → 생성 → 실행 → 판정)."""
    analysis = _analyze(payload)
    generated = _generate(payload, analysis)

    report = _execute_and_report(
        payload.project_id,
        generated.test_code,
        _payload_sources(payload.sources),
        generated.base_package,
        generated.intent,
        generated.intent_rationale,
    )
    return RunResponse(generated=generated, report=report)


@tests.post("/execute", response_model=ReportResponse,
            summary="주어진 Test Code 실행 + 판정 (codetest test)")
def execute_tests(payload: ExecuteRequest) -> ReportResponse:
    """`/src/test/test.txt` 의 Test Code 를 MCP 로 실행하고 적절성을 판단한다."""
    return _execute_and_report(
        payload.project_id,
        payload.test_code,
        _payload_sources(payload.sources),
        payload.base_package,
        payload.intent,
        payload.intent_rationale,
    )


router.include_router(projects)
router.include_router(tests)


# --- 앱 ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "%s 기동 (model=%s, mcp=%s)",
        settings.app_name, settings.llm_model, settings.mcp_base_url,
    )
    yield
    logger.info("%s 종료", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description=(
        "LLM 판단 전담 Agent. 변경 의도 파악·사고의 사슬·@SpringBootTest 생성·"
        "결과 적절성 판단을 담당하며, 코드 기반 처리는 MCP 서비스에 위임한다."
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(router)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """처리되지 않은 예외를 500 JSON 으로 정규화. 스택트레이스는 서버 로그에만 남긴다."""
    logger.exception("처리되지 않은 예외: %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "서버 내부 오류가 발생했습니다.", "error": str(exc)},
    )


@app.get("/", include_in_schema=False)
def root() -> dict:
    return {"app": settings.app_name, "docs": "/docs", "api": "/api/v1"}
