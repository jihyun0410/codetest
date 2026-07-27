"""LLM abstraction layer.

The pipeline depends only on the :class:`LLMClient` interface, so the
deterministic mock used for the MVP can later be swapped for a real Claude
API client without touching the reasoning/generation stages.
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
        raise NotImplementedError(
            "claude backend is not wired yet — run with CODETEST_LLM=mock (default)."
        )
    raise ValueError(f"unknown LLM backend: {backend}")


__all__ = ["LLMClient", "TestGenRequest", "MockLLMClient", "get_client"]
