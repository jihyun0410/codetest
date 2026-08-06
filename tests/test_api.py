"""Agent 계약 검증 — LLM 과 MCP 를 모두 스텁으로 대체해 경계를 확인한다.

핵심 관심사: Agent 가 **코드 기반 작업을 직접 하지 않고 MCP 에 위임**하는가,
그리고 정의서가 요구하는 판단 결과(의도·CoT·중요도·적절성)를 돌려주는가.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main, testgen
from app.config import settings
from app.llm import LLMResponse, LLMUnavailableError, llm_client
from app.main import app
from app.mcp_client import McpError

GENERATE_OUTPUT = """\
## THINKING
- Diff 에서 quantity > 10 조건 분기가 새로 추가되었다
- AST 상 변경 단위는 OrderService#calculateTotal 하나다
- 임계값 10 을 기준으로 실행 경로가 둘로 갈린다

## INTENT
조건 변경

## INTENT_RATIONALE
- `if (order.getQuantity() > 10)` 분기가 추가됨

## IMPORTANCE
HIGH

## IMPORTANCE_RATIONALE
- 주문 금액 계산은 데이터 정합성에 직결됨

## TEST_CASES
- [정상] 11개면 10% 할인이 적용된다
- [실패] 10개는 할인 대상이 아니다 (경계값)

## TEST_CODE
```java
package com.example.demo;

@SpringBootTest
class OrderServiceTest {
    @Test
    void applies() {}
}
```

## TEST_RATIONALE
- 임계값 경계 분기를 모두 덮기 위해
"""

REPORT_OUTPUT = """\
## VERDICT
적절

## VERDICT_RATIONALE
- 변경된 분기를 모두 통과함

## DETAILS
- 3 passed
"""

ANALYSIS = {
    "project_id": "p1",
    "changed_ranges": {"src/main/java/com/example/demo/service/OrderService.java": [(4, 12)]},
    "changed_units": [
        {
            "qualified_name": "com.example.demo.service.OrderService#calculateTotal(Order)",
            "name": "calculateTotal",
            "node_type": "Method",
            "file_path": "src/main/java/com/example/demo/service/OrderService.java",
            "start_line": 6, "end_line": 12, "entrypoint": False,
        }
    ],
    "impacted_units": [],
    "affected_files": ["src/main/java/com/example/demo/service/OrderService.java"],
    "risk": "MEDIUM", "risk_score": 30, "risk_reasons": ["직접 변경된 그래프 노드 1개"],
    "frameworks": ["Spring Boot"], "base_package": "com.example.demo",
    "graph_ready": True, "warnings": [],
}

EXECUTION = {
    "project_id": "p1", "exit_code": 0, "output": "BUILD SUCCESSFUL",
    "passed": 3, "failed": 0, "skipped": 0, "total": 3, "failures": [],
    "coverage": {"line_rate": 92.0, "line_covered": 23, "line_missed": 2, "branch_rate": 75.0},
    "jacoco_enabled": True, "springboot_applied": True,
    "applied": ["@SpringBootTest 주입 (class OrderServiceTest)"],
    "test_file_path": "src/test/java/com/example/demo/OrderServiceTest.java",
    "command": ["sh", "./gradlew", "test"],
}

GENERATE_BODY = {
    "project_id": "p1",
    "diff": "--- a/A.java\n+++ b/A.java\n@@ -1,2 +1,2 @@\n-old\n+new\n",
    "sources": [{"path": "A.java", "content": "class A {}"}],
    "scope": "staged",
}


class StubMcp:
    """MCP 대역 — Agent 가 무엇을 위임하는지 기록한다."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def health(self):
        self.calls.append("health")
        return {"status": "ok"}

    def overview(self, project_id):
        self.calls.append("overview")
        return {"name": "sample-springboot", "base_package": "com.example.demo"}

    def analyze_changes(self, project_id, diff, sources):
        self.calls.append("analyze_changes")
        return dict(ANALYSIS)

    def execute_tests(self, project_id, test_code, sources, base_package=None):
        self.calls.append("execute_tests")
        self.last_execute = {"test_code": test_code, "base_package": base_package}
        return dict(EXECUTION)

    def create_project(self, **kwargs):
        self.calls.append("create_project")
        return {
            "id": "p1", "name": kwargs["name"], "git_url": kwargs["git_url"],
            "owner": kwargs["owner"], "default_branch": kwargs.get("default_branch", "main"),
            "ingest_status": "PENDING", "ingest_error": None, "last_indexed_at": None,
            "frameworks": [], "language_stats": {},
            "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "has_github_token": bool(kwargs.get("github_token")),
        }

    def delete_project(self, project_id):
        self.calls.append("delete_project")


