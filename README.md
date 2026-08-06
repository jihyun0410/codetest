# codetest-agent — LLM 판단 전담 FastAPI 서비스

정의서:

> **LLM을 사용하여 판단하는 부분은 Agent**, 코드 기반으로 단순 처리 및 판단을 진행하는
> 부분은 MCP로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현

이 서비스는 **코드 기반 작업을 직접 하지 않는다.** AST 파싱·개요 저장·
`@SpringBootTest` 주입·JaCoCo 실행은 전부 MCP 에 FastAPI 로 위임한다.

## 전체 구성

```
  Local Client (codereview_gitver)      CLI 명령 · TUI 결과 · git diff 수집
        │  REST  X-API-Key
        ▼
  Agent (이 저장소)              :8000  LLM 판단
        │  REST  X-API-Key
        ▼
  MCP (codetest-MCP)             :8100  코드 기반 처리
```

## 담당 기능 (전부 정의서 근거)

| 기능 | 정의서 근거 |
|---|---|
| 변경 의도 파악 (기능 추가 / 조건 변경 / 성능 개선 …) | (2) |
| 파악한 의도와 근거를 결과값에 포함 | (2) |
| 사고의 사슬 — 생각 과정을 먼저 적는다 | [상세] 2 |
| 생각 과정 + 프로젝트 개요로 Test Code 생성 | [상세] 3 |
| 정상 케이스 / 실패 케이스 판단 | (3) |
| 여러 파일 변경 시 비즈니스 흐름을 하나의 테스트로 | (3) |
| 기능 중요도 High / Mid / Low | [UI] 4 |
| 결과 적절성 판단과 근거 | [UI] 3 |

## API

| 메서드 | 경로 | CLI 명령 | 설명 |
|---|---|---|---|
| `GET` | `/api/v1/health` | — | 헬스체크 + MCP 연결 상태 |
| `POST` | `/api/v1/projects` | `project register` | MCP 로 위임 |
| `DELETE` | `/api/v1/projects/{id}` | `project delete` | MCP 로 위임 |
| `POST` | `/api/v1/tests/generate` | `codetest generate` | 분석 → 의도 → 생성 |
| `POST` | `/api/v1/tests/run` | `codetest run` | 분석 → 생성 → 실행 → 판정 |
| `POST` | `/api/v1/tests/execute` | `codetest test` | 실행 → 판정 |

### 한 번의 `run` 이 하는 일

```
1. MCP  POST /analysis/changes    Git Diff + AST 로 변경 단위·영향도 확정
2. MCP  GET  /projects/{id}/overview   프로젝트 개요 (프레임워크·기준 패키지)
3. LLM  생성 호출                 THINKING → INTENT → IMPORTANCE → TEST_CASES → TEST_CODE
4. MCP  POST /tests/execute       @SpringBootTest 주입 + gradle test + jacocoTestReport
5. LLM  판정 호출                 VERDICT(적절/부적절) + 근거
```

LLM 응답은 정의서 순서를 그대로 강제한다 — `## THINKING` 을 가장 먼저 적게 해
"생각하는 과정을 먼저 적는다" 요구를 프롬프트 구조로 보장한다.

## 실행

```bash
pip install -e .
cp .env.example .env      # ANTHROPIC_API_KEY 등 입력
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

MCP 가 먼저 떠 있어야 한다. `GET /api/v1/health` 가 MCP 연결 상태를 함께 알려준다.

```jsonc
{"status": "ok", "role": "llm-based",
 "mcp": {"base_url": "http://localhost:8100", "status": "ok"}}
```

### 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CODETEST_API_KEYS` | (없음) | Local Client 인증 키(CSV). 비우면 인증 비활성화 |
| `CODETEST_MCP_BASE_URL` | `http://localhost:8100` | MCP 주소 |
| `CODETEST_MCP_API_KEY` | (없음) | MCP 호출용 키 |
| `CODETEST_MCP_EXECUTE_TIMEOUT` | `960` | Gradle 실행 대기(초) |
| `ANTHROPIC_API_KEY` | (없음) | Claude API 키 |
| `CODETEST_LLM_MODEL` | `claude-opus-5` | 사용할 모델 |

## 테스트

```bash
python -m pytest tests/ -q
```

LLM 과 MCP 를 모두 스텁으로 대체해 **Agent 가 코드 기반 작업을 직접 하지 않는지**
(= MCP 에 위임하는지)와 정의서가 요구하는 판단 결과가 나오는지를 검증한다.
