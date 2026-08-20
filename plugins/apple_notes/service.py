from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import re
from typing import Literal
import uuid

from agent.tools.base import ToolExecutionContext

from .bridge import (
    AppleNotesBridge,
    NotesBridgeError,
    NotesOperationRejected,
    NotesOutcomeUnknown,
    NotesUnitFailed,
)
from .config import AppleNotesConfig
from .receipt_store import (
    AppleNotesReceiptStore,
    NoteDocument,
    OperationReceipt,
)
from .renderer import NotesRenderer, RenderedNote

_DOCUMENT_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


@dataclass(frozen=True)
class NoteToolResult:
    status: Literal[
        "preview",
        "committed",
        "operation_rejected",
        "unit_failed",
        "outcome_unknown",
        "not_found",
    ]
    code: str = ""
    operation_id: str = ""
    document_key: str = ""
    note_id: str = ""
    folder_id: str = ""
    title: str = ""
    content_hash: str = ""
    idempotent_replay: bool = False
    preview: str = ""
    html_bytes: int = 0
    detail: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)


class AppleNotesService:
    """Own rendering, durable receipts, and serialized Notes mutations."""

    def __init__(
        self,
        *,
        config: AppleNotesConfig,
        renderer: NotesRenderer,
        receipts: AppleNotesReceiptStore,
        bridge: AppleNotesBridge,
    ) -> None:
        self._config = config
        self._renderer = renderer
        self._receipts = receipts
        self._bridge = bridge
        self._write_lock = asyncio.Lock()

    def preview(self, *, title: str, markdown: str, template: str) -> NoteToolResult:
        rendered = self._renderer.preview(
            title=title,
            markdown=markdown,
            template=self._template(template),
        )
        return NoteToolResult(
            status="preview",
            title=rendered.title,
            content_hash=rendered.content_hash,
            preview=_clip(rendered.plaintext, 1_600),
            html_bytes=rendered.html_bytes,
        )

    async def create(
        self,
        *,
        title: str,
        markdown: str,
        document_key: str,
        template: str,
        context: ToolExecutionContext | None,
    ) -> NoteToolResult:
        if not self._config.allow_create:
            return _rejected("create_disabled", "Apple Notes 创建能力已禁用")
        provenance_error = _validate_provenance(context)
        if provenance_error is not None:
            return provenance_error
        try:
            self._bridge.require_available()
            clean_key = _validate_document_key(document_key)
            actual_template = self._template(template)
            _validate_document_template(clean_key, actual_template)
            content_hash = self._renderer.fingerprint_create(
                title,
                markdown,
                actual_template,
            )
        except (ValueError, NotesOperationRejected) as error:
            return _error_result(error, fallback_kind="operation_rejected")

        async with self._write_lock:
            document = await asyncio.to_thread(self._receipts.get_document, clean_key)
            if document is not None:
                if document.last_content_hash == content_hash:
                    return self._unchanged(document)
                return _rejected(
                    "document_exists_use_append",
                    "document_key 已存在；请使用 apple_notes_append 追加内容",
                    document_key=clean_key,
                    note_id=document.note_id,
                    title=document.title,
                )
            return await self._execute_create(
                title=title,
                markdown=markdown,
                document_key=clean_key,
                template=actual_template,
                content_hash=content_hash,
                context=context,
            )

    async def append(
        self,
        *,
        document_key: str,
        markdown: str,
        section_title: str,
        template: str,
        context: ToolExecutionContext | None,
    ) -> NoteToolResult:
        if not self._config.allow_append:
            return _rejected("append_disabled", "Apple Notes 追加能力已禁用")
        provenance_error = _validate_provenance(context)
        if provenance_error is not None:
            return provenance_error
        try:
            self._bridge.require_available()
            clean_key = _validate_document_key(document_key)
            actual_template = self._template(template)
            _validate_document_template(clean_key, actual_template)
            content_hash = self._renderer.fingerprint_append(
                markdown,
                section_title,
                actual_template,
            )
        except (ValueError, NotesOperationRejected) as error:
            return _error_result(error, fallback_kind="operation_rejected")

        async with self._write_lock:
            document = await asyncio.to_thread(self._receipts.get_document, clean_key)
            if document is None:
                return _rejected(
                    "document_not_found",
                    "只能追加插件已经创建并记录的 Apple Note",
                    document_key=clean_key,
                )
            return await self._execute_append(
                document=document,
                markdown=markdown,
                section_title=section_title,
                template=actual_template,
                content_hash=content_hash,
                context=context,
            )

    async def status(self, document_key: str) -> NoteToolResult:
        try:
            clean_key = _validate_document_key(document_key)
        except ValueError as error:
            return _error_result(error, fallback_kind="operation_rejected")
        document = await asyncio.to_thread(self._receipts.get_document, clean_key)
        latest = await asyncio.to_thread(
            self._receipts.latest_for_document,
            clean_key,
        )
        if document is None and latest is None:
            return NoteToolResult(
                status="not_found",
                code="document_not_found",
                document_key=clean_key,
            )
        if latest is not None and latest.status in {
            "executing",
            "outcome_unknown",
        }:
            async with self._write_lock:
                current = await asyncio.to_thread(
                    self._receipts.latest_for_document,
                    clean_key,
                )
                if current is not None and current.status in {
                    "executing",
                    "outcome_unknown",
                }:
                    reconciled = await self._resume_or_reconcile(current)
                    assert reconciled is not None
                    return reconciled
        if latest is not None and latest.status != "committed":
            return _from_receipt(latest)
        assert document is not None
        return NoteToolResult(
            status="committed",
            code="document_status",
            operation_id=latest.operation_id if latest else "",
            document_key=document.document_key,
            note_id=document.note_id,
            folder_id=document.folder_id,
            title=document.title,
            content_hash=document.last_content_hash,
        )

    async def _execute_create(
        self,
        *,
        title: str,
        markdown: str,
        document_key: str,
        template: str,
        content_hash: str,
        context: ToolExecutionContext | None,
    ) -> NoteToolResult:
        assert context is not None
        receipt, created = await self._reserve(
            context=context,
            mode="create",
            document_key=document_key,
            title=" ".join(title.split()),
            content_hash=content_hash,
            template=template,
        )
        replay = await self._resume_or_reconcile(receipt)
        if replay is not None:
            return replay
        try:
            rendered = self._renderer.render_create(
                title=title,
                markdown=markdown,
                template=template,
                operation_id=receipt.operation_id,
                saved_at=datetime.fromisoformat(receipt.created_at),
            )
        except ValueError as error:
            return await self._reject_render(receipt, error, created=created)
        receipt = await asyncio.to_thread(
            self._receipts.mark_executing,
            receipt.operation_id,
            updated_at=_now().isoformat(),
        )
        return await self._call_bridge(
            receipt=receipt,
            rendered=rendered,
            created=created,
            action="create",
        )

    async def _execute_append(
        self,
        *,
        document: NoteDocument,
        markdown: str,
        section_title: str,
        template: str,
        content_hash: str,
        context: ToolExecutionContext | None,
    ) -> NoteToolResult:
        assert context is not None
        receipt, created = await self._reserve(
            context=context,
            mode="append",
            document_key=document.document_key,
            title=document.title,
            content_hash=content_hash,
            template=template,
        )
        replay = await self._resume_or_reconcile(receipt)
        if replay is not None:
            return replay
        try:
            rendered = self._renderer.render_append(
                note_title=document.title,
                markdown=markdown,
                section_title=section_title,
                template=template,
                operation_id=receipt.operation_id,
                saved_at=datetime.fromisoformat(receipt.created_at),
            )
        except ValueError as error:
            return await self._reject_render(receipt, error, created=created)
        receipt = await asyncio.to_thread(
            self._receipts.mark_executing,
            receipt.operation_id,
            updated_at=_now().isoformat(),
        )
        return await self._call_bridge(
            receipt=receipt,
            rendered=rendered,
            created=created,
            action="append",
            note_id=document.note_id,
        )

    async def _reserve(
        self,
        *,
        context: ToolExecutionContext,
        mode: Literal["create", "append"],
        document_key: str,
        title: str,
        content_hash: str,
        template: str,
    ) -> tuple[OperationReceipt, bool]:
        request_hash = _hash_json(
            {
                "mode": mode,
                "document_key": document_key,
                "title": title,
                "content_hash": content_hash,
                "template": template,
                "account": self._config.account,
                "folder": self._config.folder,
            }
        )
        idempotency_key = _hash_parts(
            context.current_user_source_ref,
            mode,
            document_key,
            request_hash,
        )
        created_at = _now().isoformat()
        return await asyncio.to_thread(
            self._receipts.reserve,
            operation_id=uuid.uuid4().hex,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            document_key=document_key,
            source_ref=context.current_user_source_ref,
            session_key_hash=_hash_parts(context.origin_session_key)[:24],
            mode=mode,
            title=title,
            content_hash=content_hash,
            created_at=created_at,
        )

    async def _resume_or_reconcile(
        self,
        receipt: OperationReceipt,
    ) -> NoteToolResult | None:
        if receipt.status == "committed":
            return _from_receipt(receipt, idempotent_replay=True)
        if receipt.status == "failed":
            return _from_receipt(receipt, idempotent_replay=True)
        if receipt.status == "prepared":
            return None
        if receipt.status == "executing":
            receipt = await asyncio.to_thread(
                self._receipts.mark_outcome_unknown,
                receipt.operation_id,
                error_code="interrupted_execution",
                error_detail=(
                    "发现未完成的 Apple Notes 外部调用；为避免重复写入，"
                    "只执行标识核对"
                ),
                updated_at=_now().isoformat(),
            )
        marker = f"AKASHIC_EXPORT:{receipt.operation_id}"
        try:
            found = await self._bridge.find_marker(marker)
        except NotesBridgeError as error:
            return _unknown_reconciliation_result(receipt, error)
        if found is None:
            return _from_receipt(receipt, idempotent_replay=True)
        committed = await asyncio.to_thread(
            self._receipts.mark_committed,
            receipt.operation_id,
            note_id=found.note_id,
            folder_id=found.folder_id,
            updated_at=_now().isoformat(),
        )
        return _from_receipt(committed, idempotent_replay=True)

    async def _call_bridge(
        self,
        *,
        receipt: OperationReceipt,
        rendered: RenderedNote,
        created: bool,
        action: Literal["create", "append"],
        note_id: str = "",
    ) -> NoteToolResult:
        try:
            if action == "create":
                external = await self._bridge.create(
                    title=rendered.title,
                    html=rendered.html,
                )
            else:
                external = await self._bridge.append(
                    note_id=note_id,
                    html=rendered.html,
                )
        except asyncio.CancelledError:
            _ = await asyncio.shield(
                asyncio.to_thread(
                    self._receipts.mark_outcome_unknown,
                    receipt.operation_id,
                    error_code="tool_cancelled",
                    error_detail="工具取消后无法确认 Apple Notes 外部结果",
                    updated_at=_now().isoformat(),
                )
            )
            raise
        except NotesOperationRejected as error:
            failed = await asyncio.to_thread(
                self._receipts.mark_failed,
                receipt.operation_id,
                failure_kind="operation_rejected",
                error_code=error.code,
                error_detail=error.detail,
                updated_at=_now().isoformat(),
            )
            return _from_receipt(failed, idempotent_replay=not created)
        except NotesUnitFailed as error:
            failed = await asyncio.to_thread(
                self._receipts.mark_failed,
                receipt.operation_id,
                failure_kind="unit_failed",
                error_code=error.code,
                error_detail=error.detail,
                updated_at=_now().isoformat(),
            )
            return _from_receipt(failed, idempotent_replay=not created)
        except NotesOutcomeUnknown as error:
            unknown = await asyncio.to_thread(
                self._receipts.mark_outcome_unknown,
                receipt.operation_id,
                error_code=error.code,
                error_detail=error.detail,
                updated_at=_now().isoformat(),
            )
            return _from_receipt(unknown, idempotent_replay=not created)
        committed = await asyncio.to_thread(
            self._receipts.mark_committed,
            receipt.operation_id,
            note_id=external.note_id,
            folder_id=external.folder_id,
            updated_at=_now().isoformat(),
        )
        return _from_receipt(committed, idempotent_replay=not created)

    async def _reject_render(
        self,
        receipt: OperationReceipt,
        error: ValueError,
        *,
        created: bool,
    ) -> NoteToolResult:
        failed = await asyncio.to_thread(
            self._receipts.mark_failed,
            receipt.operation_id,
            failure_kind="operation_rejected",
            error_code="rendered_content_invalid",
            error_detail=str(error),
            updated_at=_now().isoformat(),
        )
        return _from_receipt(failed, idempotent_replay=not created)

    def _unchanged(self, document: NoteDocument) -> NoteToolResult:
        latest = self._receipts.latest_for_document(document.document_key)
        return NoteToolResult(
            status="committed",
            code="content_unchanged",
            operation_id=latest.operation_id if latest else "",
            document_key=document.document_key,
            note_id=document.note_id,
            folder_id=document.folder_id,
            title=document.title,
            content_hash=document.last_content_hash,
            idempotent_replay=True,
            detail="相同内容已经写入，不重复创建或追加",
        )

    def _template(self, template: str) -> str:
        return template.strip() or self._config.default_template


