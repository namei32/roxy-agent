from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.policies.history_route import HistoryRoutePolicy


class _Provider:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return SimpleNamespace(content=response)


def _policy(provider: _Provider) -> HistoryRoutePolicy:
    return HistoryRoutePolicy(
        light_provider=provider,
        light_model="luna",
        enabled=True,
        llm_timeout_ms=60_000,
        max_tokens=150,
        reasoning_effort="low",
        query_rewrite_enabled=True,
        query_rewrite_timeout_ms=60_000,
        query_rewrite_max_tokens=80,
        query_rewrite_reasoning_effort="medium",
    )


@pytest.mark.asyncio
async def test_history_gate_and_query_rewrite_use_separate_efforts() -> None:
    provider = _Provider(
        '{"decision":"RETRIEVE","confidence":"high"}',
        '{"rewritten_query":"Edgewater hotel choice"}',
    )

    decision = await _policy(provider).decide(
        user_msg="Which hotel did I choose before?",
        metadata={},
    )

    assert decision.needs_history is True
    assert decision.rewritten_query == "Edgewater hotel choice"
    assert decision.rewrite_source == "llm"
    assert len(provider.calls) == 2
    assert provider.calls[0]["reasoning_effort"] == "low"
    assert provider.calls[0]["max_tokens"] == 150
    assert provider.calls[1]["reasoning_effort"] == "medium"
    assert provider.calls[1]["max_tokens"] == 80


@pytest.mark.asyncio
async def test_no_retrieve_skips_query_rewrite() -> None:
    provider = _Provider('{"decision":"NO_RETRIEVE","confidence":"high"}')

    decision = await _policy(provider).decide(
        user_msg="What is two plus two?",
        metadata={},
    )

    assert decision.needs_history is False
    assert decision.rewrite_source == "not_applicable"
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_query_rewrite_failure_falls_back_without_blocking_retrieval() -> None:
    provider = _Provider(
        '{"decision":"RETRIEVE","confidence":"high"}',
        RuntimeError("rewrite unavailable"),
    )

    decision = await _policy(provider).decide(
        user_msg="What did I choose before?",
        metadata={},
    )

    assert decision.needs_history is True
    assert decision.fail_open is False
    assert decision.rewritten_query == "What did I choose before?"
    assert decision.rewrite_source == "fallback"
