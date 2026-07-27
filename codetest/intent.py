"""Change-intent classification and importance scoring.

The spec requires the agent to decide whether a change is a feature
addition, a condition change, a performance improvement, etc., and to
surface the rationale in the report. This module implements a transparent,
rule-based classifier over the diff (no LLM needed for the label itself,
which keeps it deterministic and explainable).
"""
from __future__ import annotations

import re
from typing import List, Tuple

from .git_analyzer import FileDiff
from .models import ChangeUnit, MethodInfo

_CONDITION_TOKENS = ("if", "else", "switch", "case", "&&", "||", "?", "==", "!=", ">=", "<=")
_PERF_TOKENS = ("cache", "parallel", "stream", "async", "@Cacheable", "batch",
                "index", "pool", "buffer", "lazy", "CompletableFuture")
_FEATURE_TOKENS = ("public ", "@GetMapping", "@PostMapping", "@RequestMapping",
                   "class ", "interface ", "new endpoint", "@Bean")


def _count_tokens(lines: List[str], tokens: Tuple[str, ...]) -> int:
    joined = "\n".join(lines)
    return sum(joined.count(t) for t in tokens)


def classify_intent(diff: FileDiff, is_new_method: bool) -> Tuple[str, str]:
    """Return (intent, human-readable reason)."""
    added = diff.added_lines
    removed = diff.removed_lines

    feat = _count_tokens(added, _FEATURE_TOKENS)
    cond = _count_tokens(added, _CONDITION_TOKENS)
    perf = _count_tokens(added, _PERF_TOKENS)

    if diff.is_new_file or is_new_method or feat >= 2:
        return ("feature", "새 메서드/엔드포인트/클래스가 추가되어 기능 추가로 판단됨")
    if perf >= 1 and perf >= cond:
        hit = next((t for t in _PERF_TOKENS if any(t in l for l in added)), "성능 관련 토큰")
        return ("performance", f"성능 관련 요소('{hit}')가 도입되어 성능 개선으로 판단됨")
    if cond >= 1:
        return ("condition", "조건/분기 로직이 변경되어 조건 변경으로 판단됨")
    if added and not removed:
        return ("feature", "코드가 추가되기만 하여 기능 추가로 판단됨")
    return ("modification", "일반 로직 수정으로 판단됨")


def score_importance(unit: ChangeUnit, diff: FileDiff) -> str:
    """High / Mid / Low based on scope, visibility and change size."""
    score = 0
    path = diff.path.lower()

    if "controller" in path:
        score += 3          # public HTTP surface
    if "service" in path:
        score += 2          # business logic
    if unit.method and "public" in (unit.method.modifiers or []):
        score += 2
    if unit.intent in ("feature", "condition"):
        score += 2
    if unit.intent == "performance":
        score += 1
    churn = len(diff.added_lines) + len(diff.removed_lines)
    if churn >= 20:
        score += 2
    elif churn >= 6:
        score += 1

    if score >= 6:
        return "High"
    if score >= 3:
        return "Mid"
    return "Low"
