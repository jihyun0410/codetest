"""
MCP 서비스 REST 클라이언트.

정의서:
  "LLM을 사용하여 판단하는 부분은 Agent, 코드 기반으로 단순 처리 및 판단을
   진행하는 부분은 MCP로 구분하여 **Fast API를 통해 송/수신**하는 방식으로 구현"

Agent 는 코드 기반 작업을 직접 하지 않고 **오직 이 클래스를 통해서만** MCP 에 위임한다.
  · 프로젝트 개요 수집/조회   (Git clone + AST → DB)
  · Git Diff + AST 변경 단위 식별
  · @SpringBootTest 주입 + JaCoCo 실행
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_logger, settings

logger = get_logger(__name__)


class McpError(RuntimeError):
    """MCP 가 4xx/5xx 를 반환했거나 연결에 실패한 경우."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class McpClient:
    """MCP FastAPI 서비스 호출기."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float | None = None,
    ) -> None:
        raw = (base_url or settings.mcp_base_url).rstrip("/")
        self.base_url = f"{raw}/api/v1"
        self.api_key = api_key if api_key is not None else settings.mcp_api_key
        self.timeout = timeout if timeout is not None else settings.mcp_timeout_seconds

    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _request(self, method: str, path: str, timeout: float | None = None, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=timeout or self.timeout) as client:
                response = client.request(method, url, headers=self._headers(), **kwargs)
        except httpx.ConnectError as exc:
            raise McpError(
                f"MCP 서비스에 연결할 수 없습니다: {self.base_url}\n"
                f"  · MCP 가 실행 중인지 확인하세요 (uvicorn codetest_mcp.main:app).\n"
                f"  · CODETEST_MCP_BASE_URL 환경변수로 주소를 바꿀 수 있습니다.\n"
                f"  ({exc})"
            ) from None
        except httpx.TimeoutException:
            raise McpError(f"MCP 요청이 시간 초과되었습니다 ({timeout or self.timeout:.0f}s).") from None

        if response.status_code >= 400:
            raise McpError(_extract_detail(response), response.status_code)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # --- 헬스 ----------------------------------------------------------
    def health(self) -> dict:
        return self._request("GET", "/health")

    # --- 프로젝트 개요 (정의서 상세 1) ----------------------------------
    def create_project(
        self,
        name: str,
        git_url: str,
        owner: str,
        github_token: str | None = None,
        default_branch: str = "main",
    ) -> dict:
        return self._request(
            "POST",
            "/projects",
            json={
                "name": name,
                "git_url": git_url,
                "owner": owner,
                "github_token": github_token,
                "default_branch": default_branch,
            },
        )

    def delete_project(self, project_id: str) -> None:
        self._request("DELETE", f"/projects/{project_id}")

    def overview(self, project_id: str) -> dict:
        return self._request("GET", f"/projects/{project_id}/overview")

    # --- 변경 단위 식별 (정의서 (2)) ------------------------------------
    def analyze_changes(self, project_id: str, diff: str, sources: list[dict]) -> dict:
        return self._request(
            "POST",
            "/analysis/changes",
            json={"project_id": project_id, "diff": diff, "sources": sources},
        )

    # --- 테스트 실행 (정의서 (1), 상세 4) --------------------------------
    def execute_tests(
        self,
        project_id: str,
        test_code: str,
        sources: list[dict],
        base_package: str | None = None,
    ) -> dict:
        """Gradle 빌드는 오래 걸리므로 별도의 넉넉한 타임아웃을 쓴다."""
        return self._request(
            "POST",
            "/tests/execute",
            timeout=settings.mcp_execute_timeout_seconds,
            json={
                "project_id": project_id,
                "test_code": test_code,
                "sources": sources,
                "base_package": base_package,
            },
        )


def _extract_detail(response: httpx.Response) -> str:
    """FastAPI 오류 응답에서 사람이 읽을 메시지를 뽑는다."""
    try:
        payload = response.json()
    except ValueError:
        return f"MCP HTTP {response.status_code}: {response.text[:300]}"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, list):  # pydantic 검증 오류
        parts = [
            f"{'.'.join(str(x) for x in item.get('loc', []))}: {item.get('msg')}"
            for item in detail
        ]
        return f"MCP HTTP {response.status_code}: " + " / ".join(parts)
    return f"MCP HTTP {response.status_code}: {detail or response.text[:300]}"


#: 애플리케이션 전역 싱글턴
mcp_client = McpClient()
