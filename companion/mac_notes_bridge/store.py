from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
import threading
from types import TracebackType


class _ClosingConnection(sqlite3.Connection):
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


@dataclass(frozen=True)
class MacOperationRecord:
    operation_id: str
    request_hash: str
    action: str
    document_key: str
    status: str
    note_id: str
    folder_id: str
    error_code: str
    detail: str


class MacNotesReceiptStore:
    """Device-local authority for commit receipts and document ownership."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._initialized = False

    def reserve(
        self,
        *,
        operation_id: str,
        request_hash: str,
        action: str,
        document_key: str,
    ) -> MacOperationRecord:
        self._ensure_schema()
        now = _now()
        with self._connect() as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is not None:
                record = _record(row)
                if record.request_hash != request_hash:
                    raise ValueError("相同 operation_id 对应不同 Notes 请求")
                conn.commit()
                return record
            _ = conn.execute(
                """
                INSERT INTO operations(
                    operation_id, request_hash, action, document_key, status,
                    note_id, folder_id, error_code, detail, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'proposal_accepted', '', '', '', '', ?, ?)
                """,
                (operation_id, request_hash, action, document_key, now, now),
            )
            conn.commit()
        record = self.get(operation_id)
        if record is None:
            raise RuntimeError("Mac Notes proposal 写入账本后不可见")
        return record

    def get(self, operation_id: str) -> MacOperationRecord | None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
        return _record(row) if row is not None else None

    def mark_executing(self, operation_id: str) -> MacOperationRecord:
        return self._transition(
            operation_id,
            allowed={"proposal_accepted"},
            status="executing",
        )

    def mark_terminal(
        self,
        operation_id: str,
        *,
        status: str,
        note_id: str = "",
        folder_id: str = "",
        error_code: str = "",
        detail: str = "",
    ) -> MacOperationRecord:
        self._ensure_schema()
        if status not in {
            "committed",
            "not_found",
            "operation_rejected",
            "unit_failed",
            "outcome_unknown",
        }:
            raise ValueError(f"Mac Notes terminal status 无效: {status}")
        with self._connect() as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Mac Notes 操作不存在: {operation_id}")
            current = _record(row)
            if current.status in {
                "committed",
                "not_found",
                "operation_rejected",
                "unit_failed",
                "outcome_unknown",
            }:
                conn.commit()
                return current
            if current.status != "executing":
                raise RuntimeError(f"Mac Notes 操作不能从 {current.status} 结束")
            if status == "committed" and current.action in {"create", "append"}:
                self._commit_document_mapping(
                    conn,
                    current,
                    note_id=note_id,
                    folder_id=folder_id,
                )
            _ = conn.execute(
                """
                UPDATE operations
                SET status=?, note_id=?, folder_id=?, error_code=?, detail=?, updated_at=?
                WHERE operation_id=?
                """,
                (
                    status,
                    note_id,
                    folder_id,
                    error_code[:128],
                    detail[:2000],
                    _now(),
                    operation_id,
                ),
            )
            conn.commit()
        result = self.get(operation_id)
        assert result is not None
        return result

    def validate_append_target(self, document_key: str, note_id: str) -> None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT note_id FROM documents WHERE document_key=?",
                (document_key,),
            ).fetchone()
        if row is None or str(row["note_id"]) != note_id:
            raise ValueError("Mac 本地账本不拥有该 document_key/note_id 追加目标")

    def reconcile_committed(
        self,
        operation_id: str,
        *,
        note_id: str,
        folder_id: str,
    ) -> MacOperationRecord:
        """Promote an uncertain mutation only after its unique marker is found."""

        self._ensure_schema()
        with self._connect() as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Mac Notes 操作不存在: {operation_id}")
            current = _record(row)
            if current.status == "committed":
                if current.note_id != note_id:
                    raise ValueError("已提交 operation 的 note_id 与核对结果冲突")
                conn.commit()
                return current
            if current.action not in {"create", "append"} or current.status not in {
                "executing",
                "outcome_unknown",
            }:
                raise ValueError(f"Mac Notes 操作状态不允许核对提交: {current.status}")
            self._commit_document_mapping(
                conn,
                current,
                note_id=note_id,
                folder_id=folder_id,
            )
            _ = conn.execute(
                """
                UPDATE operations
                SET status='committed', note_id=?, folder_id=?, error_code='',
                    detail='', updated_at=?
                WHERE operation_id=?
                """,
                (note_id, folder_id, _now(), operation_id),
            )
            conn.commit()
        result = self.get(operation_id)
        assert result is not None
        return result

    def summary(self) -> dict[str, int]:
        self._ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM operations GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def _transition(
        self,
        operation_id: str,
        *,
        allowed: set[str],
        status: str,
    ) -> MacOperationRecord:
        self._ensure_schema()
        with self._connect() as conn:
            _ = conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Mac Notes 操作不存在: {operation_id}")
            current = _record(row)
            if current.status not in allowed:
                conn.commit()
                return current
            _ = conn.execute(
                "UPDATE operations SET status=?, updated_at=? WHERE operation_id=?",
                (status, _now(), operation_id),
            )
            conn.commit()
        result = self.get(operation_id)
        assert result is not None
        return result

    @staticmethod
    def _commit_document_mapping(
        conn: sqlite3.Connection,
        record: MacOperationRecord,
        *,
        note_id: str,
        folder_id: str,
    ) -> None:
        if not record.document_key or not note_id:
            raise ValueError("Mac Notes committed 写入缺少文档映射")
        row = conn.execute(
            "SELECT note_id FROM documents WHERE document_key=?",
            (record.document_key,),
        ).fetchone()
        if record.action == "append":
            if row is None or str(row["note_id"]) != note_id:
                raise ValueError("Mac Notes append 回执与本地文档映射不一致")
            return
        if row is not None and str(row["note_id"]) != note_id:
            raise ValueError("Mac Notes document_key 已绑定其他 Note")
        _ = conn.execute(
            """
            INSERT INTO documents(document_key, note_id, folder_id, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(document_key) DO UPDATE SET
                note_id=excluded.note_id,
                folder_id=excluded.folder_id,
                updated_at=excluded.updated_at
            """,
            (record.document_key, note_id, folder_id, _now()),
        )

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                _ = conn.executescript("""
                    CREATE TABLE IF NOT EXISTS operations(
                        operation_id TEXT PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        action TEXT NOT NULL,
                        document_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        note_id TEXT NOT NULL,
                        folder_id TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS documents(
                        document_key TEXT PRIMARY KEY,
                        note_id TEXT NOT NULL,
                        folder_id TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    """)
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn


def _record(row: sqlite3.Row) -> MacOperationRecord:
    return MacOperationRecord(
        operation_id=str(row["operation_id"]),
        request_hash=str(row["request_hash"]),
        action=str(row["action"]),
        document_key=str(row["document_key"]),
        status=str(row["status"]),
        note_id=str(row["note_id"]),
        folder_id=str(row["folder_id"]),
        error_code=str(row["error_code"]),
        detail=str(row["detail"]),
    )


def _now() -> str:
    return datetime.now().astimezone().isoformat()


__all__ = ["MacNotesReceiptStore", "MacOperationRecord"]
