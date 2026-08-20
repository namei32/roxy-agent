from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

NOTES_BRIDGE_SERVICE_ID = "notes_bridge.broker.v1"

NotesAction = Literal["create", "append", "find"]
NotesBridgeStatus = Literal[
    "committed",
    "not_found",
    "operation_rejected",
    "unit_failed",
    "outcome_unknown",
    "skipped_offline",
]


@dataclass(frozen=True)
class NotesBridgeOperation:
    operation_id: str
    action: NotesAction
    account: str
    folder: str
    create_folder_if_missing: bool = False
    document_key: str = ""
    title: str = ""
    html: str = ""
    note_id: str = ""
    marker: str = ""


@dataclass(frozen=True)
class NotesBridgeResult:
    status: NotesBridgeStatus
    operation_id: str
    note_id: str = ""
    folder_id: str = ""
    error_code: str = ""
    detail: str = ""


class NotesBridgeService(Protocol):
    def is_online(self) -> bool: ...

    def connection_status(self) -> dict[str, object]: ...

    async def execute(self, operation: NotesBridgeOperation) -> NotesBridgeResult: ...


__all__ = [
    "NOTES_BRIDGE_SERVICE_ID",
    "NotesAction",
    "NotesBridgeOperation",
    "NotesBridgeResult",
    "NotesBridgeService",
    "NotesBridgeStatus",
]
