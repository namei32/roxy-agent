"""Live-only, authenticated bridge for remote Apple Notes writes."""

from .broker import NotesBridgeBroker
from .port import (
    NOTES_BRIDGE_SERVICE_ID,
    NotesBridgeOperation,
    NotesBridgeResult,
    NotesBridgeService,
)

__all__ = [
    "NOTES_BRIDGE_SERVICE_ID",
    "NotesBridgeBroker",
    "NotesBridgeOperation",
    "NotesBridgeResult",
    "NotesBridgeService",
]
