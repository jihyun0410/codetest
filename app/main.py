"""Agent Server 진입점 + REST API.

별도 관리 서버에 배포되는 REST API. 로컬 클라이언트(api_client.AgentClient)가
이 서버와만 통신한다.

    uvicorn app.main:app --host 0.0.0.0 --port 8000

api_client.py 가 호출하는 5개 엔드포인트 전부:

  AgentClient.health()          GET    /api/v1/health          (인증 불필요)
  AgentClient.create_project()  POST   /api/v1/projects
  AgentClient.delete_project()  DELETE /api/v1/projects/{id}
  AgentClient.generate_tests()  POST   /api/v1/tests/generate
  AgentClient.report_tests()    POST   /api/v1/tests/report

테스트 **실행**은 로컬 클라이언트가 한다(개발자 환경 의존성이 필요하므로).
서버는 생성과 판정만 담당한다.
"""

from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import testgen
from app.config import get_logger, settings, setup_logging, verify_api_key
from app.db import IngestStatus, Project, SessionLocal, get_db, init_db
from app.graph.builder import GraphBuilder
from app.llm import LLMRefusalError, LLMUnavailableError
from app.repo import RepoService
from app.schemas import (
    GenerateRequest,
    GenerateResponse,
    ProjectCreate,
    ProjectRead,
    ReportRequest,
    ReportResponse,
)
from app.workflow import WorkflowGenerator

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


def _project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"프로젝트를 찾을 수 없습니다: {project_id}. "
            "`codetest project register` 로 먼저 등록하세요.",
        )
    return project


def _to_read(project: Project) -> ProjectRead:
    """ORM → 응답. github_token 은 보유 여부만 노출한다."""
    payload = ProjectRead.model_validate(project)
    payload.has_github_token = bool(project.github_token)
    return payload


@contextmanager
def _llm_errors():
    """LLM 예외를 클라이언트가 이해할 HTTP 상태로 옮긴다."""
    try:
        yield
    except LLMUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None
    except LLMRefusalError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from None


# --- 백그라운드 수집 ---------------------------------------------------------
# 수정: create_project 보다 위로 옮김. add_task 는 호출 시점에 이름을 찾으므로
#      동작은 같지만, 한 파일이 된 이상 정의가 사용보다 앞서는 편이 읽기 쉽다.
def run_ingest(project_id: str) -> None:
    """등록 직후 백그라운드: clone → AST 파싱 → Graph 적재 → Workflow 생성."""
    db = SessionLocal()
    project = db.get(Project, project_id)
    if project is None:
        db.close()
        return

    project.ingest_status = IngestStatus.RUNNING.value
    project.ingest_error = None
    db.commit()

    try:
        stats = GraphBuilder(db, project).build_full(reset=True)
        WorkflowGenerator(db, project.id).generate()

        project.ingest_status = IngestStatus.READY.value
        project.frameworks = stats.frameworks
        project.language_stats = stats.language_stats
        project.last_indexed_at = datetime.now(timezone.utc)
        db.commit()
        logger.info("[%s] 수집 완료 — 노드 %d, 간선 %d (%.2fs)",
                    project.name, stats.node_count, stats.edge_count, stats.elapsed_seconds)
    except Exception as exc:  # 어떤 실패든 상태에 남긴다
        db.rollback()
        project.ingest_status = IngestStatus.FAILED.value
        project.ingest_error = str(exc)
        db.commit()
        logger.exception("수집 실패: %s", project_id)
    finally:
        db.close()


# --- 라우터 ------------------------------------------------------------------
router = APIRouter(prefix="/api/v1")


@router.get("/health", tags=["health"], summary="헬스체크")
def health() -> dict:
    """연결 확인용 (인증 불필요)."""
    return {"status": "ok", "app": settings.app_name}


# --- 프로젝트 ----------------------------------------------------------------
projects = APIRouter(
    prefix="/projects", tags=["projects"], dependencies=[Depends(require_api_key)]
)


@projects.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED,
               summary="프로젝트 등록")
def create_project(
    payload: ProjectCreate, background: BackgroundTasks, db: Session = Depends(get_db)
) -> ProjectRead:
    """등록 후 전체 소스 수집(clone → AST → Graph → Workflow)을 백그라운드로 시작한다."""
    if db.scalar(select(Project).where(Project.name == payload.name)):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"이미 같은 이름의 프로젝트가 있습니다: {payload.name}",
        )

    project = Project(**payload.model_dump(), ingest_status=IngestStatus.PENDING.value)
    db.add(project)
    db.commit()
    db.refresh(project)

    background.add_task(run_ingest, project.id)
    return _to_read(project)


@projects.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT,
                 summary="프로젝트 삭제")
def delete_project(project_id: str, db: Session = Depends(get_db)) -> None:
    """프로젝트와 그래프/워크플로우를 함께 삭제하고 작업 사본도 제거한다."""
    project = _project(db, project_id)
    repo = RepoService(project.id, project.git_url, project.github_token)
    db.delete(project)
    db.commit()
    repo.remove()


# --- Test Code ---------------------------------------------------------------
tests = APIRouter(
    prefix="/tests", tags=["tests"], dependencies=[Depends(require_api_key)]
)


@tests.post("/generate", response_model=GenerateResponse, summary="Test Code 생성")
def generate_tests(payload: GenerateRequest, db: Session = Depends(get_db)) -> GenerateResponse:
    project = _project(db, payload.project_id)
    with _llm_errors():
        return testgen.generate(
            db, project,
            diff=payload.diff,
            sources=[(item.path, item.content) for item in payload.sources],
            scope=payload.scope,
        )


@tests.post("/report", response_model=ReportResponse, summary="테스트 결과 판정")
def report_tests(payload: ReportRequest, db: Session = Depends(get_db)) -> ReportResponse:
    _project(db, payload.project_id)
    with _llm_errors():
        return testgen.report(
            test_code=payload.test_code,
            output=payload.output,
            exit_code=payload.exit_code,
            language=payload.language,
        )


router.include_router(projects)
router.include_router(tests)


# --- 앱 ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """기동 시 런타임 디렉터리 생성 + 테이블 초기화."""
    settings.ensure_directories()
    init_db()
    logger.info("%s 기동 (model=%s)", settings.app_name, settings.llm_model)
    yield
    logger.info("%s 종료", settings.app_name)


app = FastAPI(
    title=settings.app_name,
    description="변경 코드를 AST Graph 위에서 분석해 Test Code 를 생성하는 Agent Server.",
    version="0.1.0",
    lifespan=lifespan,
)
# 수정: include_router 는 호출 시점의 라우트를 복사하므로 반드시 라우터 정의 뒤에 온다.
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
