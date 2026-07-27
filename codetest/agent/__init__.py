"""[계층 2] Agent Core — 의사결정, 파이프라인, LLM 제어.

The agent decides *which* MCP tools to call and how to stitch their answers
together; the tools themselves stay ignorant of ChangeUnits and of each other.
It owns the two judgments in the system: the single analyze+generate call
(:mod:`.prompt_engine`) and the validity verdict on a test result
(:mod:`.pipeline`).
"""
from __future__ import annotations

from . import intent_rules, pipeline, prompt_engine
from .change_analyzer import ChangeAnalyzer, analyze_changes
from .llm import LLMClient, MockLLMClient, TestGenRequest, get_client

__all__ = [
    "pipeline",
    "prompt_engine",
    "intent_rules",
    "ChangeAnalyzer",
    "analyze_changes",
    "LLMClient",
    "MockLLMClient",
    "TestGenRequest",
    "get_client",
]