def _validate_provenance(
    context: ToolExecutionContext | None,
) -> NoteToolResult | None:
    if context is None or not context.current_user_source_ref.strip():
        return _rejected(
            "explicit_user_source_required",
            "Apple Notes 写入只允许由当前用户消息及其有效授权触发",
        )
    if not context.origin_session_key.strip():
        return _rejected(
            "origin_session_required",
            "Apple Notes 写入缺少可信 session 来源",
        )
    return None


def _validate_document_key(value: str) -> str:
    normalized = value.strip()
    if _DOCUMENT_KEY_RE.fullmatch(normalized) is None:
        raise ValueError("document_key 必须是 1-128 位稳定标识，只允许字母、数字、._:-")
    return normalized


def _validate_document_template(document_key: str, template: str) -> None:
    if document_key.startswith("interview:") and template != "interview_review":
        raise ValueError("interview: document_key 必须使用 interview_review 模板")


def _from_receipt(
    receipt: OperationReceipt,
    *,
    idempotent_replay: bool = False,
) -> NoteToolResult:
    if receipt.status == "committed":
        status = "committed"
        code = "note_committed"
    elif receipt.status == "outcome_unknown":
        status = "outcome_unknown"
        code = receipt.error_code or "outcome_unknown"
    elif receipt.failure_kind == "operation_rejected":
        status = "operation_rejected"
        code = receipt.error_code or "operation_rejected"
    else:
        status = "unit_failed"
        code = receipt.error_code or "unit_failed"
    return NoteToolResult(
        status=status,
        code=code,
        operation_id=receipt.operation_id,
        document_key=receipt.document_key,
        note_id=receipt.note_id,
        folder_id=receipt.folder_id,
        title=receipt.title,
        content_hash=receipt.content_hash,
        idempotent_replay=idempotent_replay,
        detail=receipt.error_detail,
    )


