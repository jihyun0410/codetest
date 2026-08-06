"""Agent 설정 · 로깅 · API Key 인증.

Agent 는 정의서의 "LLM을 사용하여 판단하는 부분" 만 담당한다.
코드 기반 처리(AST/개요/실행)는 MCP 서비스에 FastAPI 로 위임하므로
DB/작업 디렉터리 설정이 없고 대신 MCP 접속 설정을 갖는다.

비밀값(ANTHROPIC_API_KEY, API Key)은 코드에 두지 않고 .env / OS 환경변수로만 주입한다.
"""

from __future__ import annotations

import hmac
import logging
import sys
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    app_name: str = Field(default="Code Test AI Agent", alias="CODETEST_APP_NAME")
    host: str = Field(default="0.0.0.0", alias="CODETEST_HOST")
    port: int = Field(default=8000, alias="CODETEST_PORT")

    #: Local Client 인증용 키 목록. X-API-Key 헤더와 대조한다.
    #: 비어 있으면 인증 비활성화(로컬 개발 편의).
    #: 수정: NoDecode 가 없으면 pydantic-settings 가 env 값을 JSON 으로 먼저 파싱해
    #:      아래 _split_csv 가 돌기도 전에 SettingsError 로 죽는다.
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=list, alias="CODETEST_API_KEYS"
    )

    # --- MCP 서비스 (코드 기반 처리 위임 대상) ---
    #: 정의서: "Fast API를 통해 송/수신하는 방식으로 구현"
    mcp_base_url: str = Field(default="http://localhost:8100", alias="CODETEST_MCP_BASE_URL")
    mcp_api_key: str = Field(default="", alias="CODETEST_MCP_API_KEY")
    mcp_timeout_seconds: float = Field(default=120.0, alias="CODETEST_MCP_TIMEOUT")
    #: Gradle 빌드 + Spring 컨텍스트 기동은 오래 걸린다
    mcp_execute_timeout_seconds: float = Field(
        default=960.0, alias="CODETEST_MCP_EXECUTE_TIMEOUT"
    )

    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    llm_model: str = Field(default="claude-opus-5", alias="CODETEST_LLM_MODEL")
    #: 사고 깊이/토큰 지출 제어. low | medium | high | xhigh | max
    llm_effort: str = Field(default="high", alias="CODETEST_LLM_EFFORT")
    llm_max_tokens: int = Field(default=32000, alias="CODETEST_LLM_MAX_TOKENS")

    @field_validator("api_keys", mode="before")
    @classmethod
    def _split_csv(cls, value):
        """list 타입 환경변수는 "a,b,c" CSV 를 허용한다."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value



@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()


# ---------------------------------------------------------------------------
def setup_logging(level: int = logging.INFO) -> None:
    """uvicorn 과 충돌하지 않도록 루트 로거에 StreamHandler 를 한 번만 붙인다."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def verify_api_key(provided: str | None) -> bool:
    """settings.api_keys 가 비어 있으면 인증 비활성화. 비교는 타이밍 공격 방지."""
    allowed = settings.api_keys
    if not allowed:
        return True
    if not provided:
        return False
    return any(hmac.compare_digest(provided, key) for key in allowed)
