from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, cast

import pytest

from agent.lifecycle.types import AfterToolResultCtx, PreToolCtx, PromptRenderCtx
from agent.plugins.context import PluginContext, PluginKVStore
from agent.tools.base import ToolExecutionContext, tool_execution_context_scope
from plugins.interview_coach.config import InterviewCoachConfig
from plugins.interview_coach.evidence import ProjectEvidenceService
from plugins.interview_coach.plugin import InterviewCoachPlugin


def _git_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    if root.is_dir():
        return root
    root.mkdir()
    (root / "README.md").write_text(
        "# Akashic\n\nMemoryEngine owns retrieval.\n",
        encoding="utf-8",
    )
    (root / "config.toml").write_text("secret = 'hidden'\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    return root


def _plugin(
    tmp_path: Path,
    *,
    auto_save: bool = True,
) -> InterviewCoachPlugin:
    workspace = tmp_path / "workspace"
    (workspace / "uploads").mkdir(parents=True, exist_ok=True)
    project = _git_project(tmp_path)
    plugin = InterviewCoachPlugin()
    plugin.context = PluginContext(
        event_bus=cast(Any, None),
        tool_registry=None,
        plugin_id="interview_coach",
        plugin_dir=Path(__file__).resolve().parents[1] / "plugins" / "interview_coach",
        data_dir=tmp_path / "plugin-data",
        kv_store=PluginKVStore(tmp_path / "plugin-kv.json"),
        config=InterviewCoachConfig(
            auto_save_interview_images=auto_save,
            allowed_chat_ids=["42"] if auto_save else [],
            project_root=str(project),
        ),
        workspace=workspace,
    )
    plugin.activate()
    return plugin


def _png(path: Path) -> Path:
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fixture")
    return path


def _prompt(
    *,
    media: list[str] | None,
    chat_id: str = "42",
    channel: str = "telegram",
    content: str = "",
) -> PromptRenderCtx:
    return PromptRenderCtx(
        session_key=f"{channel}:{chat_id}",
        channel=channel,
        chat_id=chat_id,
        content=content,
        media=media,
        timestamp=datetime(2026, 8, 7, tzinfo=timezone.utc),
        history=[],
        skill_names=[],
        retrieved_memory_block="",
        disabled_sections=set(),
        turn_injection_prompt="",
    )


def _tool_context(source_ref: str = "telegram:42:1") -> ToolExecutionContext:
    return ToolExecutionContext(
        origin_channel="telegram",
        origin_chat_id="42",
        origin_session_key="telegram:42",
        turn_id="turn-1",
        current_user_source_ref=source_ref,
        execution_id="execution-1",
    )


def _notes_event(
    tool_name: str,
    document_key: str,
    status: str,
    *,
    event_status: str = "success",
) -> AfterToolResultCtx:
    return AfterToolResultCtx(
        session_key="telegram:42",
        channel="telegram",
        chat_id="42",
        tool_name=tool_name,
        arguments={"document_key": document_key},
        result=json.dumps({"status": status}),
        status=event_status,
    )


def _pre_tool(
    tool_name: str,
    arguments: dict[str, object],
    *,
    chat_id: str = "42",
    source: str = "passive",
) -> PreToolCtx:
    return PreToolCtx(
        session_key=f"telegram:{chat_id}",
        channel="telegram",
        chat_id=chat_id,
        tool_name=tool_name,
        arguments=arguments,
        source=source,
    )


def test_config_requires_narrow_private_chat_grant() -> None:
    with pytest.raises(ValueError, match="allowed_chat_ids"):
        InterviewCoachConfig(auto_save_interview_images=True)
    with pytest.raises(ValueError, match="正整数"):
        InterviewCoachConfig(
            auto_save_interview_images=True,
            allowed_chat_ids=["-100"],
        )


def test_prompt_only_admits_authorized_telegram_image(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    image = _png(tmp_path / "workspace" / "uploads" / "one.png")

    admitted = plugin.prompt_hint(_prompt(media=[str(image)]))

    assert admitted is not None
    assert json.loads(admitted)["mode"] == "interview_intake"
    assert plugin.prompt_hint(_prompt(media=[str(image)], chat_id="43")) is None
    assert plugin.prompt_hint(_prompt(media=[str(image)], channel="cli")) is None
    assert plugin.prompt_hint(_prompt(media=None)) is None


@pytest.mark.asyncio
async def test_prepare_note_receipts_and_one_question_cursor_are_recoverable(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    image = _png(tmp_path / "workspace" / "uploads" / "one.png")

    with tool_execution_context_scope(_tool_context()):
        prepared = json.loads(
            await plugin.prepare_batch(
                object(),
                [str(image)],
                "面经复盘｜Memory2｜2026-08-07",
                "Memory2 与 Agent 生命周期",
                ["为什么使用双层记忆？", "RRF 如何融合检索结果？"],
                0.96,
                ["图片包含连续技术面试问题"],
                2,
                1,
                1,
            )
        )
    assert prepared["status"] == "prepared"
    assert prepared["requires_note_create"] is True
    document_key = str(prepared["document_key"])

    assert (
        await plugin.guard_scoped_tools(
            _pre_tool("apple_notes_create", {"document_key": document_key})
        )
        is None
    )
    premature_append = await plugin.guard_scoped_tools(
        _pre_tool("apple_notes_append", {"document_key": document_key})
    )
    assert premature_append is not None
    assert premature_append.decision == "deny"
    unauthorized = await plugin.guard_scoped_tools(
        _pre_tool(
            "apple_notes_create",
            {"document_key": document_key},
            chat_id="43",
        )
    )
    assert unauthorized is not None
    assert unauthorized.decision == "deny"
    assert (
        await plugin.guard_scoped_tools(
            _pre_tool(
                "apple_notes_create",
                {"document_key": "explicit-knowledge-card"},
                chat_id="43",
            )
        )
        is None
    )

    recovery = plugin.prompt_hint(_prompt(media=None, content="继续"))
    assert recovery is not None
    assert json.loads(recovery)["mode"] == "interview_note_recovery"

    await plugin.record_apple_notes_result(
        _notes_event("apple_notes_create", document_key, "committed")
    )
    assert (
        await plugin.guard_scoped_tools(
            _pre_tool("apple_notes_append", {"document_key": document_key})
        )
        is None
    )
    await plugin.terminate()
    plugin = _plugin(tmp_path)
    first = json.loads(
        cast(str, plugin.prompt_hint(_prompt(media=None, content="我的回答")))
    )
    assert first["mode"] == "interview_follow_up"
    assert first["question_number"] == 1
    assert first["current_question"] == "为什么使用双层记忆？"

    await plugin.record_apple_notes_result(
        _notes_event("apple_notes_append", document_key, "unit_failed")
    )
    after_failed_append = json.loads(
        cast(str, plugin.prompt_hint(_prompt(media=None, content="修正后重试")))
    )
    assert after_failed_append["question_number"] == 1
    assert (
        await plugin.guard_scoped_tools(
            _pre_tool("apple_notes_append", {"document_key": document_key})
        )
        is None
    )

    await plugin.record_apple_notes_result(
        _notes_event("apple_notes_append", document_key, "outcome_unknown")
    )
    replay = await plugin.guard_scoped_tools(
        _pre_tool("apple_notes_append", {"document_key": document_key})
    )
    assert replay is not None
    assert replay.decision == "deny"
    assert (
        await plugin.guard_scoped_tools(
            _pre_tool("apple_notes_status", {"document_key": document_key})
        )
        is None
    )
    unknown = json.loads(
        cast(str, plugin.prompt_hint(_prompt(media=None, content="重试")))
    )
    assert unknown["mode"] == "interview_note_recovery"
    assert unknown["pending_operation"] == "append"

    await plugin.record_apple_notes_result(
        _notes_event("apple_notes_status", document_key, "committed")
    )
    second = json.loads(
        cast(str, plugin.prompt_hint(_prompt(media=None, content="下一题")))
    )
    assert second["question_number"] == 2
    assert second["current_question"] == "RRF 如何融合检索结果？"

    await plugin.record_apple_notes_result(
        _notes_event("apple_notes_append", document_key, "committed")
    )
    assert plugin.prompt_hint(_prompt(media=None, content="完成")) is None


@pytest.mark.asyncio
async def test_prepare_rejects_low_confidence_and_is_idempotent_per_source(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    image = _png(tmp_path / "workspace" / "uploads" / "one.png")
    arguments = (
        object(),
        [str(image)],
        "面经复盘｜Agent｜2026-08-07",
        "Agent",
        ["什么是生命周期？"],
    )

    with tool_execution_context_scope(_tool_context()):
        rejected = json.loads(
            await plugin.prepare_batch(
                *arguments,
                0.6,
                ["像技术问题"],
                1,
                1,
                0,
            )
        )
        invalid = json.loads(
            await plugin.prepare_batch(
                *arguments,
                float("nan"),
                ["明确的面试题列表"],
                1,
                1,
                0,
            )
        )
        first = json.loads(
            await plugin.prepare_batch(
                *arguments,
                0.95,
                ["明确的面试题列表"],
                1,
                1,
                0,
            )
        )
        replay = json.loads(
            await plugin.prepare_batch(
                *arguments,
                0.95,
                ["明确的面试题列表"],
                1,
                1,
                0,
            )
        )

    assert rejected["status"] == "rejected"
    assert invalid["status"] == "rejected"
    assert first["status"] == "prepared"
    assert replay["status"] == "existing"
    assert replay["document_key"] == first["document_key"]
    assert replay["requires_note_create"] is True


@pytest.mark.asyncio
async def test_failed_create_can_be_prepared_again_and_framework_error_does_not_advance(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    image = _png(tmp_path / "workspace" / "uploads" / "one.png")
    arguments = (
        object(),
        [str(image)],
        "面经复盘｜Agent｜2026-08-07",
        "Agent",
        ["什么是生命周期？"],
        0.95,
        ["明确的面试题列表"],
        1,
        1,
        0,
    )
    with tool_execution_context_scope(_tool_context()):
        prepared = json.loads(await plugin.prepare_batch(*arguments))
    document_key = str(prepared["document_key"])

    await plugin.record_apple_notes_result(
        _notes_event(
            "apple_notes_create",
            document_key,
            "ignored",
            event_status="denied",
        )
    )
    still_pending = json.loads(
        cast(str, plugin.prompt_hint(_prompt(media=None, content="继续")))
    )
    assert still_pending["mode"] == "interview_note_recovery"

    await plugin.record_apple_notes_result(
        _notes_event("apple_notes_create", document_key, "unit_failed")
    )
    with tool_execution_context_scope(_tool_context("telegram:42:2")):
        retry = json.loads(await plugin.prepare_batch(*arguments))
    assert retry["status"] == "existing"
    assert retry["requires_note_create"] is True
    assert retry["note_status"] == "pending"
    assert retry["pending_operation"] == "create"


@pytest.mark.asyncio
async def test_offline_create_is_terminal_for_the_current_turn_and_not_backfilled(
    tmp_path: Path,
) -> None:
    plugin = _plugin(tmp_path)
    image = _png(tmp_path / "workspace" / "uploads" / "one.png")
    arguments = (
        object(),
        [str(image)],
        "面经复盘｜Agent｜2026-08-07",
        "Agent",
        ["什么是生命周期？"],
        0.95,
        ["明确的面试题列表"],
        1,
        1,
        0,
    )
    with tool_execution_context_scope(_tool_context()):
        prepared = json.loads(await plugin.prepare_batch(*arguments))
    document_key = str(prepared["document_key"])

    await plugin.record_apple_notes_result(
        _notes_event("apple_notes_create", document_key, "skipped_offline")
    )

    # A later ordinary turn must not rediscover a pending write merely because
    # the Mac happens to reconnect. A new attempt needs a new explicit intake.
    assert plugin.prompt_hint(_prompt(media=None, content="Mac 现在上线了")) is None
    denied = await plugin.guard_scoped_tools(
        _pre_tool("apple_notes_create", {"document_key": document_key})
    )
    assert denied is not None
    assert denied.decision == "deny"


@pytest.mark.asyncio
async def test_project_evidence_is_revision_bound_and_excludes_config(
    tmp_path: Path,
) -> None:
    service = ProjectEvidenceService(_git_project(tmp_path))

    searched = json.loads(await service.search("MemoryEngine", 5))
    read = json.loads(await service.read("README.md", 1, 3))

    assert len(searched["revision"]) == 40
    assert searched["hits"][0]["path"] == "README.md"
    assert read["revision"] == searched["revision"]
    assert searched["worktree_dirty"] is False
    assert read["worktree_dirty"] is False
    assert len(searched["evidence_hash"]) == 64
    assert len(read["evidence_hash"]) == 64
    assert "MemoryEngine owns retrieval" in read["content"]
    with pytest.raises(ValueError, match="不允许读取"):
        await service.read("config.toml", 1, 1)

    (tmp_path / "project" / "README.md").write_text(
        "# Akashic\n\nMemoryEngine owns hybrid retrieval.\n",
        encoding="utf-8",
    )
    changed = json.loads(await service.read("README.md", 1, 3))
    assert changed["revision"] == read["revision"]
    assert changed["worktree_dirty"] is True
    assert changed["evidence_hash"] != read["evidence_hash"]
