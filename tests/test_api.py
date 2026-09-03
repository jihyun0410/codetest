"""Agent 계약 검증 — LLM 을 스텁으로 대체하고 경계를 확인한다.

Agent 는 **LLM 판단만** 한다. 진입점은 MCP 이고, 코드 기반 사실(변경 단위·영향도·
실행 결과·기능 중요도)은 이미 확정된 채로 요청 본문에 실려 온다.

검증 관심사:
  · MCP 가 보내는 payload 를 그대로 받는가 (codetest-mcp `agent_client.py` 계약)
  · MCP 가 읽어 가는 응답 키를 빠짐없이 채우는가 (codetest-mcp `orchestrator.py`)
  · Agent 가 코드 기반 판단(기능 중요도)을 넘보지 않는가
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import testgen
from app.config import settings
from app.llm import LLMRefusalError, LLMResponse, LLMUnavailableError, llm_client
from app.main import app

GENERATE_OUTPUT = """\
## THINKING
- Diff 에서 quantity > 10 조건 분기가 새로 추가되었다
- AST 상 변경 단위는 OrderService#calculateTotal 하나다

## INTENT
조건 변경

## INTENT_RATIONALE
- `if (order.getQuantity() > 10)` 분기가 추가됨

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

#: MCP `ChangeAnalysisResponse` 를 model_dump 한 모양
ANALYSIS = {
    "project_id": "p1",
    "diff": "--- a/A.java\n+++ b/A.java\n@@ -1,2 +1,2 @@\n-old\n+new\n",
    "changed_ranges": {"src/main/java/com/example/demo/service/OrderService.java": [[4, 12]]},
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
    "importance": "HIGH", "importance_rationale": "주문 금액 계산은 정합성에 직결",
    "frameworks": ["Spring Boot"], "base_package": "com.example.demo",
    "graph_ready": True, "warnings": [],
}

#: MCP `ExecuteResponse` 를 model_dump 한 모양
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
    "project_name": "sample-springboot",
    "analysis": ANALYSIS,
    "sources": [{"path": "A.java", "content": "class A {}"}],
}

EXECUTE_BODY = {
    "project_id": "p1",
    "execution": EXECUTION,
    "test_code": "class OrderServiceTest {}",
    "intent": "조건 변경",
    "intent_rationale": "- 이전 generate 에서 파악",
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "api_keys", [])     # 인증 비활성화 기본값
    with TestClient(app) as c:
        yield c


def _stub_llm(monkeypatch, text: str, seen: dict | None = None):
    def _complete(system, user):
        if seen is not None:
            seen["prompt"] = user
        return LLMResponse(text=text, model="stub")

    monkeypatch.setattr(llm_client, "complete", _complete)


# --- health -----------------------------------------------------------------
def test_health_needs_no_auth_and_reports_role(client):
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["role"] == "llm-based"


# --- generate: MCP 가 읽어 가는 키 --------------------------------------------
def test_generate_returns_every_key_mcp_reads(client, monkeypatch):
    """codetest-mcp orchestrator._to_generated() 가 꺼내는 키 전부."""
    _stub_llm(monkeypatch, GENERATE_OUTPUT)
    body = client.post("/api/v1/tests/generate", json=GENERATE_BODY).json()

    assert body["intent"] == "조건 변경"
    assert "getQuantity() > 10" in body["intent_rationale"]
    assert body["thinking"].startswith("- Diff")
    assert "[정상]" in body["test_cases"] and "[실패]" in body["test_cases"]
    assert "@SpringBootTest" in body["test_code"]
    assert "```" not in body["test_code"]            # 코드 펜스는 벗겨진다
    assert body["rationale"].startswith("- 임계값")
    assert "A.java" in body["target_code"]


def test_generate_does_not_judge_importance(client, monkeypatch):
    """기능 중요도는 MCP 가 코드 그래프로 확정한다 — Agent 는 돌려주지 않는다."""
    _stub_llm(monkeypatch, GENERATE_OUTPUT)
    body = client.post("/api/v1/tests/generate", json=GENERATE_BODY).json()
    assert "importance" not in body


