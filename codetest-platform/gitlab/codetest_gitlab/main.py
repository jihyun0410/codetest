"""외부 API 호출 전부.

이 파일 밖에서는 네트워크를 타지 않습니다. 나머지 모듈은 로컬 작업만 합니다
(collector=git diff, executor=gradle, reporter=출력, config=환경변수).

═══════════════════════════════════════════════════════════════════════════
CLI 명령어별 API 호출
═══════════════════════════════════════════════════════════════════════════

codetest-ci doctor                                    설정·연결 점검
  GET  {server}/healthz
       서버 생존 확인. 인증 불필요. 실패해도 잡을 깨지 않음.
       → {"status","version","runs_in_memory","detail"}
  GET  {server}/v1/capabilities
       지원 LLM 백엔드와 MCP 서버 구성(어디서 실행되는지 포함).
       → {"llm_backends","default_llm","mcp_servers":[{key,hosted,tools}]}

codetest-ci analyze                                   수집 → 분석 → 저장
  POST {server}/v1/analyze
       변경 diff + 변경된 파일 소스만 업로드. 서버가 AST 가지치기 후
       LLM을 **1회** 호출해 분석 근거와 테스트 코드를 함께 반환.
       ← {project,changes[{path,source,added/removed_lines,...}],options}
       → {run_id,analyses[],reasoning,contexts[],flow,test{source},llm_calls}
       ※ 공백/줄바꿈만 바뀐 파일은 클라이언트가 걸러서 애초에 보내지 않음.
       ※ CODETEST_OFFLINE=true 면 호출 없이 offline_analyze_response() 사용.

codetest-ci run                                       analyze + 실행 + 회신
  POST {server}/v1/analyze                            (위와 동일)
  POST {server}/v1/runs/{run_id}/result
       Runner에서 gradlew로 돌린 결과를 회신. 서버가 정합성을
       valid/invalid/inconclusive 로 판단해 이력에 기록.
       ← {result{passed,total,failures,coverage_pct,executor,log},...}
       → {run_id,validity,validity_reason,result}
       ※ 실패해도 경고만 — 리포트는 이미 로컬에 남아 있음.
  POST {gitlab}/projects/{id}/merge_requests/{iid}/notes
       MR 코멘트. ⚠️ 기본 dry-run(콘솔 미리보기). 실제 게시는
       CODETEST_POST_NOTE=true + post_note()의 TODO 블록 활성화.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

from .config import CIConfig
from .contracts import AnalyzeRequest, ResultRequest, TestResultDTO

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


class ServerError(RuntimeError):
    """API 호출 실패 — 호출부가 사용자에게 그대로 보여줄 메시지."""


# ═══════════════════════════════════════════════════════════════════════════
# Agent 서버 API
# ═══════════════════════════════════════════════════════════════════════════


class AgentServerClient:
    def __init__(self, cfg: CIConfig, session: Optional[requests.Session] = None):
        self.cfg = cfg
        self.session = session or requests.Session()

    # -- transport --------------------------------------------------------- #
    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "User-Agent": "codetest-gitlab/0.1.0"}
        if self.cfg.token:
            headers["Authorization"] = f"Bearer {self.cfg.token}"
        return headers

    def _request(self, method: str, path: str,
                 payload: Optional[Dict[str, Any]] = None,
                 attempts: int = 3) -> Dict[str, Any]:
        """CI 잡이 서버 장애로 무너지지 않도록 5xx만 지수 백오프로 재시도."""
        if not self.cfg.server_url:
            raise ServerError("CODETEST_SERVER_URL 이 설정되지 않았습니다.")
        url = f"{self.cfg.server_url}{path}"

        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                res = self.session.request(
                    method, url, data=json.dumps(payload) if payload is not None else None,
                    headers=self._headers(), timeout=self.cfg.timeout_s,
                )
            except requests.RequestException as e:
                last_error = f"연결 실패: {e}"
            else:
                if res.status_code < 400:
                    return res.json() if res.content else {}
                detail = _detail(res)
                if res.status_code not in RETRY_STATUS:
                    raise ServerError(f"{method} {path} → {res.status_code}: {detail}")
                last_error = f"{res.status_code}: {detail}"

            if attempt < attempts:
                backoff = 2 ** (attempt - 1)
                log.warning("서버 호출 재시도 %d/%d (%s) — %ss 후", attempt, attempts,
                            last_error, backoff)
                time.sleep(backoff)

        raise ServerError(f"{method} {path} 실패 ({attempts}회 시도): {last_error}")

    # -- endpoints --------------------------------------------------------- #
    def health(self) -> Dict[str, Any]:
        """GET /healthz — doctor. 재시도 없음(진단이므로 즉시 실패가 정보)."""
        return self._request("GET", "/healthz", attempts=1)

    def capabilities(self) -> Dict[str, Any]:
        """GET /v1/capabilities — doctor. 서버가 지원하는 LLM·MCP 구성."""
        return self._request("GET", "/v1/capabilities", attempts=1)

    def analyze(self, req: AnalyzeRequest) -> Dict[str, Any]:
        """POST /v1/analyze — analyze, run. 이 워크플로의 유일한 LLM 호출 지점."""
        if self.cfg.offline:
            return offline_analyze_response(req)
        return self._request("POST", "/v1/analyze", req.to_payload())

    def submit_result(self, run_id: str, req: ResultRequest) -> Dict[str, Any]:
        """POST /v1/runs/{run_id}/result — run. 실행 결과 회신 → 정합성 판단."""
        if self.cfg.offline:
            log.info("[offline] 결과 회신 생략 (run=%s)", run_id)
            return {"run_id": run_id, "offline": True}
        return self._request("POST", f"/v1/runs/{run_id}/result", req.to_payload())


def _detail(res: requests.Response) -> str:
    try:
        body = res.json()
    except ValueError:
        return res.text[:300]
    return str(body.get("detail", body))[:300]


# ═══════════════════════════════════════════════════════════════════════════
# GitLab API — MR 코멘트
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class NoteResult:
    posted: bool
    dry_run: bool = True
    url: str = ""
    detail: str = ""


def note_endpoint(cfg: CIConfig) -> str:
    """POST 대상 URL. CI_API_V4_URL / CI_PROJECT_ID 는 GitLab이 자동 주입."""
    api = cfg.gitlab_api_url or "https://gitlab.example.com/api/v4"
    pid = cfg.numeric_project_id or urllib.parse.quote_plus(cfg.project_id)
    return f"{api.rstrip('/')}/projects/{pid}/merge_requests/{cfg.merge_request_iid}/notes"


def post_note(cfg: CIConfig, body: str) -> NoteResult:
    """POST {gitlab}/.../notes — run.

    ⚠️ 임시(STUB): 기본은 dry-run이라 네트워크를 타지 않고 콘솔에 미리보기만
    출력합니다. URL·payload는 실제 GitLab API 형식 그대로입니다.
    """
    if cfg.merge_request_iid is None:
        return NoteResult(posted=False, detail="MR 파이프라인이 아니라 건너뜁니다.")
    if not cfg.comment_on_mr:
        return NoteResult(posted=False, detail="CODETEST_COMMENT_ON_MR=false")

    url = note_endpoint(cfg)
    dry_run = os.environ.get("CODETEST_POST_NOTE", "").strip().lower() not in (
        "1", "true", "yes", "on")

    if dry_run:
        log.info("[STUB/dry-run] MR 코멘트를 게시하지 않습니다. POST %s", url)
        print("\n----- MR 코멘트 미리보기 -----\n" + body + "\n-----------------------------\n")
        return NoteResult(posted=False, dry_run=True, url=url,
                          detail="dry-run (CODETEST_POST_NOTE=true 로 실제 게시)")

    # ------------------------------------------------------------------ #
    # TODO(임시): 실제 게시 호출
    #
    #   headers = ({"JOB-TOKEN": cfg.gitlab_token} if os.environ.get("CI_JOB_TOKEN")
    #              else {"PRIVATE-TOKEN": cfg.gitlab_token})
    #   res = requests.post(url, json={"body": body}, headers=headers, timeout=15)
    #   res.raise_for_status()
    #   return NoteResult(posted=True, dry_run=False,
    #                     url=res.json().get("web_url", url))
    # ------------------------------------------------------------------ #
    log.warning("[STUB] 실제 게시 코드가 아직 열려 있지 않습니다: %s", url)
    return NoteResult(posted=False, dry_run=False, url=url,
                      detail="stub: post_note()의 TODO 블록을 활성화하세요")


def render_note(response: Dict[str, Any], result: Optional[TestResultDTO],
                cfg: CIConfig) -> str:
    """post_note() 에 실어 보낼 마크다운 본문."""
    lines: List[str] = [
        "### 🤖 codetest — 변경 기반 테스트",
        "",
        f"- 커밋: `{cfg.commit[:8] or 'N/A'}`  ·  run: `{response.get('run_id', 'N/A')}`",
        f"- LLM 호출: {response.get('llm_calls', 0)}회 (의도·중요도 분석 + 코드 생성 동시)",
    ]

    test = response.get("test") or {}
    if test.get("class_name"):
        lines.append(f"- 생성된 테스트: `{test['class_name']}`")

    flow = response.get("flow")
    if flow and flow.get("summary"):
        lines.append(f"- 비즈니스 흐름: `{flow['summary']}`")

    analyses = response.get("analyses") or []
    if analyses:
        lines += ["", "| 변경 유닛 | 의도 | 중요도 | 근거 |", "|---|---|---|---|"]
        for a in analyses:
            reason = str(a.get("intent_reason", "")).replace("|", "\\|")
            lines.append(f"| `{a.get('unit_key')}` | {a.get('intent')} | "
                         f"{a.get('importance')} | {reason} |")

    if result is not None:
        status = "⚪ SIMULATED" if result.executor == "simulated" else (
            "✅ PASS" if result.passed else "❌ FAIL")
        cov = f"{result.coverage_pct}%" if result.coverage_pct is not None else "N/A"
        branch = (f" / 분기 {result.branch_coverage_pct}%"
                  if result.branch_coverage_pct is not None else "")
        lines += [
            "",
            f"**실행 결과: {status}** — total={result.total}, fail={result.failures}, "
            f"error={result.errors}, skip={result.skipped}",
            f"커버리지: {cov}{branch}",
        ]

    for note in response.get("notes") or []:
        lines.append(f"> {note}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 임시(offline) 응답 — 서버 없이 CI 배선만 검증
# ═══════════════════════════════════════════════════════════════════════════


def offline_analyze_response(req: AnalyzeRequest) -> Dict[str, Any]:
    """POST /v1/analyze 를 호출하지 않고 돌려주는 자리 표시자.

    ⚠️ 실제 분석 결과가 아닙니다. 생성되는 테스트도 컴파일만 되는 골격입니다.
    """
    package = req.options.test_package or req.project.package or "com.example.demo"
    first = req.changes[0].path if req.changes else "Unknown.java"
    class_name = first.split("/")[-1].replace(".java", "") or "Unknown"
    test_class = f"{class_name}GeneratedTest"

    return {
        "run_id": "offline-" + (req.project.commit[:8] or "local"),
        "analyses": [{
            "unit_key": class_name,
            "intent": "modification",
            "intent_reason": "[offline] 서버 미연결 — 규칙 분석이 수행되지 않았습니다.",
            "importance": "Mid",
            "importance_reason": "[offline] 자리 표시자 값입니다.",
        }],
        "reasoning": {
            "steps": ["[offline] Agent 서버에 연결하지 않고 응답을 생성했습니다."],
            "scenarios": ["성공: 컨텍스트가 로드된다."],
            "rationale": "[offline] CI 배선 검증용 자리 표시자.",
        },
        "contexts": [],
        "flow": None,
        "test": {
            "package": package,
            "class_name": test_class,
            "source": _offline_test_source(package, test_class),
            "relative_path": f"src/test/java/{package.replace('.', '/')}/{test_class}.java",
        },
        "llm_calls": 0,
        "notes": ["[offline] CODETEST_OFFLINE=true — 서버를 호출하지 않았습니다."],
    }


def _offline_test_source(package: str, test_class: str) -> str:
    return f"""package {package};

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

/** PLACEHOLDER — generated offline (CODETEST_OFFLINE=true). */
@SpringBootTest
class {test_class} {{

    @Test
    void contextLoads() {{
        // TODO: Agent 서버 연결 후 실제 시나리오로 대체됩니다.
    }}
}}
"""
