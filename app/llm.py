"""Anthropic Claude 호출 + 응답 섹션 파서.

- 모델: Claude Opus 5, 적응형 사고(adaptive thinking) + effort 로 지출 제어
- 긴 응답의 HTTP 타임아웃을 피하려고 항상 **스트리밍**으로 호출한다
- 안전 분류기 거부(`stop_reason == "refusal"`)는 content 접근 **전에** 확인한다
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.config import get_logger, settings

logger = get_logger(__name__)

#: 서버 사이드 fallback("default" 스칼라)을 여는 베타 플래그
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

#: "## IMPORTANCE" 같은 2단계 헤딩
_SECTION_HEADING = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


class LLMUnavailableError(RuntimeError):
    """API Key 미설정 등으로 호출이 불가능한 상태."""


class LLMRefusalError(RuntimeError):
    """안전 분류기가 요청을 거부한 경우."""

    def __init__(self, category: str | None, explanation: str | None) -> None:
        super().__init__(f"LLM 이 요청을 거부했습니다 (category={category}): {explanation}")
        self.category = category
        self.explanation = explanation


@dataclass
class LLMResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    stop_reason: str | None = None
    meta: dict = field(default_factory=dict)


class LLMClient:
    """프로세스 전역에서 재사용하는 Claude 클라이언트."""

    def __init__(self) -> None:
        self._client = None

    def _ensure_client(self):
        """지연 초기화. API Key 가 없어도 SDK 표준 자격증명 경로가 동작할 수 있다."""
        if self._client is not None:
            return self._client
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailableError("anthropic SDK 가 설치되어 있지 않습니다.") from exc

        kwargs = {"api_key": settings.anthropic_api_key} if settings.anthropic_api_key else {}
        try:
            self._client = anthropic.Anthropic(**kwargs)
        except Exception as exc:
            raise LLMUnavailableError(f"Anthropic 클라이언트 생성 실패: {exc}") from exc
        return self._client

    @property
    def available(self) -> bool:
        try:
            self._ensure_client()
            return True
        except LLMUnavailableError:
            return False

    def complete(self, system: str, user: str) -> LLMResponse:
        """
        생성 1건.

        :raises LLMUnavailableError: 클라이언트를 만들 수 없을 때
        :raises LLMRefusalError:     안전 분류기가 거부했을 때
        """
        client = self._ensure_client()

        # 시스템 프롬프트는 요청마다 동일하므로 프롬프트 캐시를 건다.
        kwargs = {
            "model": settings.llm_model,
            "max_tokens": settings.llm_max_tokens,
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": user}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": settings.llm_effort},
        }
        message = self._stream_with_fallback(client, kwargs)

        # content 를 읽기 전에 반드시 stop_reason 을 확인한다.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusalError(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", "") == "text"
        )
        usage = message.usage
        return LLMResponse(
            text=text.strip(),
            model=getattr(message, "model", settings.llm_model),
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            stop_reason=message.stop_reason,
            meta={"effort": settings.llm_effort},
        )

    def _stream_with_fallback(self, client, kwargs: dict):
        """
        단계적으로 낮춰 가며 스트리밍 호출한다.

          1) 베타 엔드포인트 + 서버 사이드 fallback → 거부 시 다른 모델이 이어받음
          2) 일반 엔드포인트
          3) SDK 가 output_config / thinking 을 모르면 제거 후 재시도
        """
        try:
            with client.beta.messages.stream(
                **kwargs, betas=[_FALLBACK_BETA], fallbacks="default"
            ) as stream:
                return stream.get_final_message()
        except LLMRefusalError:
            raise
        except Exception as exc:
            logger.info("서버 사이드 fallback 미사용(%s) — 일반 경로로 재시도", exc)

        attempts = [
            dict(kwargs),
            {k: v for k, v in kwargs.items() if k != "output_config"},
            {k: v for k, v in kwargs.items() if k not in {"output_config", "thinking"}},
        ]
        last_error: Exception | None = None
        for index, attempt in enumerate(attempts, start=1):
            try:
                with client.messages.stream(**attempt) as stream:
                    return stream.get_final_message()
            except TypeError as exc:
                last_error = exc
                logger.warning("SDK 미지원 파라미터 — 축소 후 재시도(%d/%d): %s",
                               index, len(attempts), exc)
        raise LLMUnavailableError(f"Claude 호출에 실패했습니다: {last_error}")


#: 애플리케이션 전역 싱글턴
llm_client = LLMClient()


def split_sections(markdown: str) -> dict[str, str]:
    """'## 제목' 기준으로 응답 본문을 나눈다."""
    result: dict[str, str] = {}
    matches = list(_SECTION_HEADING.finditer(markdown))
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        result[match.group(1).strip()] = markdown[start:end].strip()
    return result
