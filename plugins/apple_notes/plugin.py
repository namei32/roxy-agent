from __future__ import annotations

import sys
from typing import cast

from agent.plugins import Plugin, PluginSemanticCheck, tool
from agent.tools.base import get_current_tool_context

from .bridge import AppleNotesBridge
from .config import AppleNotesConfig
from .receipt_store import AppleNotesReceiptStore
from .remote_bridge import RemoteAppleNotesBridge
from .renderer import NotesRenderer
from .service import AppleNotesService


class AppleNotesPlugin(Plugin):
    """Export authorized knowledge cards to Apple Notes."""

    api_version = 2
    name = "apple_notes"
    version = "0.1.0"
    desc = "把用户明确指定的重要内容安全地保存到 Apple 备忘录"
    author = "Roxy Agent"
    ConfigModel = AppleNotesConfig

    def __init__(self) -> None:
        self._service: AppleNotesService | None = None

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills",)

    def static_semantic_checks(self) -> list[PluginSemanticCheck]:
        script = self.context.plugin_dir / "scripts" / "notes.applescript"
        return [
            PluginSemanticCheck(
                check_id="apple_notes.fixed_script_packaged",
                passed=script.is_file(),
                evidence=str(script),
            )
        ]

    def activate(self) -> None:
        data_dir = self.context.data_dir
        if data_dir is None:
            raise RuntimeError("Apple Notes 插件缺少 plugin-data 目录")
        config = cast(AppleNotesConfig, self.context.config)
        data_dir.mkdir(parents=True, exist_ok=True)
        receipts = AppleNotesReceiptStore(data_dir / "receipts.sqlite3")
        runtime_services = self.context.runtime_services or {}
        broker = runtime_services.get("notes_bridge.broker.v1")
        use_remote = config.execution_mode == "remote" or (
            config.execution_mode == "auto"
            and sys.platform != "darwin"
            and broker is not None
        )
        if use_remote:
            if broker is None:
                raise RuntimeError("Apple Notes remote 模式缺少 Notes Bridge Broker")
            bridge = RemoteAppleNotesBridge(config=config, broker=broker)  # type: ignore[arg-type]
        else:
            bridge = AppleNotesBridge(
                config=config,
                script_path=self.context.plugin_dir / "scripts" / "notes.applescript",
                temp_dir=data_dir / "tmp",
            )
        self._service = AppleNotesService(
            config=config,
            renderer=NotesRenderer(config),
            receipts=receipts,
            bridge=bridge,
        )

    async def terminate(self) -> None:
        self._service = None

    @tool(
        name="apple_notes_preview",
        risk="read-only",
        always_on=False,
        search_hint="preview formatted Markdown before saving it to Apple Notes",
    )
    async def preview_note(
        self,
        event: object,
        title: str,
        markdown: str,
        template: str = "knowledge_card",
    ) -> str:
        """Preview a safe, formatted Apple Notes export without writing Notes.

        Args:
            title: The human-readable Apple Note title.
            markdown: Markdown content to render in the preview.
            template: knowledge_card, flow_chain, or plain.
        """

        del event
        return (
            self._require_service()
            .preview(
                title=title,
                markdown=markdown,
                template=template,
            )
            .to_json()
        )

    @tool(
        name="apple_notes_create",
        risk="read-write",
        always_on=False,
        search_hint=(
            "save important content to a new Apple Note after a current explicit "
            "request or the scoped Interview Coach automation grant"
        ),
    )
    async def create_note(
        self,
        event: object,
        title: str,
        markdown: str,
        document_key: str,
        template: str = "knowledge_card",
    ) -> str:
        """Create one plugin-owned Apple Note after an authorized request.

        Args:
            title: The human-readable Apple Note title.
            markdown: Markdown content to save.
            document_key: Stable key used for idempotency and later appends.
            template: knowledge_card, flow_chain, interview_review, or plain.
        """

        del event
        result = await self._require_service().create(
            title=title,
            markdown=markdown,
            document_key=document_key,
            template=template,
            context=get_current_tool_context(),
        )
        return result.to_json()

    @tool(
        name="apple_notes_append",
        risk="read-write",
        always_on=False,
        search_hint=(
            "append a new section to an Apple Note previously created by "
            "Roxy after an explicit request or active Interview Coach turn"
        ),
    )
    async def append_note(
        self,
        event: object,
        document_key: str,
        markdown: str,
        section_title: str = "追加内容",
        template: str = "knowledge_card",
    ) -> str:
        """Append content to a plugin-owned Apple Note.

        Args:
            document_key: Stable key of a note created by this plugin.
            markdown: Markdown content to append.
            section_title: Heading for the appended section.
            template: knowledge_card, flow_chain, interview_review, or plain.
        """

        del event
        result = await self._require_service().append(
            document_key=document_key,
            markdown=markdown,
            section_title=section_title,
            template=template,
            context=get_current_tool_context(),
        )
        return result.to_json()

    @tool(
        name="apple_notes_status",
        risk="read-only",
        always_on=False,
        search_hint="inspect the latest durable receipt for an Apple Notes export",
    )
    async def note_status(self, event: object, document_key: str) -> str:
        """Return the local receipt status for an Apple Notes export.

        Args:
            document_key: Stable key of the exported note.
        """

        del event
        return (await self._require_service().status(document_key)).to_json()

    def _require_service(self) -> AppleNotesService:
        if self._service is None:
            raise RuntimeError("Apple Notes 插件尚未激活")
        return self._service


__all__ = ["AppleNotesPlugin"]
