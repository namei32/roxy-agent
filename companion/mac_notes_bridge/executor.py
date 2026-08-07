from __future__ import annotations

import asyncio
import time

from infra.notes_bridge.port import NotesBridgeOperation, NotesBridgeResult
from plugins.apple_notes.bridge import (
    AppleNotesBridge,
    NotesOperationRejected,
    NotesOutcomeUnknown,
    NotesUnitFailed,
)
from plugins.apple_notes.config import AppleNotesConfig

from .store import MacNotesReceiptStore, MacOperationRecord


class MacNotesExecutor:
    """Execute a committed operation exactly once according to the local ledger."""

    def __init__(
        self,
        *,
        config: AppleNotesConfig,
        bridge: AppleNotesBridge,
        receipts: MacNotesReceiptStore,
        readiness_cache_seconds: float = 30.0,
    ) -> None:
        self._config = config
        self._bridge = bridge
        self._receipts = receipts
        self._readiness_cache_seconds = readiness_cache_seconds
        self._last_probe_at = 0.0
        self._last_probe_ready = False
        self._lock = asyncio.Lock()

    async def readiness(self) -> tuple[bool, bool]:
        now = time.monotonic()
        if now - self._last_probe_at <= self._readiness_cache_seconds:
            return self._last_probe_ready, self._last_probe_ready
        try:
            await self._bridge.probe()
        except (NotesOperationRejected, NotesUnitFailed, NotesOutcomeUnknown):
            ready = False
        else:
            ready = True
        self._last_probe_at = now
        self._last_probe_ready = ready
        return ready, ready

    async def accept_proposal(
        self,
        operation: NotesBridgeOperation,
        operation_hash: str,
    ) -> MacOperationRecord:
        self._validate_target(operation)
        notes_ready, permissions_ready = await self.readiness()
        if not notes_ready or not permissions_ready:
            raise NotesOperationRejected(
                "apple_notes_not_ready",
                "Mac 已连接，但 Apple Notes 或自动化权限未就绪",
                stage="proposal",
            )
        return await asyncio.to_thread(
            self._receipts.reserve,
            operation_id=operation.operation_id,
            request_hash=operation_hash,
            action=operation.action,
            document_key=operation.document_key,
        )

    async def execute(
        self,
        operation: NotesBridgeOperation,
        operation_hash: str,
    ) -> NotesBridgeResult:
        async with self._lock:
            record = await asyncio.to_thread(self._receipts.get, operation.operation_id)
            if record is None or record.request_hash != operation_hash:
                return NotesBridgeResult(
                    status="operation_rejected",
                    operation_id=operation.operation_id,
                    error_code="proposal_missing_or_changed",
                    detail="Mac 本地没有匹配的 proposal_accepted 记录",
                )
            if record.status in {
                "committed",
                "not_found",
                "operation_rejected",
                "unit_failed",
                "outcome_unknown",
            }:
                return _from_record(record)
            if record.status == "executing":
                unknown = await asyncio.to_thread(
                    self._receipts.mark_terminal,
                    operation.operation_id,
                    status="outcome_unknown",
                    error_code="interrupted_local_execution",
                    detail=(
                        "Mac 发现上次 commit 执行中断；为避免重复写入，"
                        "本次不会自动重放"
                    ),
                )
                return _from_record(unknown)
            _ = await asyncio.to_thread(
                self._receipts.mark_executing, operation.operation_id
            )
            try:
                if operation.action == "create":
                    receipt = await self._bridge.create(
                        title=operation.title,
                        html=operation.html,
                        document_key=operation.document_key,
                    )
                    status = "committed"
                elif operation.action == "append":
                    await asyncio.to_thread(
                        self._receipts.validate_append_target,
                        operation.document_key,
                        operation.note_id,
                    )
                    receipt = await self._bridge.append(
                        note_id=operation.note_id,
                        html=operation.html,
                        document_key=operation.document_key,
                    )
                    status = "committed"
                else:
                    receipt = await self._bridge.find_marker(operation.marker)
                    status = "committed" if receipt is not None else "not_found"
            except asyncio.CancelledError:
                _ = await asyncio.shield(
                    asyncio.to_thread(
                        self._receipts.mark_terminal,
                        operation.operation_id,
                        status="outcome_unknown",
                        error_code="mac_executor_cancelled",
                        detail="Mac executor 在 commit 后被取消，结果需要核对",
                    )
                )
                raise
            except NotesOperationRejected as error:
                terminal = await asyncio.to_thread(
                    self._receipts.mark_terminal,
                    operation.operation_id,
                    status="operation_rejected",
                    error_code=error.code,
                    detail=error.detail,
                )
                return _from_record(terminal)
            except (ValueError, NotesUnitFailed) as error:
                code = (
                    error.code
                    if isinstance(error, NotesUnitFailed)
                    else "local_target_rejected"
                )
                detail = (
                    error.detail if isinstance(error, NotesUnitFailed) else str(error)
                )
                terminal = await asyncio.to_thread(
                    self._receipts.mark_terminal,
                    operation.operation_id,
                    status="unit_failed",
                    error_code=code,
                    detail=detail,
                )
                return _from_record(terminal)
            except NotesOutcomeUnknown as error:
                terminal = await asyncio.to_thread(
                    self._receipts.mark_terminal,
                    operation.operation_id,
                    status="outcome_unknown",
                    error_code=error.code,
                    detail=error.detail,
                )
                return _from_record(terminal)

            terminal = await asyncio.to_thread(
                self._receipts.mark_terminal,
                operation.operation_id,
                status=status,
                note_id=receipt.note_id if receipt is not None else "",
                folder_id=receipt.folder_id if receipt is not None else "",
            )
            return _from_record(terminal)

    def _validate_target(self, operation: NotesBridgeOperation) -> None:
        if operation.account != self._config.account:
            raise NotesOperationRejected(
                "account_not_allowed",
                "云端 proposal 请求了 Mac 未授权的 Notes account",
                stage="proposal",
            )
        if operation.folder != self._config.folder:
            raise NotesOperationRejected(
                "folder_not_allowed",
                "云端 proposal 请求了 Mac 未授权的 Notes folder",
                stage="proposal",
            )
        if (
            operation.action == "create"
            and operation.create_folder_if_missing
            and not self._config.create_folder_if_missing
        ):
            raise NotesOperationRejected(
                "folder_creation_not_allowed",
                "云端 proposal 试图扩大 Mac 本地文件夹创建权限",
                stage="proposal",
            )


def _from_record(record: MacOperationRecord) -> NotesBridgeResult:
    return NotesBridgeResult(
        status=record.status,  # type: ignore[arg-type]
        operation_id=record.operation_id,
        note_id=record.note_id,
        folder_id=record.folder_id,
        error_code=record.error_code,
        detail=record.detail,
    )


__all__ = ["MacNotesExecutor"]
