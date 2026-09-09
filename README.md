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
| `CODETEST_LLM_EFFORT` | `medium` | 추론 강도 (minimal/low/medium/high). 아래 "생성 시간" 참고 |
| `CODETEST_LLM_MAX_TOKENS` | `32000` | 출력 토큰 상한 |
| `CODETEST_LLM_PING_SECONDS` | `10` | 생성 중 keep-alive 간격(초) |

## 생성 시간 — 504 Gateway Time-out 을 만드는 것

앞단 리버스 프록시(nginx)의 `proxy_read_timeout` 은 **총 소요 시간이 아니라 무응답
시간**이다. 기본값 60초 동안 upstream 에서 한 바이트도 안 오면 504 를 만든다.
예전에는 이 서버가 LLM 을 다 기다린 뒤에야 JSON 을 한 덩어리로 내보냈으므로,
생성이 3분 걸리면 3분 내내 조용했다 — 프록시 입장에서는 죽은 연결이다.

두 갈래로 고쳤다.

**1. 기다리는 동안 한 줄씩 흘려보낸다.** `Accept: application/x-ndjson` 이면
생성이 끝날 때까지 `{"type":"ping"}` 을 `CODETEST_LLM_PING_SECONDS` 간격으로 보내고,
마지막에 `{"type":"result","data":{…}}` 한 줄을 보낸다. 받는 쪽(MCP `agent_client`)은
ping 을 버리고 마지막 줄만 쓴다. Accept 를 안 보내면 예전처럼 JSON 한 덩어리로 답한다.

측정값 (LLM 이 25초 걸리는 스텁):

| | 총 소요 | 바이트 사이 최대 침묵 |
|---|---|---|
| 예전 (`application/json`) | 25.1s | **25.1s** — 생성 시간만큼 그대로 침묵 |
| 지금 (`application/x-ndjson`) | 25.1s | **10.1s** — ping 간격에서 멈춘다 |

침묵이 ping 간격에 고정되므로 **생성이 몇 분이 걸리든 504 가 나지 않는다.**
nginx 설정을 건드릴 수 없는 환경을 위한 장치다.

**2. 실제 생성 시간 자체를 줄인다.** 눈에 보이지 않는 추론 토큰이 응답 시간의
대부분을 차지한다. 기본 추론 강도를 `high` → `medium` 으로 낮췄다. 이 프롬프트는
MCP 가 AST 로 확정한 변경 단위·영향 그래프·기준 패키지·실제 구현 본문을 이미 다
넘겨 주므로 모델이 스스로 알아내야 할 것이 남아 있지 않다 — high 의 추가 숙고는
테스트 품질보다 대기 시간에 먼저 쓰인다. 되돌리려면 `CODETEST_LLM_EFFORT=high`.

설명 섹션(THINKING/근거)에는 줄 수 상한을 뒀고 **TEST_CODE 에는 두지 않았다.**
근거 문장이 길어져 봐야 대기 시간만 늘지만, 테스트 코드를 줄이면 import·필드가
빠져 컴파일이 깨진다.

## 테스트

```bash
python -m pytest tests/ -q
```

LLM 을 스텁으로 대체해 **Agent 가 코드 기반 작업을 직접 하지 않는지**(= MCP 가 준
사실만 쓰는지)와 정의서가 요구하는 판단 결과가 나오는지를 검증한다.
