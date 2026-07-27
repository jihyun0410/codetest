# Code Test AI Agent (`codetest`)

로컬 터미널에서 실행하는 **CLI 기반 AI 테스트 에이전트**입니다. Spring Boot 프로젝트의
변경된 코드를 감지하여, 변경 의도를 분석하고, 추론(chain-of-thought)을 거쳐
`@SpringBootTest` 테스트 코드를 자동 생성한 뒤, JaCoCo와 함께 실행하고 결과를
Terminal UI로 리포트합니다.

> 요구사항 정의서(`code-test.txt`)의 워크플로우를 구현한 MVP입니다.

## 워크플로우

```
Terminal 명령 입력
   └─▶ 1. 변경 소스 조회        (Git Diff: working-tree / staged, 공백·줄바꿈 노이즈 제외)
       2. 변경 메서드 분석       (AST MCP Server가 시그니처·의존 Bean·호출 순서만 필터링해 전달)
       3. 단 1회 LLM 호출        (의도/중요도 분석 근거 + @SpringBootTest 테스트 코드 동시 수신)
       4. @SpringBootTest + JaCoCo 로 실행 → 리포트

   ※ 1~4단계는 모두 메모리상의 변수로 주고받으며, DB를 거치지 않습니다.
```

## 아키텍처

| 모듈 | 역할 |
|------|------|
| `git_analyzer.py` | working-tree/staged 변경 Java 파일 및 diff 추출 (`DiffOptions`로 공백/빈 줄 무시) |
| `ast_analyzer.py` | `javalang` 기반 AST 파싱 (실패 시 regex 폴백), 변경 라인↔메서드 매핑 |
| `ast_context.py`  | AST를 **시그니처 / 의존 Bean 이름 / 호출 순서 요약** 3개 필드로 축약 |
| `mcp/`            | AST MCP Server(`server.py`, JSON-RPC over stdio) + 클라이언트(in-process / stdio) |
| `intent.py`       | 의도·중요도 분류 규칙 (1회 호출의 baseline 분석 + 응답 병합/폴백) |
| `db.py`           | `MemoryFeatureStore`(기본) / `SQLiteFeatureDB`(`--persist`) 동일 인터페이스 |
| `llm/`            | LLM 추상화(`LLMClient.analyze_and_generate` **단일 메서드**) + 결정론적 `MockLLMClient` |
| `analyzer.py`     | git diff + AST + MCP 컨텍스트 → `ChangeUnit`/`ChangeAnalysis` 조립 (메모리 전달) |
| `generator.py`    | 변경 유닛 묶음에 대해 **1회 호출**로 분석+생성, 결과를 유닛에 반영 후 파일 기록 |
| `runner.py`       | Gradle `@SpringBootTest`+JaCoCo 실행 (미설치 시 시뮬레이션) |
| `ui.py`           | Rich Terminal UI ([결과 예시], Test Code 보기, Test Result 보기) |
| `cli.py`          | Typer CLI 엔트리포인트 |

### 1. 단일 API 호출 (분석 + 생성 통합)

`reason()` → `generate_test()` 2회 호출을 `analyze_and_generate()` **1회**로 통합했습니다.
한 번의 응답(`CombinedAnalysis`)이 두 결과물을 동시에 담습니다.

```python
combined = llm.analyze_and_generate(req)   # 유일한 API 호출
combined.analyses      # [의도/중요도 분석 근거]  → 리포트의 의도·중요도·근거
combined.test_source   # [테스트 코드]           → @SpringBootTest 클래스
combined.llm_calls     # 항상 1
```

모델이 일부 유닛을 빠뜨려도 `intent.apply_analyses()`가 규칙 기반 baseline으로
채워 넣기 때문에 라벨이 비는 유닛은 없습니다. 리포트 하단에 실제 호출 횟수가 표시됩니다.

### 2. AST MCP Server

AST/전체 소스를 그대로 넘기지 않고, 서버가 아래 3가지만 필터링해 전달합니다.

| 전달 항목 | 예시 |
|-----------|------|
| 수정된 대상 메서드의 시그니처 | `public double calculateTotal(Order order)` |
| 의존 Bean 클래스 이름 목록 | `DiscountPolicy, OrderRepository` |
| 호출 순서 요약 | `DiscountPolicy.apply() → OrderRepository.save()` |

의존 Bean은 생성자 주입 / `@Autowired`·`@Resource`·`@Inject` / Spring 네이밍 규칙으로
판별하고, 호출 순서에서는 DTO·파라미터의 단순 getter/setter를 제외해 협력 흐름만 남깁니다.

전송 방식은 두 가지이며 결과는 동일합니다.

```bash
codetest run --ast-mcp inprocess   # 기본: 같은 프로세스에서 서버 핸들러 직접 호출
codetest run --ast-mcp stdio       # JSON-RPC 서브프로세스
codetest mcp-serve                 # 외부 MCP 호스트에 등록해서 사용
# MCP 호스트 설정: {"command": "python", "args": ["-m", "codetest.mcp.server"]}
```

