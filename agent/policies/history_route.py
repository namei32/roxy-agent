from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast

from agent.llm_json import load_json_object_loose
from agent.model_runtime.call_trace import model_call_purpose
from agent.looping.constants import _FLOW_SEQUENCE_PATTERN, _FLOW_TRIGGER_WORDS

RouteDecisionSource = Literal["heuristic", "llm", "fallback"]
RouteDecisionConfidence = Literal["high", "medium", "low"]
QueryRewriteSource = Literal["llm", "fallback", "disabled", "not_applicable"]
RouteDecisionReasonCode = Literal[
    "route_disabled",
    "flow_execution_state",
    "llm_no_retrieve",
    "llm_retrieve",
    "llm_low_confidence_fail_open",
    "llm_exception_fail_open",
    "empty_query_fallback",
]


@dataclass(frozen=True)
class DecisionMeta:
    source: RouteDecisionSource
    confidence: RouteDecisionConfidence
    reason_code: RouteDecisionReasonCode


@dataclass(frozen=True)
class RouteDecision:
    needs_history: bool
    rewritten_query: str
    fail_open: bool
    latency_ms: int
    meta: DecisionMeta
    gate_latency_ms: int = 0
    rewrite_latency_ms: int = 0
    rewrite_source: QueryRewriteSource = "not_applicable"


