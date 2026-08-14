from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

import pytest

from agent.tools.base import ToolExecutionContext
from plugins.apple_notes.bridge import (
    AppleNotesBridge,
    NotesMutationReceipt,
    NotesOutcomeUnknown,
    NotesOperationRejected,
    NotesUnitFailed,
    _parse_script_envelope,
)
from plugins.apple_notes.config import AppleNotesConfig
from plugins.apple_notes.receipt_store import AppleNotesReceiptStore
from plugins.apple_notes.renderer import NotesRenderer
from plugins.apple_notes.service import AppleNotesService


class FakeBridge:
    def __init__(self) -> None:
        self.create_calls = 0
        self.append_calls = 0
        self.find_calls = 0
        self.found: NotesMutationReceipt | None = None
        self.found_by_marker: dict[str, NotesMutationReceipt | None] = {}
        self.markers: list[str] = []
        self.create_error: BaseException | None = None

    def require_available(self) -> None:
        return None

    async def create(
        self, *, title: str, html: str, document_key: str = ""
    ) -> NotesMutationReceipt:
        self.create_calls += 1
        assert title
        assert document_key
        assert "ROXY_EXPORT:" in html
        if self.create_error is not None:
            raise self.create_error
        return NotesMutationReceipt(note_id="note-1", folder_id="folder-1")

    async def append(
        self, *, note_id: str, html: str, document_key: str = ""
    ) -> NotesMutationReceipt:
        self.append_calls += 1
        assert note_id == "note-1"
        assert document_key
        assert "ROXY_EXPORT:" in html
        return NotesMutationReceipt(note_id=note_id, folder_id="folder-1")

    async def find_marker(self, marker: str) -> NotesMutationReceipt | None:
        self.find_calls += 1
        assert marker.startswith(("ROXY_EXPORT:", "AKASHIC_EXPORT:"))
        self.markers.append(marker)
        return self.found_by_marker.get(marker, self.found)


class OfflineBridge(FakeBridge):
    def require_available(self) -> None:
        raise NotesOperationRejected(
            "mac_notes_bridge_offline",
            "Mac offline",
            stage="online_gate",
        )


def _context(source_ref: str = "telegram:update:100") -> ToolExecutionContext:
    return ToolExecutionContext(
        origin_channel="telegram",
        origin_chat_id="42",
        origin_session_key="telegram:42",
        turn_id="turn-1",
        current_user_source_ref=source_ref,
        execution_id="execution-1",
    )


def _service(
    tmp_path: Path,
    bridge: FakeBridge,
) -> tuple[AppleNotesService, AppleNotesReceiptStore]:
    config = AppleNotesConfig()
    receipts = AppleNotesReceiptStore(tmp_path / "receipts.sqlite3")
    service = AppleNotesService(
        config=config,
        renderer=NotesRenderer(config),
        receipts=receipts,
        bridge=bridge,  # type: ignore[arg-type]
    )
    return service, receipts


def test_renderer_produces_bounded_safe_knowledge_card() -> None:
    renderer = NotesRenderer(AppleNotesConfig())

    rendered = renderer.preview(
        title="Memory2 链路",
        markdown=(
            "# Memory2 链路\n\n"
            "## 输入\n\n- 用户消息\n- [安全链接](https://example.com)\n"
            "- [危险链接](javascript:alert(1))\n\n"
            "<script>window.bad = true</script>"
        ),
        template="flow_chain",
    )

    assert rendered.html.count("<h1>") == 1
    assert "🧭 链路知识卡" in rendered.html
    assert 'href="https://example.com"' in rendered.html
    assert 'href="javascript:' not in rendered.html
    assert "<script>" not in rendered.html
    assert "ROXY_EXPORT:preview" in rendered.html
    assert rendered.html_bytes == len(rendered.html.encode("utf-8"))


def test_renderer_supports_interview_review_template() -> None:
    renderer = NotesRenderer(AppleNotesConfig())

    rendered = renderer.preview(
        title="面经复盘｜Memory2｜2026-08-07",
        markdown="## 原题\n\nMemory2 的检索链路是什么？",
        template="interview_review",
    )

    assert "🎯 面经复盘" in rendered.html
    assert "Memory2 的检索链路是什么？" in rendered.plaintext


@pytest.mark.asyncio
async def test_interview_export_requires_text_only_template(tmp_path: Path) -> None:
    bridge = FakeBridge()
    service, _ = _service(tmp_path, bridge)

    wrong_template = await service.create(
        title="面经复盘",
        markdown="## 原题\n\n什么是 Agent？",
        document_key="interview:batch-1",
        template="knowledge_card",
        context=_context(),
    )
    image_markdown = await service.create(
        title="面经复盘",
        markdown="## 原题\n\n![原图](/tmp/workspace/uploads/source.png)",
        document_key="interview:batch-2",
        template="interview_review",
        context=_context("telegram:update:101"),
    )

    assert wrong_template.status == "operation_rejected"
    assert "interview_review" in wrong_template.detail
    assert image_markdown.status == "operation_rejected"
    assert "只允许整理后的文字" in image_markdown.detail
    assert bridge.create_calls == 0


