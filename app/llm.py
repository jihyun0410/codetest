"""OpenAI 호출 + 응답 섹션 파서.

- 모델 / 추론 강도 / 토큰 상한은 전부 .env 로 주입한다 (app.config.Settings).
- 안전 거부는 message.refusal 과 finish_reason == "content_filter" 둘 다에서 확인한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.config import get_logger, settings

logger = get_logger(__name__)

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
    """프로세스 전역에서 재사용하는 OpenAI 클라이언트."""

    def __init__(self) -> None:
        self._client = None

    def _ensure_client(self):
        """지연 초기화. 키가 없으면 SDK 가 OPENAI_API_KEY 환경변수를 스스로 찾는다."""
        if self._client is not None:
            return self._client
        try:
            import openai
        except ImportError as exc:  # pragma: no cover
            raise LLMUnavailableError("openai SDK 가 설치되어 있지 않습니다.") from exc

        kwargs: dict = {}
        if settings.openai_api_key:
            kwargs["api_key"] = settings.openai_api_key
        # 사내 게이트웨이/Azure 등 OpenAI 호환 엔드포인트. 반드시 명시적으로 넘긴다 —
        # pydantic-settings 는 .env 를 os.environ 에 넣지 않으므로 SDK 가 스스로 못 읽는다.
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        try:
            self._client = openai.OpenAI(**kwargs)
        except Exception as exc:
            raise LLMUnavailableError(f"OpenAI 클라이언트 생성 실패: {exc}") from exc
        logger.info("LLM 엔드포인트 = %s (model=%s)", self._client.base_url, settings.llm_model)
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

        :raises LLMUnavailableError: 클라이언트를 만들 수 없거나 호출이 실패했을 때
        :raises LLMRefusalError:     안전 분류기가 거부했을 때
        """
        client = self._ensure_client()

        # ponytail: 비스트리밍. max_completion_tokens 를 크게 잡으면 응답이 길어지므로
        #           타임아웃이 실제로 문제되면 그때 stream=True 로 바꾼다.
        completion = self._create(client, {
            "model": settings.llm_model,
            "max_completion_tokens": settings.llm_max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "reasoning_effort": settings.llm_effort,
        })

        choice = completion.choices[0]
        # 본문을 읽기 전에 거부 여부를 먼저 확인한다.
        refusal = getattr(choice.message, "refusal", None)
        if refusal or choice.finish_reason == "content_filter":
            raise LLMRefusalError("content_filter", refusal or "안전 필터에 의해 차단되었습니다.")

        usage = completion.usage
        details = getattr(usage, "prompt_tokens_details", None)
        return LLMResponse(
            text=(choice.message.content or "").strip(),
            model=getattr(completion, "model", None) or settings.llm_model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            cache_read_tokens=getattr(details, "cached_tokens", 0) or 0,
            stop_reason=choice.finish_reason,
            meta={"effort": settings.llm_effort},
        )

    def _create(self, client, kwargs: dict):
        """호출 1건. 실패하면 **무엇을 고쳐야 하는지** 구분해서 알린다.

        연결 오류와 키/모델 오류는 원인도 조치도 전혀 다르다. 예전에는 어떤 예외든
        reasoning_effort 탓으로 보고 재시도해, 연결이 막힌 경우에도 두 번 호출한 뒤
        "OpenAI 호출에 실패했습니다: Connection error" 만 남겼다.
        """
        import openai

        try:
            return client.chat.completions.create(**kwargs)

        except openai.BadRequestError as exc:
            # reasoning_effort 는 추론 모델 전용 — 그것 때문에 거부당했을 때만 빼고 재시도한다.
            if "reasoning_effort" in kwargs and "reasoning_effort" in str(exc):
                logger.info(
                    "%s 가 reasoning_effort 를 받지 않음 — 제거 후 재시도", settings.llm_model
                )
                return self._create(
                    client, {k: v for k, v in kwargs.items() if k != "reasoning_effort"}
                )
            raise LLMUnavailableError(f"OpenAI 가 요청을 거부했습니다 (400): {exc}") from None

        except openai.APIConnectionError as exc:
            # 요청이 서버에 닿지도 못한 경우. 키/모델이 틀렸다면 401/404 로 온다.
            raise LLMUnavailableError(
                f"OpenAI 에 연결하지 못했습니다: {exc}\n"
                f"  · 요청 주소: {client.base_url}\n"
                f"  · 사내망이면 OPENAI_BASE_URL 로 게이트웨이 주소를 지정하세요.\n"
                f"  · 프록시가 필요하면 HTTPS_PROXY 를 설정하세요.\n"
                f"  · 키나 모델이 틀린 경우라면 이 오류가 아니라 401/404 가 옵니다."
            ) from None

        except openai.AuthenticationError as exc:
            raise LLMUnavailableError(
                f"인증에 실패했습니다 (401) — OPENAI_API_KEY 를 확인하세요.\n"
                f"  · 요청 주소: {client.base_url}\n"
                f"  · 사내 게이트웨이(LiteLLM 등)라면 그 게이트웨이가 발급한 키여야 합니다.\n"
                f"    OpenAI 원본 키를 넣으면 프록시가 자기 DB 에서 못 찾아 401 을 돌려줍니다.\n"
                f"  · OS 환경변수가 .env 보다 우선합니다 — 예전 키가 export 되어 있는지 확인하세요.\n"
                f"  ({exc})"
            ) from None

        except openai.NotFoundError as exc:
            raise LLMUnavailableError(
                f"모델을 찾지 못했습니다 (404) — CODETEST_LLM_MODEL={settings.llm_model} 과 "
                f"요청 주소({client.base_url})를 확인하세요: {exc}"
            ) from None

        except Exception as exc:
            raise LLMUnavailableError(f"OpenAI 호출에 실패했습니다: {exc}") from None


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
