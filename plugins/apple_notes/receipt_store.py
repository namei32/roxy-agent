from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading
from types import TracebackType
from typing import Literal, cast


class _ClosingConnection(sqlite3.Connection):
    """Give ``with connection`` transaction semantics and deterministic close."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()

OperationStatus = Literal[
    "prepared",
    "executing",
    "committed",
    "failed",
    "outcome_unknown",
]
FailureKind = Literal["", "operation_rejected", "unit_failed", "outcome_unknown"]


@dataclass(frozen=True)
class NoteDocument:
    document_key: str
    note_id: str
    folder_id: str
    title: str
    last_content_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    idempotency_key: str
    request_hash: str
    document_key: str
    source_ref: str
    session_key_hash: str
    mode: Literal["create", "append"]
    title: str
    content_hash: str
    status: OperationStatus
    note_id: str
    folder_id: str
    failure_kind: FailureKind
    error_code: str
    error_detail: str
    created_at: str
    updated_at: str


class ReceiptConflictError(RuntimeError):
    pass


class AppleNotesReceiptStore:
    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._schema_lock = threading.Lock()
        self._initialized = False

    def reserve(
        self,
        *,
        operation_id: str,
        idempotency_key: str,
        request_hash: str,
        document_key: str,
        source_ref: str,
        session_key_hash: str,
        mode: Literal["create", "append"],
        title: str,
        content_hash: str,
        created_at: str,
    ) -> tuple[OperationReceipt, bool]:
        """Reserve one durable operation or return its exact replay."""

        self._ensure_schema()
        with closing(self._connect()) as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM note_operations WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                receipt = _receipt_from_row(row)
                if receipt.request_hash != request_hash:
                    raise ReceiptConflictError("相同幂等键对应了不同 Apple Notes 请求")
                conn.commit()
                return receipt, False
            _ = conn.execute(
                """
                INSERT INTO note_operations(
                    operation_id, idempotency_key, request_hash, document_key,
                    source_ref, session_key_hash, mode, title, content_hash,
                    status, note_id, folder_id, failure_kind, error_code,
                    error_detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', '', '', '', '', '', ?, ?)
                """,
                (
                    operation_id,
                    idempotency_key,
                    request_hash,
                    document_key,
                    source_ref,
                    session_key_hash,
                    mode,
                    title,
                    content_hash,
                    created_at,
                    created_at,
                ),
            )
            conn.commit()
        receipt = self.get_operation(operation_id)
        if receipt is None:
            raise RuntimeError("Apple Notes 操作预留后不可见")
        return receipt, True

    def get_operation(self, operation_id: str) -> OperationReceipt | None:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM note_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return _receipt_from_row(row) if row is not None else None

    def get_document(self, document_key: str) -> NoteDocument | None:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM note_documents WHERE document_key=?",
                (document_key,),
            ).fetchone()
        return _document_from_row(row) if row is not None else None

    def latest_for_document(self, document_key: str) -> OperationReceipt | None:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM note_operations
                WHERE document_key=?
                ORDER BY created_at DESC, operation_id DESC
                LIMIT 1
                """,
                (document_key,),
            ).fetchone()
        return _receipt_from_row(row) if row is not None else None

    def mark_executing(self, operation_id: str, *, updated_at: str) -> OperationReceipt:
        return self._transition(
            operation_id,
            allowed={"prepared", "outcome_unknown", "executing"},
            status="executing",
            updated_at=updated_at,
            failure_kind="",
            error_code="",
            error_detail="",
        )

    def mark_failed(
        self,
        operation_id: str,
        *,
        failure_kind: Literal["operation_rejected", "unit_failed"],
        error_code: str,
        error_detail: str,
        updated_at: str,
    ) -> OperationReceipt:
        return self._transition(
            operation_id,
            allowed={"prepared", "executing"},
            status="failed",
            updated_at=updated_at,
            failure_kind=failure_kind,
            error_code=error_code,
            error_detail=error_detail,
        )

    def mark_outcome_unknown(
        self,
        operation_id: str,
        *,
        error_code: str,
        error_detail: str,
        updated_at: str,
    ) -> OperationReceipt:
        return self._transition(
            operation_id,
            allowed={"prepared", "executing", "outcome_unknown"},
            status="outcome_unknown",
            updated_at=updated_at,
            failure_kind="outcome_unknown",
            error_code=error_code,
            error_detail=error_detail,
        )

    def mark_committed(
        self,
        operation_id: str,
        *,
        note_id: str,
        folder_id: str,
        updated_at: str,
    ) -> OperationReceipt:
        """Commit the external receipt and document mapping in one transaction."""

        self._ensure_schema()
        with closing(self._connect()) as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM note_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Apple Notes 操作不存在: {operation_id}")
            receipt = _receipt_from_row(row)
            if receipt.status == "committed":
                if receipt.note_id != note_id:
                    raise ReceiptConflictError("已提交操作的 note_id 发生变化")
                conn.commit()
                return receipt
            if receipt.status not in {"executing", "outcome_unknown"}:
                raise ReceiptConflictError(
                    f"Apple Notes 操作不能从 {receipt.status} 提交"
                )
            document_row = conn.execute(
                "SELECT * FROM note_documents WHERE document_key=?",
                (receipt.document_key,),
            ).fetchone()
            if receipt.mode == "create":
                if document_row is not None:
                    document = _document_from_row(document_row)
                    if document.note_id != note_id:
                        raise ReceiptConflictError("文档键已绑定到另一条 Apple Note")
                    _ = conn.execute(
                        """
                        UPDATE note_documents
                        SET title=?, folder_id=?, last_content_hash=?, updated_at=?
                        WHERE document_key=?
                        """,
                        (
                            receipt.title,
                            folder_id,
                            receipt.content_hash,
                            updated_at,
                            receipt.document_key,
                        ),
                    )
                else:
                    _ = conn.execute(
                        """
                        INSERT INTO note_documents(
                            document_key, note_id, folder_id, title,
                            last_content_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            receipt.document_key,
                            note_id,
                            folder_id,
                            receipt.title,
                            receipt.content_hash,
                            receipt.created_at,
                            updated_at,
                        ),
                    )
            else:
                if document_row is None:
                    raise ReceiptConflictError("追加目标不再存在于插件文档映射中")
                document = _document_from_row(document_row)
                if document.note_id != note_id:
                    raise ReceiptConflictError("追加回执与文档 note_id 不一致")
                _ = conn.execute(
                    """
                    UPDATE note_documents
                    SET folder_id=?, last_content_hash=?, updated_at=?
                    WHERE document_key=?
                    """,
                    (
                        folder_id,
                        receipt.content_hash,
                        updated_at,
                        receipt.document_key,
                    ),
                )
            _ = conn.execute(
                """
                UPDATE note_operations
                SET status='committed', note_id=?, folder_id=?,
                    failure_kind='', error_code='', error_detail='', updated_at=?
                WHERE operation_id=?
                """,
                (note_id, folder_id, updated_at, operation_id),
            )
            conn.commit()
        committed = self.get_operation(operation_id)
        if committed is None:
            raise RuntimeError("Apple Notes 操作提交后不可见")
        return committed

    def _transition(
        self,
        operation_id: str,
        *,
        allowed: set[OperationStatus],
        status: OperationStatus,
        updated_at: str,
        failure_kind: FailureKind,
        error_code: str,
        error_detail: str,
    ) -> OperationReceipt:
        self._ensure_schema()
        with closing(self._connect()) as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM note_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Apple Notes 操作不存在: {operation_id}")
            current = _receipt_from_row(row)
            if current.status not in allowed:
                raise ReceiptConflictError(
                    f"Apple Notes 操作不能从 {current.status} 变为 {status}"
                )
            _ = conn.execute(
                """
                UPDATE note_operations
                SET status=?, failure_kind=?, error_code=?, error_detail=?, updated_at=?
                WHERE operation_id=?
                """,
                (
                    status,
                    failure_kind,
                    error_code,
                    error_detail,
                    updated_at,
                    operation_id,
                ),
            )
            conn.commit()
        updated = self.get_operation(operation_id)
        if updated is None:
            raise RuntimeError("Apple Notes 操作更新后不可见")
        return updated

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._schema_lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect(raw=True)) as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                if version not in {0, self.SCHEMA_VERSION}:
                    raise RuntimeError(f"Apple Notes 收据库版本不受支持: {version}")
                _ = conn.executescript("""
                    CREATE TABLE IF NOT EXISTS note_operations (
                        operation_id TEXT PRIMARY KEY,
                        idempotency_key TEXT NOT NULL UNIQUE,
                        request_hash TEXT NOT NULL,
                        document_key TEXT NOT NULL,
                        source_ref TEXT NOT NULL,
                        session_key_hash TEXT NOT NULL,
                        mode TEXT NOT NULL CHECK (mode IN ('create', 'append')),
                        title TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN (
                                'prepared', 'executing', 'committed',
                                'failed', 'outcome_unknown'
                            )
                        ),
                        note_id TEXT NOT NULL DEFAULT '',
                        folder_id TEXT NOT NULL DEFAULT '',
                        failure_kind TEXT NOT NULL DEFAULT '' CHECK (
                            failure_kind IN (
                                '', 'operation_rejected', 'unit_failed',
                                'outcome_unknown'
                            )
                        ),
                        error_code TEXT NOT NULL DEFAULT '',
                        error_detail TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_note_operations_document
                    ON note_operations(document_key, created_at DESC);

                    CREATE TABLE IF NOT EXISTS note_documents (
                        document_key TEXT PRIMARY KEY,
                        note_id TEXT NOT NULL UNIQUE,
                        folder_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        last_content_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """)
                _ = conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
                conn.commit()
            self._initialized = True

    def _connect(self, *, raw: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self._path), timeout=10, factory=_ClosingConnection
        )
        conn.row_factory = sqlite3.Row
        _ = conn.execute("PRAGMA foreign_keys=ON")
        _ = conn.execute("PRAGMA busy_timeout=5000")
        if raw:
            _ = conn.execute("PRAGMA journal_mode=WAL")
        return conn


def _receipt_from_row(row: sqlite3.Row) -> OperationReceipt:
    status = str(row["status"])
    if status not in {
        "prepared",
        "executing",
        "committed",
        "failed",
        "outcome_unknown",
    }:
        raise ValueError(f"Apple Notes 收据状态非法: {status}")
    failure_kind = str(row["failure_kind"])
    if failure_kind not in {
        "",
        "operation_rejected",
        "unit_failed",
        "outcome_unknown",
    }:
        raise ValueError(f"Apple Notes 失败分类非法: {failure_kind}")
    mode = str(row["mode"])
    if mode not in {"create", "append"}:
        raise ValueError(f"Apple Notes 操作类型非法: {mode}")
    return OperationReceipt(
        operation_id=str(row["operation_id"]),
        idempotency_key=str(row["idempotency_key"]),
        request_hash=str(row["request_hash"]),
        document_key=str(row["document_key"]),
        source_ref=str(row["source_ref"]),
        session_key_hash=str(row["session_key_hash"]),
        mode=cast(Literal["create", "append"], mode),
        title=str(row["title"]),
        content_hash=str(row["content_hash"]),
        status=cast(OperationStatus, status),
        note_id=str(row["note_id"]),
        folder_id=str(row["folder_id"]),
        failure_kind=cast(FailureKind, failure_kind),
        error_code=str(row["error_code"]),
        error_detail=str(row["error_detail"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _document_from_row(row: sqlite3.Row) -> NoteDocument:
    return NoteDocument(
        document_key=str(row["document_key"]),
        note_id=str(row["note_id"]),
        folder_id=str(row["folder_id"]),
        title=str(row["title"]),
        last_content_hash=str(row["last_content_hash"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