@pytest.fixture
def mcp(monkeypatch) -> StubMcp:
    stub = StubMcp()
    monkeypatch.setattr(main, "mcp_client", stub)
    monkeypatch.setattr(testgen, "llm_client", llm_client)
    return stub


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", [])
    with TestClient(app) as c:
        yield c


def _stub_llm(monkeypatch, text: str):
    monkeypatch.setattr(
        llm_client, "complete", lambda system, user: LLMResponse(text=text, model="stub")
    )


def _stub_llm_by_prompt(monkeypatch, seen: dict | None = None):
    """생성/판정 프롬프트를 구분해 각각의 응답을 준다."""

    def _complete(system, user):
        if seen is not None:
            seen.setdefault("prompts", []).append(user)
        is_report = "실행 결과" in user
        return LLMResponse(text=REPORT_OUTPUT if is_report else GENERATE_OUTPUT, model="stub")

    monkeypatch.setattr(llm_client, "complete", _complete)


# --- health -----------------------------------------------------------------
def test_health_reports_mcp_status(client, mcp):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["role"] == "llm-based"
    assert body["mcp"]["status"] == "ok"


def test_health_survives_mcp_outage(client, monkeypatch, mcp):
    def _boom():
        raise McpError("연결 실패")

    monkeypatch.setattr(mcp, "health", _boom)
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert "unreachable" in body["mcp"]["status"]


# --- 프로젝트: MCP 위임 ------------------------------------------------------
def test_create_project_is_delegated_to_mcp(client, mcp):
    body = client.post("/api/v1/projects", json={
        "name": "demo", "git_url": "https://github.com/acme/demo",
        "owner": "kim", "github_token": "ghp_secret",
    }).json()

    assert "create_project" in mcp.calls
    assert body["has_github_token"] is True
    assert "ghp_secret" not in str(body)


def test_delete_project_is_delegated_to_mcp(client, mcp):
    assert client.delete("/api/v1/projects/p1").status_code == 204
    assert "delete_project" in mcp.calls


# --- 생성: 의도 + CoT (정의서 (2), [상세] 2) ----------------------------------
def test_generate_returns_intent_and_thinking(client, mcp, monkeypatch):
    _stub_llm(monkeypatch, GENERATE_OUTPUT)
    body = client.post("/api/v1/tests/generate", json=GENERATE_BODY).json()

    assert body["intent"] == "조건 변경"                    # (2) 의도 파악
    assert "getQuantity() > 10" in body["intent_rationale"]      # (2) 근거
    assert body["thinking"].startswith("- Diff")            # [상세 2] 사고의 사슬
    assert "[정상]" in body["test_cases"]                   # (3) 정상 케이스
    assert "[실패]" in body["test_cases"]                   # (3) 실패 케이스
    assert body["importance"] == "HIGH"                     # [UI 4]
    assert "@SpringBootTest" in body["test_code"]
    assert body["base_package"] == "com.example.demo"


def test_generate_delegates_ast_work_to_mcp(client, mcp, monkeypatch):
    _stub_llm(monkeypatch, GENERATE_OUTPUT)
    client.post("/api/v1/tests/generate", json=GENERATE_BODY)
    # Agent 는 AST/그래프를 직접 계산하지 않는다
    assert "analyze_changes" in mcp.calls
    assert "execute_tests" not in mcp.calls   # generate 는 실행하지 않는다


def test_generate_prompt_carries_mcp_facts(client, mcp, monkeypatch):
    seen: dict = {}
    _stub_llm_by_prompt(monkeypatch, seen)
    client.post("/api/v1/tests/generate", json=GENERATE_BODY)

    prompt = seen["prompts"][0]
    assert "OrderService#calculateTotal" in prompt   # MCP 가 준 변경 단위
    assert "Spring Boot" in prompt                   # MCP 가 준 개요
    assert "MEDIUM" in prompt                        # MCP 가 준 영향도


