"""reasoning_effort 를 더 이상 보내지 않는지, 그 재시도 로그가 다시는 찍히지 않는지 확인.

과거에는 매 호출마다 reasoning_effort 를 보냈다가 모델이 거부하면 빼고 재시도했다.
그 요청/재시도/로그를 통째로 없앴으므로, 첫 호출부터 reasoning_effort 없이 성공해야 한다.
"""

from __future__ import annotations

import logging

from app.llm import LLMClient


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return "ok"


class _FakeChat:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()


class _FakeClient:
    def __init__(self) -> None:
        self.chat = _FakeChat()


def test_create_never_sends_reasoning_effort_and_logs_nothing(caplog):
    client = _FakeClient()
    llm = LLMClient()

    with caplog.at_level(logging.INFO):
        result = llm._create(client, {"model": "x", "messages": []})

    assert result == "ok"
    assert client.chat.completions.calls == [{"model": "x", "messages": []}]
    assert "reasoning_effort" not in caplog.text
