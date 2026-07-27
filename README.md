# Code Test AI Agent (`codetest`)

로컬 터미널에서 실행하는 **CLI 기반 AI 테스트 에이전트**입니다. Spring Boot 프로젝트의
변경된 코드를 감지하여, 변경 의도를 분석하고, 추론(chain-of-thought)을 거쳐
`@SpringBootTest` 테스트 코드를 자동 생성한 뒤, JaCoCo와 함께 실행하고 결과를
Terminal UI로 리포트합니다.

> 요구사항 정의서(`code-test.txt`)의 워크플로우를 구현한 MVP입니다.

## 워크플로우

```
Terminal 명령 입력
   └─▶ 1. 변경 소스 조회        (Git Diff: working-tree / staged)
       2. 변경 내용·영향도·메서드 분석  (JavaParser 계열 AST + 의도 분류) → DB 저장
       3. 추론 후 테스트 코드 생성   (LLM 추상화 레이어, 기본 mock)
       4. @SpringBootTest + JaCoCo 로 실행 → 리포트
```

## 아키텍처

| 모듈 | 역할 |
|------|------|
| `git_analyzer.py` | working-tree/staged 변경 Java 파일 및 diff 추출 |
| `ast_analyzer.py` | `javalang` 기반 AST 파싱 (실패 시 regex 폴백), 변경 라인↔메서드 매핑 |
| `intent.py`       | 변경 의도 분류(기능추가/조건변경/성능개선/수정) + 중요도(High/Mid/Low) 점수화 |
| `db.py`           | 탐색된 프로젝트 feature를 SQLite에 저장 |
| `llm/`            | LLM 추상화(`LLMClient`) + 결정론적 `MockLLMClient` (추론 + 테스트 생성) |
| `analyzer.py`     | git+AST+intent → `ChangeUnit` 조립 |
| `generator.py`    | 변경 유닛을 하나의 비즈니스 흐름 테스트로 묶어 생성 |
| `runner.py`       | Gradle `@SpringBootTest`+JaCoCo 실행 (미설치 시 시뮬레이션) |
| `ui.py`           | Rich Terminal UI ([결과 예시], Test Code 보기, Test Result 보기) |
| `cli.py`          | Typer CLI 엔트리포인트 |

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
| `codetest features` | AST로 탐색되어 DB에 저장된 feature 목록 확인 |

공통 옵션: `--project/-p <경로>` (대상 프로젝트, 기본 cwd), `--llm mock|claude`,
`--show code|result|all` (비대화형 펼치기), `--no-interactive`.

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
열어두었습니다.

## 테스트

```bash
python -m pytest tests/     # 에이전트 자체의 단위 테스트
```
