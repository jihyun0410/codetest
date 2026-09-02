"""Agent Server 진입점 + REST API.

정의서:
  "**LLM을 사용하여 판단하는 부분은 Agent**, 코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현"

이 서버는 **LLM 판단만** 수행한다. 진입점은 MCP 다 — CLI 명령을 받은 MCP 가
Git clone·AST·개요 저장·기능 중요도 판단·@SpringBootTest 주입·JaCoCo 실행을
코드 기반으로 끝낸 뒤, LLM 이 필요한 부분만 이 서버에 FastAPI 로 넘긴다.

    IntelliJ Terminal → CLI → MCP → Agent(이 서버) → MCP → CLI

    uvicorn app.main:app --host 0.0.0.0 --port 8000

MCP 가 호출하는 엔드포인트:

  GET  /api/v1/health            연결 확인 (인증 불필요)
  POST /api/v1/tests/generate    변경 의도 파악 + 사고의 사슬 + Test Code 생성
  POST /api/v1/tests/execute     실행 결과 적절성 판단

**기능 중요도는 여기서 판단하지 않는다.** 코드 그래프로 확정하는 값이라 MCP 의 몫이다
(codetest-MCP `importance.py`).
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app import testgen
from app.config import get_logger, settings, setup_logging, verify_api_key
from app.llm import LLMRefusalError, LLMUnavailableError
from app.schemas import (
    ExecuteRequest,
    GenerateRequest,
    GenerateResponse,
    ReportResponse,
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


# --- 라우터 ------------------------------------------------------------------
router = APIRouter(prefix="/api/v1")


@router.get("/health", tags=["health"], summary="헬스체크")
def health() -> dict:
    """연결 확인용 (인증 불필요). MCP 의 `hello` 도구가 이 값을 함께 알린다."""
    return {
        "status": "ok",
        "app": settings.app_name,
        "role": "llm-based",
        "model": settings.llm_model,
    }


tests = APIRouter(
    prefix="/tests", tags=["tests"], dependencies=[Depends(require_api_key)]
)


@tests.post("/generate", response_model=GenerateResponse,
            summary="Test Code 생성 (CLI: codetest generate / run)")
def generate_tests(payload: GenerateRequest) -> GenerateResponse:
    """
    MCP 가 확정한 변경 단위·영향도를 근거로 의도를 파악하고 @SpringBootTest 를 만든다.

    코드 기반 작업은 하지 않는다 — 분석은 이미 끝난 상태로 본문에 실려 온다.
    """
    if not payload.analysis:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "analysis 가 비어 있습니다. MCP 의 변경 분석 결과를 함께 보내야 합니다.",
        )
    with _llm_errors():
        return testgen.generate(
            payload.analysis,
            sources=[(item.path, item.content) for item in payload.sources],
            project_name=payload.project_name or payload.project_id,
        )


@tests.post("/execute", response_model=ReportResponse,
            summary="실행 결과 적절성 판단 (CLI: codetest test / run)")
def execute_tests(payload: ExecuteRequest) -> ReportResponse:
    """MCP 가 돌린 @SpringBootTest 결과를 보고 적절성을 판단한다."""
    if not payload.execution:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "execution 이 비어 있습니다. MCP 의 실행 결과를 함께 보내야 합니다.",
        )
    with _llm_errors():
        return testgen.report(
            payload.execution,
            payload.test_code,
            payload.intent,
            payload.intent_rationale,
        )


router.include_router(tests)


# --- 앱 ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("%s 기동 (model=%s)", settings.app_name, settings.llm_model)
    yield
    logger.info("%s 종료", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description=(
        "LLM 판단 전담 Agent. 변경 의도 파악·사고의 사슬·@SpringBootTest 생성·"
        "결과 적절성 판단을 담당한다. 코드 기반 처리와 기능 중요도 판단은 MCP 의 몫이다."
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
