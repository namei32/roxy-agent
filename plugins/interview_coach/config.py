from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InterviewCoachConfig(BaseModel):
    """校验面经自动归档的持久授权与资源边界。"""

    model_config = ConfigDict(extra="forbid")

    auto_save_interview_images: bool = False
    allowed_chat_ids: list[str] = Field(default_factory=list)
    classification_threshold: float = Field(default=0.85, ge=0.5, le=1.0)
    project_root: str = ""
    max_media_files: int = Field(default=10, ge=1, le=10)
    max_media_bytes: int = Field(default=20_000_000, ge=1_000_000, le=50_000_000)
    max_follow_up_questions: int = Field(default=5, ge=1, le=8)
    questions_per_turn: Literal[1] = 1
    save_original_images: Literal[False] = False
    note_batching: Literal["telegram_media_group"] = "telegram_media_group"

    @field_validator("allowed_chat_ids")
    @classmethod
    def validate_allowed_chat_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = str(raw).strip()
            if not value.isdigit() or int(value) <= 0:
                raise ValueError(
                    "allowed_chat_ids 只能包含正整数 Telegram 私聊 chat ID"
                )
            if value not in normalized:
                normalized.append(value)
        return normalized

    @field_validator("project_root")
    @classmethod
    def normalize_project_root(cls, value: str) -> str:
        return str(Path(value).expanduser()) if value.strip() else ""

    @model_validator(mode="after")
    def require_scoped_grant(self) -> "InterviewCoachConfig":
        if self.auto_save_interview_images and not self.allowed_chat_ids:
            raise ValueError("启用面经自动保存时必须配置 allowed_chat_ids")
        return self
