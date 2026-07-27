"""결정론적 의도/중요도 규칙 (baseline · 폴백 · 병합).

Since intent analysis and test generation are served by a **single** LLM call,
this is no longer a pipeline stage. It plays two roles instead:

* it produces the deterministic baseline analysis (``analyze_unit``) that the
  mock backend returns as part of that one call, and that any real backend
  falls back to for units the model did not label;
* it merges the analyses coming back from that call onto the change units
  (``apply_analyses``), which is what the report renders as
  [의도/중요도 분석 근거].

The rules stay transparent on purpose: an explainable label beats a guessed one.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Tuple

from ..models import ChangeUnit, UnitAnalysis

_CONDITION_TOKENS = ("if", "else", "switch", "case", "&&", "||", "?", "==", "!=", ">=", "<=")
_PERF_TOKENS = ("cache", "parallel", "stream", "async", "@Cacheable", "batch",
                "index", "pool", "buffer", "lazy", "CompletableFuture")
_FEATURE_TOKENS = ("public ", "@GetMapping", "@PostMapping", "@RequestMapping",
                   "class ", "interface ", "new endpoint", "@Bean")

VALID_INTENTS = ("feature", "condition", "performance", "modification")
VALID_IMPORTANCE = ("High", "Mid", "Low")


def _count_tokens(lines: Sequence[str], tokens: Tuple[str, ...]) -> int:
    joined = "\n".join(lines)
    return sum(joined.count(t) for t in tokens)


def classify_intent(unit: ChangeUnit) -> Tuple[str, str]:
    """Return (intent, human-readable reason) for a change unit."""
    added, removed = unit.added_lines, unit.removed_lines

    feat = _count_tokens(added, _FEATURE_TOKENS)
    cond = _count_tokens(added, _CONDITION_TOKENS)
    perf = _count_tokens(added, _PERF_TOKENS)

    if unit.is_new_file or unit.is_new_method or feat >= 2:
        return ("feature", "새 메서드/엔드포인트/클래스가 추가되어 기능 추가로 판단됨")
    if perf >= 1 and perf >= cond:
        hit = next((t for t in _PERF_TOKENS if any(t in l for l in added)), "성능 관련 토큰")
        return ("performance", f"성능 관련 요소('{hit}')가 도입되어 성능 개선으로 판단됨")
    if cond >= 1:
        return ("condition", "조건/분기 로직이 변경되어 조건 변경으로 판단됨")
    if added and not removed:
        return ("feature", "코드가 추가되기만 하여 기능 추가로 판단됨")
    return ("modification", "일반 로직 수정으로 판단됨")


def score_importance(unit: ChangeUnit, intent_kind: str = "") -> Tuple[str, str]:
    """High / Mid / Low based on scope, visibility and change size, with a reason."""
    kind = intent_kind or unit.intent
    path = unit.file_path.lower()
    score = 0
    factors: List[str] = []

    if "controller" in path:
        score += 3          # public HTTP surface
        factors.append("외부에 노출되는 Controller 계층(+3)")
    if "service" in path:
        score += 2          # business logic
        factors.append("핵심 비즈니스 로직인 Service 계층(+2)")
    if unit.method and "public" in (unit.method.modifiers or []):
        score += 2
        factors.append("public 메서드로 외부 계약에 영향(+2)")
    if kind in ("feature", "condition"):
        score += 2
        factors.append(f"의도가 '{kind}'로 동작 변화가 큼(+2)")
    elif kind == "performance":
        score += 1
        factors.append("성능 개선으로 결과 동치성 확인 필요(+1)")

    churn = len(unit.added_lines) + len(unit.removed_lines)
    if churn >= 20:
        score += 2
        factors.append(f"변경 규모가 큼({churn}줄, +2)")
    elif churn >= 6:
        score += 1
        factors.append(f"변경 규모가 보통({churn}줄, +1)")

    if unit.context and unit.context.dependency_beans:
        factors.append("의존 Bean " + ", ".join(unit.context.dependency_beans) + "에 파급 가능")

    level = "High" if score >= 6 else "Mid" if score >= 3 else "Low"
    reason = f"점수 {score} → {level} · " + (" / ".join(factors) if factors else "특이 요인 없음")
    return level, reason


def analyze_unit(unit: ChangeUnit) -> UnitAnalysis:
    """Deterministic baseline analysis for one unit (intent + importance + 근거)."""
    kind, reason = classify_intent(unit)
    level, importance_reason = score_importance(unit, kind)
    return UnitAnalysis(
        unit_key=unit.display_name, intent=kind, intent_reason=reason,
        importance=level, importance_reason=importance_reason,
    )


def baseline_analyses(units: Iterable[ChangeUnit]) -> List[UnitAnalysis]:
    return [analyze_unit(u) for u in units]


def normalize(analysis: UnitAnalysis, unit: ChangeUnit) -> UnitAnalysis:
    """Clamp an LLM-provided analysis to the labels the report understands."""
    if analysis.intent not in VALID_INTENTS:
        analysis.intent = "modification"
    if analysis.importance not in VALID_IMPORTANCE:
        analysis.importance = score_importance(unit, analysis.intent)[0]
    if not analysis.unit_key:
        analysis.unit_key = unit.display_name
    return analysis


def apply_analyses(
    units: Sequence[ChangeUnit], analyses: Sequence[UnitAnalysis]
) -> List[UnitAnalysis]:
    """Merge the single call's [의도/중요도 분석 근거] onto the change units.

    Units the model skipped fall back to the deterministic baseline so a
    partial response never leaves a unit unlabeled.
    """
    by_key: Dict[str, UnitAnalysis] = {a.unit_key: a for a in analyses if a.unit_key}
    applied: List[UnitAnalysis] = []

    for unit in units:
        analysis = by_key.get(unit.display_name) or analyze_unit(unit)
        analysis = normalize(analysis, unit)
        unit.intent = analysis.intent
        unit.intent_reason = analysis.intent_reason
        unit.importance = analysis.importance
        unit.importance_reason = analysis.importance_reason
        applied.append(analysis)
    return applied