def test_generate_prompt_carries_mcp_facts(client, monkeypatch):
    """Agent 는 AST 를 다시 계산하지 않고 MCP 가 준 사실을 프롬프트에 싣는다."""
    seen: dict = {}
    _stub_llm(monkeypatch, GENERATE_OUTPUT, seen)
    client.post("/api/v1/tests/generate", json=GENERATE_BODY)

    prompt = seen["prompt"]
    assert "sample-springboot" in prompt                  # project_name
    assert "calculateTotal" in prompt                     # changed_units
    assert "MEDIUM" in prompt                             # risk
    assert "직접 변경된 그래프 노드 1개" in prompt          # risk_reasons
    assert "Spring Boot" in prompt                        # frameworks
    assert "com.example.demo" in prompt                   # base_package
    assert "class A {}" in prompt                         # sources


def test_generate_without_analysis_is_422(client):
    res = client.post("/api/v1/tests/generate", json={**GENERATE_BODY, "analysis": {}})
    assert res.status_code == 422
    assert "analysis" in res.json()["detail"]


# --- execute: 적절성 판단 -----------------------------------------------------
def test_execute_returns_every_key_mcp_reads(client, monkeypatch):
    """codetest-mcp orchestrator._to_report() 가 꺼내는 키 전부."""
    _stub_llm(monkeypatch, REPORT_OUTPUT)
    body = client.post("/api/v1/tests/execute", json=EXECUTE_BODY).json()

    assert body["verdict"] == "적절"
    assert "변경된 분기" in body["verdict_rationale"]
    assert "3 passed" in body["details"]
    # 파악한 의도는 결과값에 그대로 실려 돌아온다
    assert body["intent"] == "조건 변경"
    assert body["intent_rationale"] == "- 이전 generate 에서 파악"


def test_execute_prompt_carries_execution_facts(client, monkeypatch):
    seen: dict = {}
    _stub_llm(monkeypatch, REPORT_OUTPUT, seen)
    client.post("/api/v1/tests/execute", json=EXECUTE_BODY)

    prompt = seen["prompt"]
    assert "BUILD SUCCESSFUL" in prompt        # output
    assert "조건 변경" in prompt                # intent — 의도 검증 여부 판단 근거
    assert "92.0" in prompt                    # JaCoCo 커버리지


def test_execute_does_not_recount_results(client, monkeypatch):
    """실행 집계·커버리지는 MCP 가 이미 갖고 있다 — 응답에 되풀이하지 않는다."""
    _stub_llm(monkeypatch, REPORT_OUTPUT)
    body = client.post("/api/v1/tests/execute", json=EXECUTE_BODY).json()
    for key in ("passed", "failed", "total", "coverage", "exit_code", "result"):
        assert key not in body


def test_execute_without_execution_is_422(client):
    res = client.post("/api/v1/tests/execute", json={**EXECUTE_BODY, "execution": {}})
    assert res.status_code == 422
    assert "execution" in res.json()["detail"]


# --- LLM 장애 ----------------------------------------------------------------
def test_llm_unavailable_is_503(client, monkeypatch):
    def _boom(system, user):
        raise LLMUnavailableError("키 없음")

    monkeypatch.setattr(llm_client, "complete", _boom)
    res = client.post("/api/v1/tests/generate", json=GENERATE_BODY)
    assert res.status_code == 503
    assert "키 없음" in res.json()["detail"]


def test_llm_refusal_is_422(client, monkeypatch):
    def _refuse(system, user):
        raise LLMRefusalError("content_filter", "거부됨")

    monkeypatch.setattr(llm_client, "complete", _refuse)
    assert client.post("/api/v1/tests/execute", json=EXECUTE_BODY).status_code == 422


# --- 인증 ---------------------------------------------------------------------
def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["s3cret"])
    _stub_llm(monkeypatch, GENERATE_OUTPUT)

    assert client.post("/api/v1/tests/generate", json=GENERATE_BODY).status_code == 401
    assert client.post("/api/v1/tests/generate", json=GENERATE_BODY,
                       headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/api/v1/tests/generate", json=GENERATE_BODY,
                       headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/api/v1/health").status_code == 200      # health 는 항상 열림


# --- 순수 함수 -----------------------------------------------------------------
def test_intent_is_normalized_to_one_line():
    assert testgen._normalize_intent("조건 변경\n부연 설명") == "조건 변경"
    assert testgen._normalize_intent(None) == ""


def test_code_fence_is_stripped():
    assert testgen._extract_code("```java\nclass A {}\n```") == "class A {}"
    assert testgen._extract_code("class B {}") == "class B {}"
