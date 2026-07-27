"""LLM 통신 모듈.

The pipeline depends only on :class:`LLMClient` — and on exactly one method of
it, :meth:`~LLMClient.analyze_and_generate` — so an entire run costs a single
API call. The deterministic mock can be swapped for a real Claude client
without touching any pipeline stage.
"""
from __future__ import annotations

from .base_client import LLMClient, TestGenRequest
from .mock_client import MockLLMClient


def get_client(backend: str = "mock") -> LLMClient:
    backend = (backend or "mock").lower()
    if backend == "mock":
        return MockLLMClient()
    if backend == "claude":
        # Placeholder for the real Anthropic-backed client. Kept out of the MVP
        # (no network/key dependency); add `codetest.agent.llm.claude_client`
        # here later. It only needs analyze_and_generate() — one request that
        # returns 의도/중요도 분석 근거 + 테스트 코드 together, built and parsed
        # by codetest.agent.prompt_engine.
        raise NotImplementedError(
            "claude backend is not wired yet — run with CODETEST_LLM=mock (default)."
        )
    raise ValueError(f"unknown LLM backend: {backend}")


__all__ = ["LLMClient", "TestGenRequest", "MockLLMClient", "get_client"]
