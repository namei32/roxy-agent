from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AppleNotesConfig(BaseModel):
    """Validate the narrow Apple Notes target and rendering limits."""

    model_config = ConfigDict(extra="forbid")

    account: str = "default"
    folder: str = "Akashic"
    create_folder_if_missing: bool = True
    allow_create: bool = True
    allow_append: bool = True
    execution_mode: Literal["auto", "local", "remote"] = "auto"
    max_markdown_characters: int = Field(default=50_000, ge=200, le=200_000)
    max_html_bytes: int = Field(default=200_000, ge=1_000, le=1_000_000)
    script_timeout_seconds: float = Field(default=15.0, ge=1.0, le=60.0)
    default_template: Literal[
        "knowledge_card",
        "flow_chain",
        "interview_review",
        "plain",
    ] = "knowledge_card"
    # The footer carries the durable operation marker used to reconcile an
    # uncertain external write.  Allowing it to be disabled would make safe
    # recovery impossible, so API v0.1 deliberately requires it.
    include_provenance_footer: Literal[True] = True

    @field_validator("account", "folder")
    @classmethod
    def validate_target_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("不能是空字符串")
        if any(ord(char) < 32 for char in normalized):
            raise ValueError("不能包含控制字符")
        return normalized
