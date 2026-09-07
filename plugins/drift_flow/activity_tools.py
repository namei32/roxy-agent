"""仅当前 Drift 执行可以显式保存面向用户的成果。"""

from __future__ import annotations

import json
from typing import Any

from agent.tools.base import Tool
from plugins.default_proactive.context import AgentTickContext
from plugins.drift_flow.activity_store import DriftActivityStore


class LeaveArtifactTool(Tool):
    def __init__(self, ctx: AgentTickContext, store: DriftActivityStore) -> None:
        self._ctx = ctx
        self._store = store

    @property
    def name(self) -> str:
        return "leave_artifact"

    @property
    def description(self) -> str:
        return (
            "把本次活动中真正完成、适合用户阅读的札记、小作品或清单保存到日常。"
            "只保存公开成品，不保存内部推理、原始工具输出、秘密或未经整理的记忆。"
            "成果不可覆盖，每次活动最多 8 份，单份正文最多 32 KiB。"
            "此工具不发送消息；要分享时仍使用既有 message_push，并以实际送达为准。"
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {
            "title": {"type": "string", "maxLength": 80},
            "kind": {"type": "string", "enum": ["note", "story", "checklist", "other"]},
            "content": {"type": "string", "description": "完整的公开 Markdown 或纯文本成品"},
        }, "required": ["title", "kind", "content"], "additionalProperties": False}

    async def execute(self, title: str, kind: str, content: str) -> str:
        result = self._store.publish_artifact(
            self._ctx.drift_activity_id, self._ctx.drift_activity_call_id,
            title=title, kind=kind, content=content,
        )
        return json.dumps({"ok": True, **result, "message_sent": False}, ensure_ascii=False)