def _error_result(
    error: BaseException,
    *,
    fallback_kind: Literal["operation_rejected", "unit_failed", "outcome_unknown"],
) -> NoteToolResult:
    if isinstance(error, NotesOutcomeUnknown):
        status = "outcome_unknown"
        code = error.code
        detail = error.detail
    elif isinstance(error, NotesOperationRejected):
        status = "operation_rejected"
        code = error.code
        detail = error.detail
    elif isinstance(error, NotesUnitFailed):
        status = "unit_failed"
        code = error.code
        detail = error.detail
    else:
        status = fallback_kind
        code = "invalid_request"
        detail = str(error)
    return NoteToolResult(status=status, code=code, detail=detail)


def _unknown_reconciliation_result(
    receipt: OperationReceipt,
    error: NotesBridgeError,
) -> NoteToolResult:
    detail = receipt.error_detail
    reconcile_detail = f"核对失败 {error.code}: {error.detail}"
    if detail:
        detail = f"{detail}；{reconcile_detail}"
    else:
        detail = reconcile_detail
    return NoteToolResult(
        status="outcome_unknown",
        code=receipt.error_code or "outcome_unknown",
        operation_id=receipt.operation_id,
        document_key=receipt.document_key,
        note_id=receipt.note_id,
        folder_id=receipt.folder_id,
        title=receipt.title,
        content_hash=receipt.content_hash,
        idempotent_replay=True,
        detail=detail,
    )


def _rejected(
    code: str,
    detail: str,
    *,
    document_key: str = "",
    note_id: str = "",
    title: str = "",
) -> NoteToolResult:
    return NoteToolResult(
        status="operation_rejected",
        code=code,
        document_key=document_key,
        note_id=note_id,
        title=title,
        detail=detail,
    )


def _hash_parts(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        hasher.update(len(encoded).to_bytes(8, "big"))
        hasher.update(encoded)
    return hasher.hexdigest()


def _hash_json(value: dict[str, str]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _now() -> datetime:
    return datetime.now().astimezone()


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"