# --- run: 생성 + 실행 + 판정 --------------------------------------------------
def test_run_executes_through_mcp_and_judges(client, mcp, monkeypatch):
    _stub_llm_by_prompt(monkeypatch)
    body = client.post("/api/v1/tests/run", json=GENERATE_BODY).json()

    assert mcp.calls.count("execute_tests") == 1
    report = body["report"]
    assert report["result"] == "PASS"
    assert report["verdict"] == "적절"                       # [UI 3]
    assert report["springboot_applied"] is True              # (1)
    assert report["coverage"]["line_rate"] == 92.0           # [상세 4] JaCoCo
    # (2) 파악한 의도가 결과값에 실려 온다
    assert report["intent"] == "조건 변경"
    assert "getQuantity() > 10" in report["intent_rationale"]


def test_run_passes_base_package_from_generation(client, mcp, monkeypatch):
    _stub_llm_by_prompt(monkeypatch)
    client.post("/api/v1/tests/run", json=GENERATE_BODY)
    assert mcp.last_execute["base_package"] == "com.example.demo"


def test_exit_code_beats_llm_opinion(client, mcp, monkeypatch):
    _stub_llm_by_prompt(monkeypatch)
    monkeypatch.setattr(
        mcp, "execute_tests",
        lambda *a, **k: {**EXECUTION, "exit_code": 1, "failed": 2, "passed": 1},
    )
    body = client.post("/api/v1/tests/run", json=GENERATE_BODY).json()
    assert body["report"]["result"] == "FAIL"   # LLM 은 '적절' 이라 해도 exit code 가 사실


# --- execute: codetest test ---------------------------------------------------
def test_execute_runs_provided_test_code(client, mcp, monkeypatch):
    _stub_llm(monkeypatch, REPORT_OUTPUT)
    body = client.post("/api/v1/tests/execute", json={
        "project_id": "p1", "test_code": "class T {}", "sources": [],
        "intent": "조건 변경", "intent_rationale": "- 이전 실행에서 파악",
    }).json()

    assert mcp.last_execute["test_code"] == "class T {}"
    assert body["result"] == "PASS"
    assert body["intent"] == "조건 변경"       # 이전에 파악한 의도를 그대로 싣는다


def test_execute_rejects_empty_test_code(client, mcp):
    res = client.post("/api/v1/tests/execute", json={
        "project_id": "p1", "test_code": "   ", "sources": [],
    })
    assert res.status_code == 422


# --- 오류 전파 ----------------------------------------------------------------
def test_llm_unavailable_is_503(client, mcp, monkeypatch):
    def _boom(system, user):
        raise LLMUnavailableError("키 없음")

    monkeypatch.setattr(llm_client, "complete", _boom)
    res = client.post("/api/v1/tests/generate", json=GENERATE_BODY)
    assert res.status_code == 503
    assert "키 없음" in res.json()["detail"]


def test_mcp_client_error_is_forwarded(client, mcp, monkeypatch):
    def _boom(project_id, diff, sources):
        raise McpError("프로젝트를 찾을 수 없습니다", 404)

    monkeypatch.setattr(mcp, "analyze_changes", _boom)
    res = client.post("/api/v1/tests/generate", json=GENERATE_BODY)
    assert res.status_code == 404


def test_mcp_server_error_becomes_502(client, mcp, monkeypatch):
    def _boom(project_id, diff, sources):
        raise McpError("MCP 내부 오류", 500)

    monkeypatch.setattr(mcp, "analyze_changes", _boom)
    assert client.post("/api/v1/tests/generate", json=GENERATE_BODY).status_code == 502


def test_api_key_is_enforced_when_configured(client, mcp, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["s3cret"])
    payload = {"name": "x", "git_url": "https://github.com/a/b", "owner": "kim"}

    assert client.post("/api/v1/projects", json=payload).status_code == 401
    assert client.post("/api/v1/projects", json=payload,
                       headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/api/v1/projects", json=payload,
                       headers={"X-API-Key": "s3cret"}).status_code == 201
    assert client.get("/api/v1/health").status_code == 200   # health 는 항상 열림


# --- 순수 함수 ---------------------------------------------------------------
def test_intent_is_normalized_to_spec_categories():
    assert testgen._normalize_intent("조건 변경") == "조건 변경"
    assert testgen._normalize_intent("이 변경은 기능 추가에 해당") == "기능 추가"
    assert testgen._normalize_intent("성능 개선\n추가 설명") == "성능 개선"


def test_importance_falls_back_to_graph_value():
    assert testgen._normalize_importance(None, "MID") == "MID"
    assert testgen._normalize_importance("MEDIUM", "LOW") == "MID"
    assert testgen._normalize_importance("중요도: HIGH", "LOW") == "HIGH"
