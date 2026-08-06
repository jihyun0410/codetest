"""
핵심 파이프라인 통합 테스트 (외부 의존 없음).

git / GitHub / LLM / MCP 없이도 아래 흐름이 동작하는지 검증한다.

  AST 파싱 → Graph 적재 → 심볼 해석 → Workflow 생성 → Diff 영향도 → 출력 양식 파싱

Tree-sitter 문법이 없는 환경에서는 각 파서의 폴백 경로가 실행되며,
그 경우에도 File/Class/Method 골격과 워크플로우가 만들어져야 한다.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base, EdgeType, NodeType, Project, RiskLevel
from app.graph.impact import ImpactAnalyzer, parse_diff_ranges
from app.graph.store import GraphStore
from app.parsing.registry import detect_language, is_supported, parse_source
from app.workflow import WorkflowGenerator, to_mermaid

# ---------------------------------------------------------------------------
#  샘플 소스
# ---------------------------------------------------------------------------
PYTHON_APP = '''\
from fastapi import FastAPI

from services.user_service import UserService

app = FastAPI()
service = UserService()


@app.post("/api/users/login")
def login(user_id: int):
    """로그인 엔드포인트 (워크플로우 진입점)."""
    return service.get_user(user_id)


@app.get("/api/orders")
def list_orders():
    return service.find_orders()
'''

PYTHON_SERVICE = '''\
class UserService:
    def get_user(self, user_id):
        return self.query_user(user_id)

    def query_user(self, user_id):
        sql = "SELECT id, name, status FROM users WHERE id = %s"
        return sql

    def find_orders(self):
        return "SELECT * FROM orders"
'''

JAVA_CONTROLLER = """\
package com.demo.user;

import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class UserController {

    private final UserService userService;

    public UserController(UserService userService) {
        this.userService = userService;
    }

    @PostMapping("/api/users/login")
    public String login(Long id) {
        return userService.getUser(id);
    }
}
"""

MYBATIS_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
  "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.demo.user.UserMapper">
  <select id="selectUser" parameterType="long" resultType="com.demo.user.User">
    SELECT id, name, status FROM users WHERE id = #{id}
  </select>
  <update id="updateStatus">
    UPDATE users SET status = #{status} WHERE id = #{id}
  </update>
</mapper>
"""

SQL_FILE = """\
-- 회원 조회
SELECT id, name FROM users WHERE status = 'ACTIVE';

UPDATE orders SET state = 'DONE' WHERE order_id = 1;

CREATE TABLE tmp (id INT);
"""


