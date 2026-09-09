from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, cast

from agent.prompting import is_context_frame
from proactive_v2.config import ProactiveConfig
from proactive_v2.presence import PresenceStore
from session.manager import SessionManager


@dataclass
class RecentProactiveMessage:
    content: str
    timestamp: datetime | None = None
    state_summary_tag: str = "none"
    source_refs: list[object] = field(default_factory=list[object])
    session_key: str = ""
    message_id: str = ""


class Sensor:
    def __init__(
        self,
        *,
        cfg: ProactiveConfig,
        sessions: SessionManager,
        presence: PresenceStore | None,
    ) -> None:
        self._cfg = cfg
        self._sessions = sessions
        self._presence = presence

    def target_session_key(self) -> str:
        channel = (self._cfg.default_channel or "").strip()
        chat_id = self._cfg.default_chat_id.strip()
        return f"{channel}:{chat_id}" if channel and chat_id else ""

    def context_view(self, now: datetime | None = None) -> dict[str, Any]:
        return self._sessions.control_store.proactive_context_view(
            channel=self._cfg.default_channel, target=self.target_session_key(),
            now=(now or datetime.now(timezone.utc)).isoformat(),
        )

    def context_candidates(self, query: str = "", *, now: datetime | None = None) -> list[dict[str, Any]]:
        from session.proactive_context import text_terms
        cards = self.context_view(now)["sessions"]
        terms = text_terms(query)
        if terms:
            cards = [{**card, "matched_terms": sorted(terms & text_terms(card['title'] + " " + card['summary']))[:12]} for card in cards]
            cards = sorted(cards, key=lambda c: len(c["matched_terms"]), reverse=True)
        return cards[:8]

    def read_context(self, *, session_id: str | None = None, query: str = "", n: int = 20, now: datetime | None = None) -> dict[str, Any]:
        from session.proactive_context import choose_context, content_digest
        view = self.context_view(now)
        chosen = choose_context(view["sessions"], query=query, session_id=session_id)
        if chosen is None:
            return {"session_id": None, "messages": [], "references": [], "candidates": self.context_candidates(query)}
        rows = self._sessions.control_store.proactive_context_messages(
            session_ids=[chosen["session_id"]], now=view["observed_at"], limit=max(1, min(n, 20)))
        messages = []; references = []; budget = 8000
        for row in reversed(rows):
            if row.get("proactive") or is_context_frame(str(row["content"])):
                continue
            content = str(row["content"])
            excerpt = content[:min(1200, budget)]
            if not excerpt:
                break
            budget -= len(excerpt)
            messages.append({"id": row["id"], "session_id": row["session_key"], "role": row["role"],
                             "content": excerpt, "timestamp": row["timestamp"]})
            references.append({"message_id": row["id"], "session_id": row["session_key"], "sha256": content_digest(content)})
        return {"session_id": chosen["session_id"], "messages": list(reversed(messages)), "references": references + chosen["references"],
                "summary": chosen, "observed_at": view["observed_at"],
                "selection_reason": "explicit_session" if session_id else "keyword_relevance" if query else "recent_user_interaction"}

    def is_busy(self, busy_fn: Any = None) -> bool:
        view = self.context_view()
        if view["busy"]:
            return True
        keys = self._sessions.list_sessions() if self._cfg.default_channel == "mobile" else [{"key": self.target_session_key()}]
        return bool(busy_fn and any(busy_fn(row["key"]) for row in keys if str(row["key"]).startswith(self._cfg.default_channel + ":")))

    def last_proactive_at(self) -> datetime | None:
        return self._parse_timestamp(self.context_view()['last_proactive_at'])

    def last_user_at(self) -> datetime | None:
        if self._cfg.default_channel == "mobile":
            return self._parse_timestamp(self.context_view()["last_user_at"])
        if self._presence is None:
            return None
        return self._presence.get_last_user_at(self.target_session_key())

    def collect_recent(self) -> list[dict[str, object]]:
        """读取并筛选近期用户与助手消息。"""

        if self._cfg.default_channel == "mobile":
            return self.read_context()["messages"]
        # 1. 定位目标会话
        session_key = self.target_session_key()
        if not session_key:
            return []
        session = self._sessions.get_or_create(session_key)
        messages = session.messages[-self._cfg.recent_chat_messages :]

        # 2. 过滤系统上下文并限制注入长度
        results: list[dict[str, object]] = []
        for message in messages:
            if message.get("role") not in ("user", "assistant"):
                continue
            if not message.get("content"):
                continue
            content = str(message.get("content", ""))
            if is_context_frame(content):
                continue
            results.append(
                {
                    "role": message["role"],
                    "content": content[:200],
                    "timestamp": str(message.get("timestamp", "")),
                }
            )
        return results

    def collect_recent_proactive(self, n: int = 5, *, now: datetime | None = None) -> list[RecentProactiveMessage]:
        """按时间顺序返回最近已发送的主动消息。"""

        if self._cfg.default_channel == "mobile":
            view = self.context_view(now)
            rows = self._sessions.control_store.proactive_recent_messages(
                channel=self._cfg.default_channel, target=self.target_session_key(), now=view["observed_at"], limit=max(1, min(n, 64)))
            return [RecentProactiveMessage(content=str(row["content"])[:3000], timestamp=self._parse_timestamp(row["timestamp"]),
                    source_refs=list(row.get("source_refs") or []), session_key=row["session_key"], message_id=row["id"]) for row in rows]
        # 1. 定位目标会话
        session_key = self.target_session_key()
        if not session_key:
            return []
        session = self._sessions.get_or_create(session_key)

        # 2. 从最新消息逆序收集并恢复时间顺序
        results: list[RecentProactiveMessage] = []
        for message in reversed(session.messages):
            if message.get("role") != "assistant":
                continue
            if not message.get("proactive") or not message.get("content"):
                continue
            raw_source_refs = message.get("source_refs")
            results.append(
                RecentProactiveMessage(
                    content=str(message["content"]),
                    timestamp=self._parse_timestamp(message.get("timestamp")),
                    state_summary_tag=str(
                        message.get("state_summary_tag", "none") or "none"
                    ),
                    source_refs=(
                        []
                        if raw_source_refs is None
                        else list(cast(list[object], raw_source_refs))
                    ),
                )
            )
            if len(results) >= n:
                break
        return list(reversed(results))

    @staticmethod
    def _parse_timestamp(raw: object) -> datetime | None:
        text = str(raw or "").strip()
        if not text:
            return None
        try:
            ts = datetime.fromisoformat(text)
        except ValueError:
            return None
        if ts.tzinfo is None:
            return ts.replace(tzinfo=datetime.now().astimezone().tzinfo)
        return ts
