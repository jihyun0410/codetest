"""api_client.AgentClient 가 호출하는 5개 엔드포인트 계약 검증 (LLM/git 없이)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import main, testgen  # 수정: api 모듈이 main 으로 합쳐짐
from app.config import settings
from app.db import Base, Project, get_db
from app.llm import LLMResponse, LLMUnavailableError, llm_client
from app.main import app

LLM_OUTPUT = """\
## IMPORTANCE
HIGH

## IMPORTANCE_RATIONALE
- 로그인 엔드포인트가 직접 수정됨

## LANGUAGE
python

## RUN_COMMAND
python -m pytest -q {file}

## TEST_CODE
```python
def test_login_returns_none():
    assert lookup(999) is None
```

## TEST_RATIONALE
- 반환 타입 변경으로 None 경로가 새로 생겼기 때문
"""

REPORT_OUTPUT = """\
## RESULT
PASS

## VERDICT
적절

## VERDICT_RATIONALE
- 변경된 분기를 모두 통과함

## DETAILS
- 1 passed
"""


@pytest.fixture
def client(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    def _get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _get_db
    # clone/AST 수집은 네트워크가 필요하므로 등록 테스트에서는 건너뛴다.
    monkeypatch.setattr(main, "run_ingest", lambda project_id: None)
    monkeypatch.setattr(settings, "api_keys", [])          # 인증 비활성화 기본값

    with TestClient(app) as c:
        c.session_factory = Session
        yield c
    app.dependency_overrides.clear()


def _register(client, name="demo") -> str:
    res = client.post("/api/v1/projects", json={
        "name": name, "git_url": "https://github.com/acme/demo",
        "owner": "kim", "github_token": "ghp_secret", "default_branch": "main",
    })
    assert res.status_code == 201, res.text
    return res.json()["id"]


# --- health ----------------------------------------------------------------
def test_health_needs_no_auth(client):
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


# --- projects --------------------------------------------------------------
def test_create_project_hides_the_token(client):
    body = client.post("/api/v1/projects", json={
        "name": "demo", "git_url": "https://github.com/acme/demo/",
        "owner": "kim", "github_token": "ghp_secret",
    }).json()

    assert body["ingest_status"] == "PENDING"
    assert body["has_github_token"] is True
    assert "ghp_secret" not in str(body)      # 토큰 원문은 절대 응답에 없다
    assert body["git_url"] == "https://github.com/acme/demo"   # 끝 슬래시 정규화


def test_duplicate_name_is_409(client):
    _register(client)
    assert client.post("/api/v1/projects", json={
        "name": "demo", "git_url": "https://github.com/acme/other", "owner": "kim",
    }).status_code == 409


def test_bad_git_url_is_422(client):
    assert client.post("/api/v1/projects", json={
        "name": "x", "git_url": "ftp://nope", "owner": "kim",
    }).status_code == 422


def test_delete_project(client, monkeypatch):
    monkeypatch.setattr(main.RepoService, "remove", lambda self: None)
    project_id = _register(client)

    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 204
    assert client.delete(f"/api/v1/projects/{project_id}").status_code == 404


# --- tests/generate --------------------------------------------------------
def test_generate_returns_runnable_test(client, monkeypatch):
    project_id = _register(client)
    monkeypatch.setattr(llm_client, "complete",
                        lambda system, user: LLMResponse(text=LLM_OUTPUT, model="stub"))

    body = client.post("/api/v1/tests/generate", json={
        "project_id": project_id,
        "diff": "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-old\n+new\n",
        "sources": [{"path": "app.py", "content": "def lookup(i): return None"}],
        "scope": "staged",
    }).json()

    assert body["importance"] == "HIGH"
    assert body["language"] == "python"
    assert body["file_extension"] == ".py"
    assert body["run_command"] == ["python", "-m", "pytest", "-q", "{file}"]
    assert body["test_code"].startswith("def test_login_returns_none")
    assert "```" not in body["test_code"]          # 펜스는 벗겨진다
    assert "app.py" in body["target_code"]


def test_generate_prompt_carries_diff_and_sources(client, monkeypatch):
    project_id = _register(client)
    seen = {}
    monkeypatch.setattr(llm_client, "complete", lambda system, user: (
        seen.update(user=user) or LLMResponse(text=LLM_OUTPUT, model="stub")))

    client.post("/api/v1/tests/generate", json={
        "project_id": project_id, "diff": "MARKER_DIFF",
        "sources": [{"path": "a.py", "content": "MARKER_SRC"}], "scope": "worktree",
    })
    assert "MARKER_DIFF" in seen["user"] and "MARKER_SRC" in seen["user"]
    assert "worktree" in seen["user"]


def test_generate_unknown_project_is_404(client):
    assert client.post("/api/v1/tests/generate", json={
        "project_id": "nope", "diff": "", "sources": [],
    }).status_code == 404


def test_llm_unavailable_is_503(client, monkeypatch):
    project_id = _register(client)

    def _boom(system, user):
        raise LLMUnavailableError("키 없음")

    monkeypatch.setattr(llm_client, "complete", _boom)
    res = client.post("/api/v1/tests/generate", json={
        "project_id": project_id, "diff": "", "sources": [],
    })
    assert res.status_code == 503
    assert "키 없음" in res.json()["detail"]


# --- tests/report ----------------------------------------------------------
def test_report_uses_exit_code_as_truth(client, monkeypatch):
    project_id = _register(client)
    monkeypatch.setattr(llm_client, "complete",
                        lambda system, user: LLMResponse(text=REPORT_OUTPUT, model="stub"))

    passed = client.post("/api/v1/tests/report", json={
        "project_id": project_id, "test_code": "def test_x(): pass",
        "output": "1 passed", "exit_code": 0, "language": "python",
    }).json()
    assert passed["result"] == "PASS"
    assert passed["verdict"] == "적절"

    # LLM 이 PASS 라고 해도 exit code 가 사실이므로 FAIL 이어야 한다.
    failed = client.post("/api/v1/tests/report", json={
        "project_id": project_id, "test_code": "def test_x(): pass",
        "output": "1 failed", "exit_code": 1, "language": "python",
    }).json()
    assert failed["result"] == "FAIL"


# --- 인증 -------------------------------------------------------------------
def test_api_key_is_enforced_when_configured(client, monkeypatch):
    monkeypatch.setattr(settings, "api_keys", ["s3cret"])
    payload = {"name": "x", "git_url": "https://github.com/a/b", "owner": "kim"}

    assert client.post("/api/v1/projects", json=payload).status_code == 401
    assert client.post("/api/v1/projects", json=payload,
                       headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.post("/api/v1/projects", json=payload,
                       headers={"X-API-Key": "s3cret"}).status_code == 201
    assert client.get("/api/v1/health").status_code == 200      # health 는 항상 열림


# --- 순수 함수 ---------------------------------------------------------------
def test_importance_falls_back_to_graph_value():
    assert testgen._normalize_importance(None, "MID") == "MID"
    assert testgen._normalize_importance("MEDIUM", "LOW") == "MID"
    assert testgen._normalize_importance("중요도: HIGH", "LOW") == "HIGH"


def test_run_command_always_has_a_file_slot():
    assert testgen._parse_command("pytest -q") == ["pytest", "-q", "{file}"]
    assert testgen._parse_command("`pytest {file}`") == ["pytest", "{file}"]
    assert testgen._parse_command("") is None
