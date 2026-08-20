from __future__ import annotations

import re

from infra.notes_bridge.port import (
    NotesBridgeOperation,
    NotesBridgeResult,
    NotesBridgeService,
)

from .bridge import (
    NotesMutationReceipt,
    NotesOperationRejected,
    NotesOutcomeUnknown,
    NotesUnitFailed,
)
from .config import AppleNotesConfig

_MARKER_RE = re.compile(r"(?:ROXY|AKASHIC)_EXPORT:([A-Za-z0-9_-]{1,128})")


class RemoteAppleNotesBridge:
    """Adapt the runtime-owned live broker to the Apple Notes plugin port."""

    def __init__(
        self,
        *,
        config: AppleNotesConfig,
        broker: NotesBridgeService,
    ) -> None:
        self._config = config
        self._broker = broker

    def require_available(self) -> None:
        if not self._broker.is_online():
            raise NotesOperationRejected(
                "mac_notes_bridge_offline",
                "Mac 当前离线或 Apple Notes 权限未就绪；本次不会排队或稍后补写",
                stage="online_gate",
            )

    async def create(
        self,
        *,
        title: str,
        html: str,
        document_key: str = "",
    ) -> NotesMutationReceipt:
        operation_id = _operation_id_from_html(html)
        result = await self._broker.execute(
            NotesBridgeOperation(
                operation_id=operation_id,
                action="create",
                account=self._config.account,
                folder=self._config.folder,
                create_folder_if_missing=self._config.create_folder_if_missing,
                document_key=document_key,
                title=title,
                html=html,
            )
        )
        return _mutation_receipt(result, stage="create")

    async def append(
        self,
        *,
        note_id: str,
        html: str,
        document_key: str = "",
    ) -> NotesMutationReceipt:
        operation_id = _operation_id_from_html(html)
        result = await self._broker.execute(
            NotesBridgeOperation(
                operation_id=operation_id,
                action="append",
                account=self._config.account,
                folder=self._config.folder,
                document_key=document_key,
                note_id=note_id,
                html=html,
            )
        )
        return _mutation_receipt(result, stage="append")

    async def find_marker(self, marker: str) -> NotesMutationReceipt | None:
        match = _MARKER_RE.fullmatch(marker)
        if match is None:
            raise NotesUnitFailed(
                "notes_operation_marker_invalid",
                "远程 Notes 核对缺少有效的 Roxy 操作标识",
                stage="prepare_input",
            )
        operation_id = match.group(1)
        result = await self._broker.execute(
            NotesBridgeOperation(
                operation_id=f"find-{operation_id}",
                action="find",
                account=self._config.account,
                folder=self._config.folder,
                marker=marker,
            )
        )
        if result.status == "not_found":
            return None
        return _mutation_receipt(result, stage="find_marker")


def _operation_id_from_html(html: str) -> str:
    match = _MARKER_RE.search(html)
    if match is None:
        raise NotesUnitFailed(
            "notes_operation_marker_missing",
            "远程 Notes 写入缺少持久化操作标识",
            stage="prepare_input",
        )
    return match.group(1)


def _mutation_receipt(
    result: NotesBridgeResult,
    *,
    stage: str,
) -> NotesMutationReceipt:
    if result.status == "committed" and result.note_id and result.folder_id:
        return NotesMutationReceipt(
            note_id=result.note_id,
            folder_id=result.folder_id,
        )
    if result.status in {"operation_rejected", "skipped_offline"}:
        raise NotesOperationRejected(
            result.error_code or "mac_notes_bridge_offline",
            result.detail or "Mac Notes Bridge 拒绝了操作",
            stage=stage,
        )
    if result.status == "outcome_unknown":
        raise NotesOutcomeUnknown(
            result.error_code or "mac_bridge_commit_outcome_unknown",
            result.detail or "Mac Notes Bridge 写入结果未知",
            stage=stage,
        )
    raise NotesUnitFailed(
        result.error_code or "mac_notes_bridge_failed",
        result.detail or f"Mac Notes Bridge 返回意外状态: {result.status}",
        stage=stage,
    )


__all__ = ["RemoteAppleNotesBridge"]
