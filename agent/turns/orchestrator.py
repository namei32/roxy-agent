from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import uuid4

from agent.turns.outbound import OutboundDispatch, OutboundPort
from agent.turns.result import TurnResult, TurnSideEffect
from bus.events import DeliveryStatus

if TYPE_CHECKING:
    from agent.core.runtime_support import SessionLike
    from agent.looping.ports import SessionServices

logger = logging.getLogger("agent.turn_orchestrator")


@dataclass
class TurnOrchestratorDeps:
    session: SessionServices
    outbound: OutboundPort


class TurnOrchestrator:
    def __init__(self, deps: TurnOrchestratorDeps) -> None:
        self._session = deps.session
        self._outbound = deps.outbound

    async def handle_proactive_turn(
        self,
        *,
        result: TurnResult,
        session_key: str,
        channel: str,
        chat_id: str,
    ) -> bool:
        # 1. proactive 先处理 skip：不发消息，只跑 skip 路径副作用。
        if result.decision == "skip":
            await self._run_side_effects(result)
            return False

        if result.outbound is None:
            raise ValueError("proactive reply result requires outbound")

        if channel == "mobile" and result.outbound.context_started_at is not None:
            reason = self._session.session_manager.control_store.proactive_send_guard(
                target=session_key, since=result.outbound.context_started_at,
                origin=result.outbound.origin_session_key, references=result.outbound.context_references)
            if reason:
                logger.info("proactive send deferred: %s", reason)
                result.decision = "skip"
                if result.trace is not None:
                    result.trace.extra["skip_reason"] = reason
                return False

        owner_session_key = session_key
        if channel == "mobile":
            origin = result.outbound.origin_session_key
            if origin is not None:
                if not origin.startswith("mobile:") or not self._session.session_manager.session_exists(origin):
                    raise ValueError("主动消息的来源会话不存在或渠道不匹配")
                session_key = origin
            else:
                activity = result.outbound.activity_key
                session_key = self._session.session_manager.control_store.resolve_proactive_session(
                    owner_session_key=owner_session_key,
                    route_key=f"activity:{activity}" if activity else "inbox",
                    title=result.outbound.activity_title if activity else "Roxy 来信",
                )
            chat_id = session_key.removeprefix("mobile:")

        admission_id = None
        if channel == "mobile":
            try:
                _, admission_id = self._session.session_manager.admit_existing(session_key)
            except KeyError:
                return False
        try:
            content = result.outbound.content
            media = list(result.outbound.media or [])
            delivery_id = uuid4().hex
            control_turn_id = f"turn:{uuid4().hex}"
            receipt = None
            try:
                # 2. 先执行发送前 side_effects，再真正 dispatch 到 outbound。
                await self._run_effects(result.side_effects)
                receipt = await self._outbound.dispatch(
                    OutboundDispatch(
                        channel=channel,
                        chat_id=chat_id,
                        content=content,
                        metadata={"delivery_id": delivery_id, "_canonical_history_owner": "turn_orchestrator", **(
                            {"_proactive_context_guard": {"target": owner_session_key, "destination": session_key, "since": result.outbound.context_started_at,
                             "origin": result.outbound.origin_session_key, "references": result.outbound.context_references}}
                            if channel == "mobile" and result.outbound.context_started_at is not None else {})},
                        media=media,
                        control_turn_id=control_turn_id,
                    )
                )
            except Exception as e:
                logger.exception("proactive outbound dispatch failed: %s", e)

            # 3. 只有用户真正收到后，才把 proactive 消息写入可见会话历史。
            sent = receipt is not None and receipt.status is DeliveryStatus.SUCCESS
            if sent:
                assert receipt is not None
                if channel == "mobile":
                    await self._session.session_manager.append_delivered_message(
                        session_key, content, media=list(receipt.canonical_media),
                        metadata=self._proactive_metadata(result, delivery_id, control_turn_id))
                else:
                    session = self._session.session_manager.get_or_create(session_key)
                    self._persist_proactive_session(
                        session=session,
                        content=content,
                        media=list(receipt.canonical_media),
                        result=result,
                        delivery_id=delivery_id,
                        control_turn_id=control_turn_id,
                    )
                    await self._session.session_manager.append_messages(
                        session, session.messages[-1:]
                    )
                if self._session.presence:
                    self._session.presence.record_proactive_sent(session_key)
                await self._run_effects(result.success_side_effects)
            else:
                await self._run_effects(result.failure_side_effects)

            return sent
        finally:
            if admission_id is not None:
                self._session.session_manager.release_admission(admission_id)

    async def _run_side_effects(self, result: TurnResult) -> None:
        await self._run_effects(result.side_effects)

    async def _run_effects(self, effects: list[TurnSideEffect]) -> None:
        for effect in effects:
            try:
                await effect.run()
            except Exception as e:
                logger.warning("turn side effect failed: %s", e)

    def _persist_proactive_session(
        self,
        *,
        session: SessionLike,
        content: str,
        media: list[str],
        result: TurnResult,
        delivery_id: str,
        control_turn_id: str,
    ) -> None:
        _ = session.add_message("assistant", content, media=media if media else None,
                                **self._proactive_metadata(result, delivery_id, control_turn_id))

    @staticmethod
    def _proactive_metadata(result: TurnResult, delivery_id: str, control_turn_id: str) -> dict[str, object]:
        source_refs = []
        state_summary_tag = "none"
        if result.trace is not None and isinstance(result.trace.extra, dict):
            raw_refs = result.trace.extra.get("source_refs", [])
            if isinstance(raw_refs, list):
                source_refs = [ref for ref in raw_refs if isinstance(ref, dict)]
            state_summary_tag = str(result.trace.extra.get("state_summary_tag", "none"))
        return {
            "proactive": True, "delivery_id": delivery_id, "control_turn_id": control_turn_id,
            "tools_used": ["message_push"], "evidence_item_ids": [str(item_id) for item_id in result.evidence],
            "source_refs": source_refs, "state_summary_tag": state_summary_tag,
            "context_source": {"session_id": result.outbound.origin_session_key, "references": result.outbound.context_references} if result.outbound else None,
        }
