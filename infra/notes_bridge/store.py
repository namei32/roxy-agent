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
class BridgeAuditRecord:
    operation_id: str
    bridge_id: str
    connection_epoch: str
    action: str
    document_key_hash: str
    request_hash: str
    content_hash: str
    status: str
    error_code: str
    created_at: str
    updated_at: str


class NotesBridgeAuditStore:
    """Metadata-only cloud ledger. Note titles and bodies are never stored."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._initialized = False

    def record(
        self,
        *,
        operation_id: str,
        bridge_id: str,
        connection_epoch: str,
        action: str,
        document_key_hash: str,
        request_hash: str,
        content_hash: str,
        status: str,
        error_code: str = "",
    ) -> None:
        self._ensure_schema()
        now = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            _ = conn.execute(
                """
                INSERT INTO bridge_operations(
                    operation_id, bridge_id, connection_epoch, action,
                    document_key_hash, request_hash, content_hash, status,
                    error_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    connection_epoch=excluded.connection_epoch,
                    status=excluded.status,
                    error_code=excluded.error_code,
                    updated_at=excluded.updated_at
                """,
                (
                    operation_id,
                    bridge_id,
                    connection_epoch,
                    action,
                    document_key_hash,
                    request_hash,
                    content_hash,
                    status,
                    error_code,
                    now,
                    now,
                ),
            )

    def get(self, operation_id: str) -> BridgeAuditRecord | None:
        self._ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM bridge_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return BridgeAuditRecord(**dict(row)) if row is not None else None

    def recover_incomplete(self) -> dict[str, int]:
        """Classify operations left in-flight by a previous cloud process.

        The cloud ledger intentionally has no payload with which to resume. A
        pre-commit operation is therefore known not to have been submitted,
        while a recorded commit boundary must remain uncertain and must never
        be replayed automatically.
        """

        self._ensure_schema()
        now = datetime.now().astimezone().isoformat()
        with self._connect() as conn:
            before_commit = conn.execute(
                """
                UPDATE bridge_operations
                SET status='skipped_offline',
                    error_code='cloud_restarted_before_commit', updated_at=?
                WHERE status IN ('proposed', 'proposal_accepted')
                """,
                (now,),
            ).rowcount
            after_commit = conn.execute(
                """
                UPDATE bridge_operations
                SET status='outcome_unknown',
                    error_code='cloud_restarted_after_commit', updated_at=?
                WHERE status='commit_sent'
                """,
                (now,),
            ).rowcount
        return {
            "skipped_before_commit": max(0, int(before_commit)),
            "unknown_after_commit": max(0, int(after_commit)),
        }

    def _ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                _ = conn.execute("""
                    CREATE TABLE IF NOT EXISTS bridge_operations(
                        operation_id TEXT PRIMARY KEY,
                        bridge_id TEXT NOT NULL,
                        connection_epoch TEXT NOT NULL,
                        action TEXT NOT NULL,
                        document_key_hash TEXT NOT NULL,
                        request_hash TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        status TEXT NOT NULL,
                        error_code TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """)
                _ = conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_bridge_status ON bridge_operations(status, updated_at)"
                )
            self._initialized = True

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, factory=_ClosingConnection)
        conn.row_factory = sqlite3.Row
        return conn


__all__ = ["BridgeAuditRecord", "NotesBridgeAuditStore"]