@pytest.fixture()
def db():
    """테스트용 인메모리 SQLite 세션."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def project(db):
    row = Project(name="demo", git_url="https://github.com/demo/demo", owner="tester")
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
#  파서
# ---------------------------------------------------------------------------
def test_language_detection():
    assert detect_language("src/A.java") == "java"
    assert detect_language("src/a.ts") == "typescript"
    assert detect_language("a.py") == "python"
    assert detect_language("m.xml") == "xml"
    assert detect_language("q.sql") == "sql"
    assert detect_language("README.md") is None

    # 빌드 산출물/의존성 디렉터리는 제외된다.
    assert is_supported("src/main/java/A.java")
    assert not is_supported("node_modules/pkg/index.js")
    assert not is_supported("target/classes/A.java")


def test_python_parser_extracts_entrypoint_and_sql():
    result = parse_source("app/main.py", PYTHON_APP)

    methods = {n.name: n for n in result.nodes if n.node_type == NodeType.METHOD}
    assert "login" in methods
    # @app.post("/api/users/login") → 워크플로우 진입점으로 표시되어야 한다
    assert methods["login"].meta["entrypoint"] is True
    assert methods["login"].meta["http_method"] == "POST"
    assert methods["login"].meta["route"] == "/api/users/login"
    assert "FastAPI" in result.frameworks

    service = parse_source("services/user_service.py", PYTHON_SERVICE)
    sql_nodes = [n for n in service.nodes if n.node_type == NodeType.SQL]
    assert sql_nodes, "인라인 SQL 문자열이 SQL 노드로 승격되어야 한다"
    assert any(
        edge.edge_type == EdgeType.EXECUTES for edge in service.edges
    ), "SQL 노드에는 Executes 간선이 연결되어야 한다"


def test_java_parser_produces_skeleton():
    """Tree-sitter 유무와 무관하게 File/Class/Method 골격이 나와야 한다."""
    result = parse_source("src/main/java/com/demo/user/UserController.java", JAVA_CONTROLLER)

    types = {n.node_type for n in result.nodes}
    assert NodeType.FILE in types
    assert NodeType.CLASS in types
    assert any(n.qualified_name.startswith("com.demo.user.UserController") for n in result.nodes)
    assert any(edge.edge_type == EdgeType.CONTAINS for edge in result.edges)


def test_mybatis_parser_creates_sql_nodes():
    result = parse_source("src/main/resources/UserMapper.xml", MYBATIS_XML)

    sql_nodes = [n for n in result.nodes if n.node_type == NodeType.SQL]
    qnames = {n.qualified_name for n in sql_nodes}

    # Java Mapper 인터페이스의 Executes 힌트와 매칭되는 규약이어야 한다.
    assert "mapper:com.demo.user.UserMapper.selectUser" in qnames
    assert "mapper:com.demo.user.UserMapper.updateStatus" in qnames
    assert "MyBatis" in result.frameworks

    select_node = next(n for n in sql_nodes if n.name == "selectUser")
    assert select_node.meta["operation"] == "SELECT"
    assert "users" in select_node.meta["tables"]


def test_sql_parser_skips_ddl():
    result = parse_source("db/queries.sql", SQL_FILE)
    sql_nodes = [n for n in result.nodes if n.node_type == NodeType.SQL]

    operations = {n.meta["operation"] for n in sql_nodes}
    assert operations == {"SELECT", "UPDATE"}, "DML 만 노드로 만들어야 한다 (CREATE 제외)"


# ---------------------------------------------------------------------------
#  Graph 적재 + 심볼 해석
# ---------------------------------------------------------------------------
def _load_python_graph(db, project) -> GraphStore:
    """샘플 Python 프로젝트를 그래프에 적재한다."""
    from app.parsing.base import ParseResult

    aggregate = ParseResult()
    aggregate.merge(parse_source("app/main.py", PYTHON_APP))
    aggregate.merge(parse_source("services/user_service.py", PYTHON_SERVICE))

    store = GraphStore(db, project.id)
    store.upsert_nodes(aggregate.nodes)
    index = store.build_index()
    store.persist_edges(aggregate.edges, index)
    db.flush()
    return store


def test_graph_store_resolves_cross_file_calls(db, project):
    store = _load_python_graph(db, project)

    nodes = {node.qualified_name: node for node in store.all_nodes()}
    login = next(n for n in nodes.values() if n.name == "login")
    get_user = next(n for n in nodes.values() if n.name == "get_user")

    calls = [
        edge
        for edge in store.all_edges()
        if edge.edge_type == EdgeType.CALLS.value
        and edge.source_id == login.id
        and edge.target_id == get_user.id
    ]
    assert calls, "파일을 넘나드는 호출(login → get_user)이 Calls 간선으로 해석되어야 한다"


def test_workflow_generation_and_mermaid(db, project):
    _load_python_graph(db, project)

    generator = WorkflowGenerator(db, project.id)
    workflows = generator.generate()
    db.flush()

    assert workflows, "진입점이 있으면 Workflow 가 생성되어야 한다"

    login_flow = next(wf for wf in workflows if "login" in wf.key)
    assert login_flow.name == "POST /api/users/login"

    steps = login_flow.steps
    assert len(steps) >= 2, "진입점 + 호출 대상이 단계로 포함되어야 한다"
    # 모든 단계는 물리 경로와 논리 경로를 함께 가진다 (정의서 BE-3)
    for step in steps:
        assert step["physical_path"]
        assert step["logical_path"]
    assert steps[0]["kind"] == "entry"
    assert login_flow.involved_files, "Flow 실행 파일 목록이 채워져야 한다"

    mermaid = to_mermaid(login_flow)
    assert mermaid.startswith("flowchart TD")
    assert "classDef entry" in mermaid


# ---------------------------------------------------------------------------
#  Diff → 영향도
# ---------------------------------------------------------------------------
SAMPLE_DIFF = """\
diff --git a/services/user_service.py b/services/user_service.py
index 1111111..2222222 100644
--- a/services/user_service.py
+++ b/services/user_service.py
@@ -1,7 +1,7 @@
 class UserService:
     def get_user(self, user_id):
-        return self.query_user(user_id)
+        return self.query_user(user_id) or None
"""


def test_parse_diff_ranges():
    ranges = parse_diff_ranges(SAMPLE_DIFF)
    assert "services/user_service.py" in ranges
    assert ranges["services/user_service.py"] == [(1, 7)]


def test_impact_analysis_reaches_entrypoint(db, project):
    store = _load_python_graph(db, project)

    analyzer = ImpactAnalyzer(store)
    report = analyzer.analyze(parse_diff_ranges(SAMPLE_DIFF))

    assert report.changed, "변경 라인과 겹치는 노드를 찾아야 한다"
    assert report.reasons, "영향도 등급에는 반드시 근거가 있어야 한다 (정의서 Rationale 요구)"
    assert report.risk in {RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH}

    # get_user 를 호출하는 login(진입점)까지 역방향으로 도달해야 한다.
    reached = {info.qualified_name for info in report.impacted}
    assert any("login" in name for name in reached), (
        f"진입점까지 영향이 전파되어야 한다. 도달한 노드: {sorted(reached)}"
    )


# ---------------------------------------------------------------------------
#  출력 양식 파싱
# ---------------------------------------------------------------------------
