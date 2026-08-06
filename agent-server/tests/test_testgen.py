"""Test Code 생성 응답 파싱 검증 (LLM 없이 순수 파서만)."""

from __future__ import annotations

from app.testgen import (
    _extract_code,
    _normalize_importance,
    _parse_command,
)

LLM_OUTPUT = """\
## IMPORTANCE
HIGH

## IMPORTANCE_RATIONALE
- 로그인 엔드포인트가 직접 수정됨

## LANGUAGE
python

## RUN_COMMAND
python -m pytest -q {file}

## TEST_CODE
```python
def test_login_returns_none_for_missing_user():
    assert lookup(999) is None
```

## TEST_RATIONALE
- 반환 타입 변경으로 None 경로가 새로 생겼기 때문
"""


def test_sections_parse():
    from app.llm import split_sections

    sections = split_sections(LLM_OUTPUT)
    assert sections["IMPORTANCE"].strip() == "HIGH"
    assert sections["LANGUAGE"].strip() == "python"
    assert "pytest" in sections["RUN_COMMAND"]


def test_extract_code_strips_fence():
    from app.llm import split_sections

    code = _extract_code(split_sections(LLM_OUTPUT)["TEST_CODE"])
    assert code.startswith("def test_login_returns_none_for_missing_user():")
    assert "```" not in code


def test_parse_command_keeps_file_placeholder():
    assert _parse_command("python -m pytest -q {file}") == [
        "python", "-m", "pytest", "-q", "{file}"
    ]
    # 자리표시자가 없으면 끝에 붙여 준다 (실행 시 파일 경로가 반드시 필요)
    assert _parse_command("pytest -q")[-1] == "{file}"
    assert _parse_command("") is None


def test_normalize_importance():
    assert _normalize_importance("HIGH", "LOW") == "HIGH"
    assert _normalize_importance("MEDIUM", "LOW") == "MID"       # MEDIUM → MID
    assert _normalize_importance("알 수 없음", "MID") == "MID"    # 파싱 실패 시 그래프 값
