"""설정 · 로깅 · API Key 인증.

비밀값(ANTHROPIC_API_KEY, API Key)은 코드에 두지 않고 .env / OS 환경변수로만 주입한다.
"""

from __future__ import annotations

import hmac
import logging
import sys
from functools import lru_cache
from pathlib import Path
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

    database_url: str = Field(
        default="sqlite:///./data/codetest.db", alias="CODETEST_DATABASE_URL"
    )
    #: 대상 프로젝트를 clone 해 두는 작업 디렉터리
    workspace_dir: Path = Field(default=Path("./workspace"), alias="CODETEST_WORKSPACE_DIR")

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

    def ensure_directories(self) -> None:
        """기동 시 필요한 런타임 디렉터리를 만든다."""
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite"):
            Path(self.database_url.split("///")[-1]).parent.mkdir(parents=True, exist_ok=True)


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
