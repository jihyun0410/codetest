# codetest-agent — LLM 판단 전담 FastAPI 서비스

정의서:

> **LLM을 사용하여 판단하는 부분은 Agent**, 코드 기반으로 단순 처리 및 판단을 진행하는
> 부분은 MCP로 구분하여 Fast API를 통해 송/수신하는 방식으로 구현

이 서비스는 **코드 기반 작업을 직접 하지 않는다.** AST 파싱·개요 저장·기능 중요도
판정·`@SpringBootTest` 주입·JaCoCo 실행은 전부 MCP 가 끝낸 상태로 넘어온다.

## 전체 구성

진입점은 **MCP** 다. CLI 는 MCP 만 알고, Agent 는 MCP 가 부른다.

```
  CLI (codereview_gitver)               CLI 명령 · TUI 결과 · git diff 수집
        │  MCP tools/call  X-API-Key
        ▼
  MCP (codetest-MCP)             :80    코드 기반 처리 · 기능 중요도 판정
        │  REST  X-API-Key
        ▼
  Agent (이 저장소)              :8000  LLM 판단
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
| 결과 적절성 판단과 근거 | [UI] 3 |

기능 중요도(High/Mid/Low)와 그 근거는 **여기서 판단하지 않는다** — 코드 그래프로
확정하는 값이라 MCP(`codetest-MCP/codetest_mcp/importance.py`)의 몫이다.

## API (호출하는 쪽은 MCP 다)

| 메서드 | 경로 | MCP 도구 | 설명 |
|---|---|---|---|
| `GET` | `/api/v1/health` | — | 헬스체크 (인증 불필요) |
| `POST` | `/api/v1/tests/generate` | `test_generate` / `test_run` | 의도 파악 → 생각 과정 → Test Code 생성 |
| `POST` | `/api/v1/tests/execute` | `execute_tests` / `test_run` | 실행 결과 적절성 판정 |

### 한 번의 `codetest run` 이 하는 일

```
CLI  test_run 호출
 └ MCP  Git Diff + AST 로 변경 단위·영향도 확정          (코드 기반)
   MCP  기능 중요도 High/Mid/Low + 판단 근거 확정        (코드 기반)
   └ Agent  POST /api/v1/tests/generate
              THINKING → INTENT → TEST_CASES → TEST_CODE (LLM)
   MCP  @SpringBootTest 주입 + gradle test + jacocoTestReport
   └ Agent  POST /api/v1/tests/execute
              VERDICT(적절/부적절) + 근거                (LLM)
CLI  결과 화면 (중요도·근거 / TEST CODE 보기 / TEST RESULT)
```

LLM 응답은 정의서 순서를 그대로 강제한다 — `## THINKING` 을 가장 먼저 적게 해
"생각하는 과정을 먼저 적는다" 요구를 프롬프트 구조로 보장한다.

## 실행

```bash
pip install -e .
cp .env.example .env      # OPENAI_API_KEY 등 입력
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**Agent 가 먼저 떠 있어야 MCP 가 붙는다.** MCP 쪽 `CODETEST_MCP_AGENT_URL` 이 이
서버를 가리켜야 한다.

```jsonc
{"status": "ok", "app": "Code Test AI Agent", "role": "llm-based", "model": "gpt-5"}
```

### 환경변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CODETEST_API_KEYS` | (없음) | MCP 인증 키(CSV). 비우면 인증 비활성화 |
| `CODETEST_HOST` / `CODETEST_PORT` | `0.0.0.0` / `8000` | 수신 주소 |
| `OPENAI_API_KEY` | (없음) | LLM API 키 |
| `OPENAI_BASE_URL` | (없음) | OpenAI 호환 엔드포인트. 비우면 공식 주소 |
| `CODETEST_LLM_MODEL` | `gpt-5` | 사용할 모델 |
| `CODETEST_LLM_EFFORT` | `high` | 추론 강도 (minimal/low/medium/high) |
| `CODETEST_LLM_MAX_TOKENS` | `32000` | 출력 토큰 상한 |

## 테스트

```bash
python -m pytest tests/ -q
```

LLM 을 스텁으로 대체해 **Agent 가 코드 기반 작업을 직접 하지 않는지**(= MCP 가 준
사실만 쓰는지)와 정의서가 요구하는 판단 결과가 나오는지를 검증한다.