노출 툴: `ast_method_context`(단건), `ast_change_context`(배치).

### 3. 메모리 기반 세션 파이프라인

CLI 한 번의 실행 안에서는 DB를 거치지 않고 파이썬 변수로만 데이터를 주고받습니다.
`pipeline.build_report()`가 세션 스토어 하나를 만들어 각 단계에 넘기며, 기본값은
`MemoryFeatureStore`라 `.codetest/features.db` 파일 자체가 생성되지 않습니다.

```bash
codetest run              # 메모리 전용 (기본)
codetest run --persist    # 동일 인터페이스로 SQLite에도 기록 → codetest features 로 조회
```

### 4. 공백·줄바꿈 변경 무시

diff 추출 시 `--ignore-all-space --ignore-space-at-eol --ignore-blank-lines`를 기본 적용하고,
파싱 단계에서도 빈 줄만 추가/삭제된 라인을 제외합니다. 들여쓰기만 바뀐 파일은 아예
분석 대상에서 빠지며, 어떤 파일이 제외됐는지는 리포트에 표시됩니다.

```
• 공백/줄바꿈만 변경되어 제외한 파일: src/main/java/com/example/demo/controller/OrderController.java
```

`--no-ignore-whitespace` 로 끄면 기존처럼 모든 변경을 분석합니다.

## 설치

```bash
cd codetest-agent
python -m pip install -e .
# 또는: python -m pip install -r requirements.txt  (그 후 `python -m codetest.cli`)
```

## 명령어

| 명령 | 설명 |
|------|------|
| `codetest run` | 로컬에서 staging에 올라가지 않은(working-tree) 변경 파일에 대해 테스트 생성+실행+리포트 |
| `codetest run --stage` | staging(git staged) 단계 파일에 대해 테스트 생성+실행+리포트 |
| `codetest generate` | Git Working Tree 변경분에 대해 **테스트 코드만** 생성 |
| `codetest test` | `<project>/src/test/test.txt` 의 테스트를 실행하고 리포트 |
| `codetest features` | `--persist` 로 저장된 feature 목록 확인 |
| `codetest mcp-serve` | AST MCP Server를 stdio로 기동 (외부 MCP 호스트용) |

공통 옵션: `--project/-p <경로>` (대상 프로젝트, 기본 cwd), `--llm mock|claude`,
`--show code|result|all` (비대화형 펼치기), `--no-interactive`.

`run`/`generate` 추가 옵션:

| 옵션 | 기본값 | 설명 |
|------|--------|------|
| `--persist` | off | 세션 데이터를 SQLite에도 기록 (기본은 메모리 전용) |
| `--ignore-whitespace` / `--no-ignore-whitespace` | on | 공백·빈 줄만 바뀐 변경 무시 |
| `--ast-mcp inprocess\|stdio` | `inprocess` | AST MCP Server 연결 방식 |

환경변수로도 지정할 수 있습니다: `CODETEST_LLM`, `CODETEST_PERSIST`,
`CODETEST_IGNORE_WHITESPACE`, `CODETEST_IGNORE_BLANK_LINES`, `CODETEST_AST_MCP`.

> 정의서의 `codetest run - stage` 표기는 `codetest run --stage` (별칭 `-stage`)로 구현했습니다.

## 빠른 시작 (샘플 프로젝트)

`sample-springboot/` 에 대상 Spring Boot 프로젝트가 포함되어 있습니다.

```bash
# 1) 샘플을 git 저장소로 만들고 베이스라인 커밋
cd sample-springboot
git init && git add -A && git commit -m "baseline"

# 2) 소스를 변경 (예: OrderService에 할인 로직 추가) 후
codetest run -p .          # working-tree 변경 감지 → 생성 → (Gradle 있으면)실행 → 리포트
codetest generate -p .     # 생성만
codetest test -p .         # src/test/test.txt 실행
```

Java/Gradle이 없는 환경에서는 실행 단계가 **SIMULATED** 로 표시되며(테스트는 실제로
컴파일/실행되지 않음), 리포트에 실제 실행용 Gradle 명령이 안내됩니다.

## LLM 백엔드

기본값은 결정론적 `mock` 입니다. 실제 Claude 연동은 `codetest/llm/` 에 `claude`
구현을 추가하고 `--llm claude` (또는 `CODETEST_LLM=claude`) 로 전환하도록 인터페이스만
열어두었습니다. 구현해야 할 메서드는 `analyze_and_generate(req) -> CombinedAnalysis`
하나뿐이며, 요청에는 AST MCP가 필터링한 컨텍스트(`req.prompt_context()`)와 변경 라인만
담기므로 프롬프트 크기가 파일 크기와 무관하게 유지됩니다.

## 테스트

```bash
python -m pytest tests/     # 에이전트 자체의 단위 테스트
```
