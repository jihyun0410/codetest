"""LLM abstraction layer.

The pipeline depends only on the :class:`LLMClient` interface — and on exactly
one method of it, :meth:`~LLMClient.analyze_and_generate`, so an entire
run costs a single API call. The deterministic mock used for the MVP can later
be swapped for a real Claude client without touching any pipeline stage.
"""
from __future__ import annotations

from .base import LLMClient, TestGenRequest
from .mock import MockLLMClient


def get_client(backend: str = "mock") -> LLMClient:
    backend = (backend or "mock").lower()
    if backend == "mock":
        return MockLLMClient()
    if backend == "claude":
        # Placeholder for the real Anthropic-backed client. Kept out of the MVP
        # (no network/key dependency); wire up `codetest.llm.claude` here later.
        # It only needs to implement analyze_and_generate() — one request that
        # returns 의도/중요도 분석 근거 + 테스트 코드 together.
        raise NotImplementedError(
            "claude backend is not wired yet — run with CODETEST_LLM=mock (default)."
        )
    raise ValueError(f"unknown LLM backend: {backend}")


__all__ = ["LLMClient", "TestGenRequest", "MockLLMClient", "get_client"]
