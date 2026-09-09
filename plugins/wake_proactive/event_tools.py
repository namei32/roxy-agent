from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


EVENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "send_event",
            "description": "发送根据本轮单条 alert 或 context 写成的自然主动消息。",
            "parameters": {
                "type": "object",
                "properties": {"message": {"type": "string"}, "related_session_id": {"type": "string", "description": "仅明确延续所提供会话时填写，通用提醒省略"}},
                "required": ["message"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_event",
            "description": "当前 ContextEvent 不值得单独打扰用户，保持安静。",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass(frozen=True, slots=True)
class EventToolResult:
    decision: Literal["reply", "skip"]
    message: str


def execute_event_tool(name: str, arguments: dict[str, Any]) -> EventToolResult:
    if name == "skip_event":
        return EventToolResult(decision="skip", message="")
    if name != "send_event":
        raise ValueError(f"unknown wake event tool: {name}")
    message = str(arguments.get("message") or "").strip()
    if not message:
        raise ValueError("send_event message 不能为空")
    return EventToolResult(decision="reply", message=message)