class HistoryRoutePolicy:
    def __init__(
        self,
        *,
        light_provider: Any,
        light_model: str,
        enabled: bool,
        llm_timeout_ms: int,
        max_tokens: int,
        reasoning_effort: str = "",
        query_rewrite_enabled: bool = True,
        query_rewrite_timeout_ms: int = 3_000,
        query_rewrite_max_tokens: int = 80,
        query_rewrite_reasoning_effort: str = "",
    ) -> None:
        self._light_provider = light_provider
        self._light_model = light_model
        self._enabled = bool(enabled)
        self._llm_timeout_ms = int(llm_timeout_ms)
        self._max_tokens = int(max_tokens)
        self._reasoning_effort = str(reasoning_effort or "").strip()
        self._query_rewrite_enabled = bool(query_rewrite_enabled)
        self._query_rewrite_timeout_ms = int(query_rewrite_timeout_ms)
        self._query_rewrite_max_tokens = int(query_rewrite_max_tokens)
        self._query_rewrite_reasoning_effort = str(
            query_rewrite_reasoning_effort or ""
        ).strip()

    @staticmethod
    def is_flow_execution_state(user_msg: str, metadata: dict[str, object]) -> bool:
        text = user_msg or ""
        if any(word in text for word in _FLOW_TRIGGER_WORDS):
            return True
        if _FLOW_SEQUENCE_PATTERN.search(text):
            return True
        return False

    async def decide(
        self,
        *,
        user_msg: str,
        metadata: dict[str, object],
        recent_history: str = "",
    ) -> RouteDecision:
        start = datetime.now()
        cleaned_user_msg = _strip_multiple_choice_block(user_msg)
        default_query = cleaned_user_msg or user_msg

        if not self._enabled:
            return self._build_decision(
                needs_history=True,
                rewritten_query=default_query,
                fail_open=False,
                meta=DecisionMeta(
                    source="heuristic",
                    confidence="high",
                    reason_code="route_disabled",
                ),
                start=start,
            )

        if self.is_flow_execution_state(user_msg, metadata):
            return self._build_decision(
                needs_history=True,
                rewritten_query=default_query,
                fail_open=False,
                meta=DecisionMeta(
                    source="heuristic",
                    confidence="high",
                    reason_code="flow_execution_state",
                ),
                start=start,
            )

        prompt = self._build_prompt(
            user_msg=default_query, recent_history=recent_history
        )
        gate_start = datetime.now()
        try:
            timeout_s = max(0.1, self._llm_timeout_ms / 1000.0)
            with model_call_purpose("history_gate"):
                resp = await asyncio.wait_for(
                    self._light_provider.chat(
                        messages=[{"role": "user", "content": prompt}],
                        tools=[],
                        model=self._light_model,
                        max_tokens=self._max_tokens,
                        reasoning_effort=self._reasoning_effort or None,
                    ),
                    timeout=timeout_s,
                )
            payload = load_json_object_loose((resp.content or "").strip())
        except Exception:
            gate_latency_ms = self._elapsed_ms(gate_start)
            return await self._finalize_retrieval_decision(
                needs_history=True,
                default_query=default_query,
                recent_history=recent_history,
                fail_open=True,
                meta=DecisionMeta(
                    source="fallback",
                    confidence="low",
                    reason_code="llm_exception_fail_open",
                ),
                start=start,
                gate_latency_ms=gate_latency_ms,
            )
        gate_latency_ms = self._elapsed_ms(gate_start)

        decision = (
            str(payload.get("decision", "")).upper() if payload is not None else ""
        )
        confidence = self._normalize_confidence(
            str(payload.get("confidence", "medium")).lower()
            if payload is not None
            else "low"
        )

        if confidence == "low":
            return await self._finalize_retrieval_decision(
                needs_history=True,
                default_query=default_query,
                recent_history=recent_history,
                fail_open=True,
                meta=DecisionMeta(
                    source="llm",
                    confidence=confidence,
                    reason_code="llm_low_confidence_fail_open",
                ),
                start=start,
                gate_latency_ms=gate_latency_ms,
            )

        needs_history = decision != "NO_RETRIEVE"
        reason_code: RouteDecisionReasonCode = (
            "llm_retrieve" if needs_history else "llm_no_retrieve"
        )
        if not needs_history:
            return self._build_decision(
                needs_history=False,
                rewritten_query=default_query,
                fail_open=False,
                meta=DecisionMeta(
                    source="llm",
                    confidence=confidence,
                    reason_code=reason_code,
                ),
                start=start,
                gate_latency_ms=gate_latency_ms,
            )

        return await self._finalize_retrieval_decision(
            needs_history=True,
            default_query=default_query,
            recent_history=recent_history,
            fail_open=False,
            meta=DecisionMeta(
                source="llm",
                confidence=confidence,
                reason_code=reason_code,
            ),
            start=start,
            gate_latency_ms=gate_latency_ms,
        )

    async def _finalize_retrieval_decision(
        self,
        *,
        needs_history: bool,
        default_query: str,
        recent_history: str,
        fail_open: bool,
        meta: DecisionMeta,
        start: datetime,
        gate_latency_ms: int,
    ) -> RouteDecision:
        rewritten, rewrite_source, rewrite_latency_ms = await self._rewrite_query(
            user_msg=default_query,
            recent_history=recent_history,
        )
        return self._build_decision(
            needs_history=needs_history,
            rewritten_query=rewritten,
            fail_open=fail_open,
            meta=meta,
            start=start,
            gate_latency_ms=gate_latency_ms,
            rewrite_latency_ms=rewrite_latency_ms,
            rewrite_source=rewrite_source,
        )

    async def _rewrite_query(
        self,
        *,
        user_msg: str,
        recent_history: str,
    ) -> tuple[str, QueryRewriteSource, int]:
        if not self._query_rewrite_enabled:
            return user_msg, "disabled", 0
        start = datetime.now()
        prompt = self._build_rewrite_prompt(
            user_msg=user_msg,
            recent_history=recent_history,
        )
        try:
            timeout_s = max(0.1, self._query_rewrite_timeout_ms / 1000.0)
            with model_call_purpose("query_rewrite"):
                resp = await asyncio.wait_for(
                    self._light_provider.chat(
                        messages=[{"role": "user", "content": prompt}],
                        tools=[],
                        model=self._light_model,
                        max_tokens=self._query_rewrite_max_tokens,
                        reasoning_effort=(self._query_rewrite_reasoning_effort or None),
                    ),
                    timeout=timeout_s,
                )
            payload = load_json_object_loose((resp.content or "").strip())
            rewritten = (
                str(payload.get("rewritten_query", "")).strip()
                if payload is not None
                else ""
            )
            if rewritten:
                return rewritten, "llm", self._elapsed_ms(start)
        except Exception:
            pass
        return user_msg, "fallback", self._elapsed_ms(start)

    @staticmethod
    def _normalize_confidence(value: str) -> RouteDecisionConfidence:
        if value in {"high", "medium", "low"}:
            return cast(RouteDecisionConfidence, value)
        return "low"

    @staticmethod
    def _build_prompt(*, user_msg: str, recent_history: str) -> str:
        history_section = (
            f"\n近期对话摘要：\n{recent_history}\n" if recent_history else ""
        )
        return f"""判断当前用户消息是否需要检索历史事件记忆。
{history_section}
当前消息：{user_msg}

规则：
- 闲聊、通识问答、无需历史上下文 -> NO_RETRIEVE
- 涉及历史偏好、过往对话、用户特征 -> RETRIEVE

只返回 JSON：{{"decision":"RETRIEVE|NO_RETRIEVE","confidence":"high|medium|low"}}"""

    @staticmethod
    def _build_rewrite_prompt(*, user_msg: str, recent_history: str) -> str:
        history_section = (
            f"\n近期对话摘要（仅用于消解指代）：\n{recent_history}\n"
            if recent_history
            else ""
        )
        return f"""把当前用户消息改写成适合历史记忆检索的简洁查询。
{history_section}
当前消息：{user_msg}

要求：
- 保留人名、地点、产品、时间、数量及用户实际要查的事实
- 去掉“我之前/之前说过/聊过”等 meta 表述
- 不回答问题，不添加对话中没有的事实

只返回 JSON：{{"rewritten_query":"..."}}"""

    @staticmethod
    def _build_decision(
        *,
        needs_history: bool,
        rewritten_query: str,
        fail_open: bool,
        meta: DecisionMeta,
        start: datetime,
        gate_latency_ms: int = 0,
        rewrite_latency_ms: int = 0,
        rewrite_source: QueryRewriteSource = "not_applicable",
    ) -> RouteDecision:
        latency = HistoryRoutePolicy._elapsed_ms(start)
        return RouteDecision(
            needs_history=needs_history,
            rewritten_query=rewritten_query,
            fail_open=fail_open,
            latency_ms=latency,
            meta=meta,
            gate_latency_ms=gate_latency_ms,
            rewrite_latency_ms=rewrite_latency_ms,
            rewrite_source=rewrite_source,
        )

    @staticmethod
    def _elapsed_ms(start: datetime) -> int:
        return int((datetime.now() - start).total_seconds() * 1000)


_MULTIPLE_CHOICE_SPLIT_PATTERNS = (
    re.compile(r"\n\s*Options:\s*\n", re.IGNORECASE),
    re.compile(r"\n\s*选项[:：]\s*\n"),
)


def _strip_multiple_choice_block(user_msg: str) -> str:
    text = (user_msg or "").strip()
    if not text:
        return ""
    for pattern in _MULTIPLE_CHOICE_SPLIT_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            return text[: match.start()].strip()
    return text