def test_renderer_rejects_oversize_and_unknown_template() -> None:
    renderer = NotesRenderer(
        AppleNotesConfig(max_markdown_characters=200, max_html_bytes=1_000)
    )

    with pytest.raises(ValueError, match="未知备忘录模板"):
        renderer.preview(title="标题", markdown="正文", template="unknown")
    with pytest.raises(ValueError, match="正文超过限制"):
        renderer.preview(title="标题", markdown="x" * 201, template="plain")


def test_recovery_marker_cannot_be_disabled() -> None:
    with pytest.raises(ValueError):
        AppleNotesConfig.model_validate({"include_provenance_footer": False})


@pytest.mark.asyncio
async def test_create_and_append_are_idempotent_per_explicit_user_source(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge()
    service, receipts = _service(tmp_path, bridge)

    created = await service.create(
        title="Memory2 链路",
        markdown="## 链路\n\n输入 → 提取 → 写入",
        document_key="memory2-chain",
        template="flow_chain",
        context=_context(),
    )
    replayed_create = await service.create(
        title="Memory2 链路",
        markdown="## 链路\n\n输入 → 提取 → 写入",
        document_key="memory2-chain",
        template="flow_chain",
        context=_context(),
    )

    assert created.status == "committed"
    assert replayed_create.status == "committed"
    assert replayed_create.idempotent_replay is True
    assert bridge.create_calls == 1

    first_append = await service.append(
        document_key="memory2-chain",
        markdown="新增验收标准",
        section_title="验收",
        template="knowledge_card",
        context=_context("telegram:update:101"),
    )
    replayed_append = await service.append(
        document_key="memory2-chain",
        markdown="新增验收标准",
        section_title="验收",
        template="knowledge_card",
        context=_context("telegram:update:101"),
    )
    repeated_by_new_request = await service.append(
        document_key="memory2-chain",
        markdown="新增验收标准",
        section_title="验收",
        template="knowledge_card",
        context=_context("telegram:update:102"),
    )

    assert first_append.status == "committed"
    assert replayed_append.idempotent_replay is True
    assert repeated_by_new_request.status == "committed"
    assert bridge.append_calls == 2
    assert receipts.get_document("memory2-chain") is not None


@pytest.mark.asyncio
async def test_write_requires_current_explicit_user_provenance(tmp_path: Path) -> None:
    bridge = FakeBridge()
    service, _ = _service(tmp_path, bridge)

    missing_context = await service.create(
        title="标题",
        markdown="正文",
        document_key="doc-1",
        template="plain",
        context=None,
    )
    proactive_context = await service.create(
        title="标题",
        markdown="正文",
        document_key="doc-1",
        template="plain",
        context=ToolExecutionContext(origin_session_key="proactive:tick"),
    )

    assert missing_context.code == "explicit_user_source_required"
    assert proactive_context.code == "explicit_user_source_required"
    assert bridge.create_calls == 0


@pytest.mark.asyncio
async def test_remote_offline_returns_full_content_without_creating_receipt(
    tmp_path: Path,
) -> None:
    bridge = OfflineBridge()
    service, receipts = _service(tmp_path, bridge)
    content = "完整回答：Mac 离线时仍必须返回给当前用户。"

    result = await service.create(
        title="离线降级",
        markdown=content,
        document_key="offline-note",
        template="plain",
        context=_context(),
    )

    assert result.status == "skipped_offline"
    assert result.content == content
    assert result.code == "mac_notes_bridge_offline"
    assert receipts.latest_for_document("offline-note") is None
    assert bridge.create_calls == 0


@pytest.mark.asyncio
async def test_render_failure_is_terminal_before_external_execution(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge()
    config = AppleNotesConfig(max_html_bytes=1_000)
    receipts = AppleNotesReceiptStore(tmp_path / "receipts.sqlite3")
    service = AppleNotesService(
        config=config,
        renderer=NotesRenderer(config),
        receipts=receipts,
        bridge=bridge,  # type: ignore[arg-type]
    )
    arguments = {
        "title": "渲染边界",
        "markdown": "&" * 900,
        "document_key": "render-limit",
        "template": "plain",
        "context": _context(),
    }

    rejected = await service.create(**arguments)
    replayed = await service.create(**arguments)

    assert rejected.status == "operation_rejected"
    assert rejected.code == "rendered_content_invalid"
    assert replayed.status == "operation_rejected"
    assert replayed.idempotent_replay is True
    assert bridge.create_calls == 0
    receipt = receipts.latest_for_document("render-limit")
    assert receipt is not None and receipt.status == "failed"


@pytest.mark.asyncio
async def test_append_is_limited_to_plugin_owned_note(tmp_path: Path) -> None:
    bridge = FakeBridge()
    service, _ = _service(tmp_path, bridge)

    result = await service.append(
        document_key="unknown-note",
        markdown="不得写入",
        section_title="追加",
        template="plain",
        context=_context(),
    )

    assert result.status == "operation_rejected"
    assert result.code == "document_not_found"
    assert bridge.append_calls == 0


@pytest.mark.asyncio
async def test_unknown_outcome_never_replays_effect_and_can_reconcile(
    tmp_path: Path,
) -> None:
    bridge = FakeBridge()
    bridge.create_error = NotesOutcomeUnknown(
        "notes_script_timeout",
        "timeout",
        stage="create",
    )
    service, _ = _service(tmp_path, bridge)
    arguments = {
        "title": "待恢复",
        "markdown": "可能已经写入",
        "document_key": "recoverable-note",
        "template": "plain",
        "context": _context(),
    }

    first = await service.create(**arguments)
    second = await service.create(**arguments)

    assert first.status == "outcome_unknown"
    assert second.status == "outcome_unknown"
    assert second.idempotent_replay is True
    assert bridge.create_calls == 1
    assert bridge.find_calls == 2
    assert bridge.markers == [
        f"ROXY_EXPORT:{first.operation_id}",
        f"AKASHIC_EXPORT:{first.operation_id}",
    ]

    bridge.found = NotesMutationReceipt(note_id="note-1", folder_id="folder-1")
    status = await service.status("recoverable-note")

    assert status.status == "committed"
    assert status.idempotent_replay is True
    assert bridge.create_calls == 1
    assert bridge.find_calls == 3


@pytest.mark.asyncio
async def test_recovery_falls_back_to_legacy_akashic_marker(tmp_path: Path) -> None:
    bridge = FakeBridge()
    bridge.create_error = NotesOutcomeUnknown(
        "notes_script_timeout",
        "timeout",
        stage="create",
    )
    service, _ = _service(tmp_path, bridge)
    first = await service.create(
        title="旧标识恢复",
        markdown="可能已经写入",
        document_key="legacy-marker",
        template="plain",
        context=_context(),
    )
    bridge.found_by_marker[f"AKASHIC_EXPORT:{first.operation_id}"] = (
        NotesMutationReceipt(note_id="note-legacy", folder_id="folder-1")
    )

    status = await service.status("legacy-marker")

    assert status.status == "committed"
    assert bridge.markers[-2:] == [
        f"ROXY_EXPORT:{first.operation_id}",
        f"AKASHIC_EXPORT:{first.operation_id}",
    ]


@pytest.mark.asyncio
async def test_receipt_database_does_not_store_note_body(tmp_path: Path) -> None:
    bridge = FakeBridge()
    service, _ = _service(tmp_path, bridge)
    secret_body = "UNIQUE-NOTE-BODY-DO-NOT-PERSIST"

    result = await service.create(
        title="安全边界",
        markdown=secret_body,
        document_key="privacy-note",
        template="plain",
        context=_context(),
    )

    assert result.status == "committed"
    for path in tmp_path.iterdir():
        if path.is_file():
            assert secret_body.encode("utf-8") not in path.read_bytes()


class _FakeProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"OK\tnote-1\tfolder-1\n", b""


@pytest.mark.asyncio
async def test_bridge_passes_untrusted_content_as_data_not_script_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "notes.applescript"
    script.write_text("-- fixed script\n", encoding="utf-8")
    osascript = tmp_path / "osascript"
    osascript.write_text("binary placeholder", encoding="utf-8")
    captured: dict[str, Any] = {}

    async def fake_spawn(*args: str, **kwargs: Any) -> _FakeProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        html_path = Path(args[7])
        captured["html"] = html_path.read_text(encoding="utf-8")
        captured["mode"] = os.stat(html_path).st_mode & 0o777
        return _FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)
    bridge = AppleNotesBridge(
        config=AppleNotesConfig(),
        script_path=script,
        temp_dir=tmp_path / "private",
        osascript_path=osascript,
        platform="darwin",
    )
    malicious_title = 'title" & do shell script "touch /tmp/owned" & "'
    html = "<p>safe data only</p>"

    result = await bridge.create(title=malicious_title, html=html)

    args = captured["args"]
    assert result.note_id == "note-1"
    assert args[6] == malicious_title
    assert captured["html"] == html
    assert captured["mode"] == 0o600
    assert not Path(args[7]).exists()
    assert script.read_text(encoding="utf-8") == "-- fixed script\n"


@pytest.mark.asyncio
async def test_bridge_rejects_symlinked_private_temp_directory(
    tmp_path: Path,
) -> None:
    script = tmp_path / "notes.applescript"
    script.write_text("-- fixed script\n", encoding="utf-8")
    osascript = tmp_path / "osascript"
    osascript.write_text("binary placeholder", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    temp_dir = tmp_path / "private"
    temp_dir.symlink_to(outside, target_is_directory=True)
    bridge = AppleNotesBridge(
        config=AppleNotesConfig(),
        script_path=script,
        temp_dir=temp_dir,
        osascript_path=osascript,
        platform="darwin",
    )

    with pytest.raises(NotesUnitFailed) as raised:
        await bridge.create(title="title", html="<p>body</p>")

    assert raised.value.code == "notes_tempfile_failed"
    assert list(outside.iterdir()) == []


@pytest.mark.asyncio
async def test_bridge_classifies_process_start_failure_before_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "notes.applescript"
    script.write_text("-- fixed script\n", encoding="utf-8")
    osascript = tmp_path / "osascript"
    osascript.write_text("binary placeholder", encoding="utf-8")

    async def fail_spawn(*args: str, **kwargs: Any) -> _FakeProcess:
        del args, kwargs
        raise OSError("spawn failed")

    monkeypatch.setattr("asyncio.create_subprocess_exec", fail_spawn)
    bridge = AppleNotesBridge(
        config=AppleNotesConfig(),
        script_path=script,
        temp_dir=tmp_path / "private",
        osascript_path=osascript,
        platform="darwin",
    )

    with pytest.raises(NotesUnitFailed) as raised:
        await bridge.create(title="title", html="<p>body</p>")

    assert raised.value.code == "osascript_start_failed"
    assert list((tmp_path / "private").iterdir()) == []


def test_script_envelope_classifies_pre_effect_and_post_effect_errors() -> None:
    with pytest.raises(NotesUnitFailed, match="lookup failed"):
        _parse_script_envelope("ERROR\tresolve_folder\t-1\tlookup failed")
    with pytest.raises(NotesOutcomeUnknown, match="write failed"):
        _parse_script_envelope("ERROR\tappend_note\t-1\twrite failed")


@pytest.mark.skipif(
    sys.platform != "darwin", reason="AppleScript compiler is macOS-only"
)
def test_packaged_applescript_compiles(tmp_path: Path) -> None:
    import subprocess

    source = (
        Path(__file__).parents[1]
        / "plugins"
        / "apple_notes"
        / "scripts"
        / "notes.applescript"
    )
    subprocess.run(
        ["/usr/bin/osacompile", "-o", str(tmp_path / "notes.scpt"), str(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    source_text = source.read_text(encoding="utf-8")
    assert "with properties {body:htmlText}" in source_text
    assert "name:noteTitle" not in source_text


def test_tool_results_are_machine_readable_json(tmp_path: Path) -> None:
    bridge = FakeBridge()
    service, _ = _service(tmp_path, bridge)

    payload = json.loads(
        service.preview(
            title="预览",
            markdown="正文",
            template="plain",
        ).to_json()
    )

    assert payload["status"] == "preview"
    assert payload["title"] == "预览"
    assert payload["html_bytes"] > 0


@pytest.mark.asyncio
async def test_plugin_manager_publishes_tools_and_skill_atomically(
    tmp_path: Path,
) -> None:
    from agent.plugins.manager import PluginManager
    from agent.plugins.registry import plugin_registry
    from agent.tools.registry import ToolRegistry
    from bus.event_bus import EventBus

    plugin_root = tmp_path / "plugins"
    source = Path(__file__).parents[1] / "plugins" / "apple_notes"
    shutil.copytree(source, plugin_root / "apple_notes")
    manager = PluginManager(
        plugin_dirs=[plugin_root],
        event_bus=EventBus(),
        workspace=tmp_path / "workspace",
        installed_cache_root=tmp_path / "cache",
        tool_registry=ToolRegistry(),
    )
    try:
        await manager.load_all()

        gate = manager.latest_gate("apple_notes")
        snapshot = manager.current_snapshot
        assert manager.loaded_count == 1
        assert gate is not None and gate.status == "passed"
        assert snapshot is not None and snapshot.tool_registry is not None
        assert snapshot.tool_registry.get_tool_names_by_source(
            "plugin", "apple_notes"
        ) == {
            "apple_notes_preview",
            "apple_notes_create",
            "apple_notes_append",
            "apple_notes_status",
        }
        assert [item.plugin_id for item in manager.active_plugins()] == ["apple_notes"]
        assert manager.active_plugins()[0].skill_roots == (
            plugin_root / "apple_notes" / "skills",
        )
    finally:
        await manager.terminate_all()
        plugin_registry._handlers._handlers.clear()
        plugin_registry._classes.clear()
        plugin_registry._instances.clear()
