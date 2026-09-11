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


# --- 앞단 프록시 대비 keep-alive 스트림 ----------------------------------------
#
# nginx 의 proxy_read_timeout 은 총 소요 시간이 아니라 **무응답 시간**이다.
# LLM 이 생각하는 동안 한 바이트도 안 보내면 504 가 만들어진다. 그래서
# Accept: application/x-ndjson 이면 ping 을 흘려보내며 기다린다.
NDJSON = {"Accept": "application/x-ndjson"}


def _ndjson_lines(response) -> list[dict]:
    import json as _json

    return [_json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_generate_streams_pings_while_the_llm_thinks(client, monkeypatch):
    """생성이 오래 걸려도 그 사이 ping 이 나가야 프록시가 504 를 만들지 않는다."""
    import time

    monkeypatch.setattr(settings, "llm_ping_seconds", 0.05)
    monkeypatch.setattr(
        llm_client,
        "complete",
        lambda system, user: (time.sleep(0.3), LLMResponse(text=GENERATE_OUTPUT, model="stub"))[1],
    )

    res = client.post("/api/v1/tests/generate", json=GENERATE_BODY, headers=NDJSON)
    assert res.status_code == 200
    lines = _ndjson_lines(res)

    assert any(line["type"] == "ping" for line in lines), "ping 이 하나도 없으면 프록시가 끊는다"
    assert lines[-1]["type"] == "result"
    assert "@SpringBootTest" in lines[-1]["data"]["test_code"]


def test_stream_reports_llm_failure_in_the_body(client, monkeypatch):
    """스트림이 시작된 뒤에는 상태 코드를 못 바꾸므로 오류도 본문으로 온다."""
    def _boom(system, user):
        raise LLMUnavailableError("키가 없습니다")

    monkeypatch.setattr(llm_client, "complete", _boom)
    res = client.post("/api/v1/tests/generate", json=GENERATE_BODY, headers=NDJSON)

    assert res.status_code == 200
    last = _ndjson_lines(res)[-1]
    assert last == {"type": "error", "status": 503, "detail": "키가 없습니다"}


def test_execute_streams_too(client, monkeypatch):
    _stub_llm(monkeypatch, REPORT_OUTPUT)
    res = client.post("/api/v1/tests/execute", json=EXECUTE_BODY, headers=NDJSON)

    last = _ndjson_lines(res)[-1]
    assert last["type"] == "result"
    assert last["data"]["verdict"] == "적절"


def test_plain_json_still_works_for_older_callers(client, monkeypatch):
    """Accept 를 안 보내는 예전 MCP 는 예전처럼 JSON 한 덩어리를 받는다."""
    _stub_llm(monkeypatch, GENERATE_OUTPUT)
    res = client.post("/api/v1/tests/generate", json=GENERATE_BODY)

    assert res.headers["content-type"].startswith("application/json")
    assert "@SpringBootTest" in res.json()["test_code"]


# --- 프롬프트 예산 -------------------------------------------------------------
def test_oversized_context_drops_whole_files_instead_of_cutting_one():
    """잘린 파일을 보내면 모델이 없는 API 를 지어낸다 — 통째로 빼고 알린다."""
    big = "class Big { " + "int x; " * 8000 + "}"
    section = testgen._target_code_section(
        [("Changed.java", "class Changed {}"), ("Big.java", big), ("Tail.java", "class Tail {}")]
    )

    assert "class Changed {}" in section                     # 우선순위 1순위는 남는다
    assert "int x;" not in section                           # 잘린 조각이 들어가지 않는다
    assert "생략된 파일" in section and "Big.java" in section  # 뺐다고 알린다
    assert len(section) < testgen.MAX_TARGET_CHARS + 2000


def test_small_context_is_sent_whole():
    section = testgen._target_code_section([("A.java", "class A {}"), ("B.java", "class B {}")])
    assert "class A {}" in section and "class B {}" in section
    assert "생략된 파일" not in section


# --- 빌드 실패는 테스트 실패와 다르다 --------------------------------------------
#
# 컴파일이 깨지면 테스트가 시작조차 못해 집계가 전부 0 이 된다. 이 사실을 빼고
# 보내면 "실패 0건이니 통과" 라는 엉뚱한 판정이 나온다.
def test_build_errors_reach_the_prompt(client, monkeypatch):
    seen: dict = {}
    _stub_llm(monkeypatch, REPORT_OUTPUT, seen)

    client.post("/api/v1/tests/execute", json={
        **EXECUTE_BODY,
        "execution": {
            "exit_code": 1, "total": 0, "passed": 0, "failed": 0, "skipped": 0,
            "failures": [], "springboot_applied": True, "jacoco_enabled": True,
            "build_errors": ["FooTest.java:52: not a statement"],
        },
    })

    prompt = seen["prompt"]
    assert "테스트가 한 건도 실행되지 않았습니다" in prompt
    assert "FooTest.java:52: not a statement" in prompt
    assert "빌드 오류" in prompt


def test_a_normal_run_says_nothing_about_the_build(client, monkeypatch):
    seen: dict = {}
    _stub_llm(monkeypatch, REPORT_OUTPUT, seen)
    client.post("/api/v1/tests/execute", json=EXECUTE_BODY)

    assert "빌드 오류" not in seen["prompt"]


def test_the_judge_is_told_not_to_read_zero_failures_as_a_pass():
    """프롬프트 규칙 자체를 고정한다 — 이 문장이 빠지면 오판이 되돌아온다."""
    from app.testgen import _REPORT_SYSTEM

    assert "빌드 오류가 보고되면 테스트는 한 건도 실행되지 않은 것입니다" in _REPORT_SYSTEM
    assert "'부적절' 로 판단" in _REPORT_SYSTEM
