from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import threading
from typing import cast

from agent.plugins.context import PluginKVStore

_STATE_KEY = "interview_coach_state"
_STATE_VERSION = 1
_WORKFLOW_STATUSES = frozenset({"prepared", "active", "paused", "completed"})
_NOTE_STATUSES = frozenset({"pending", "committed", "failed", "outcome_unknown"})
_PENDING_OPERATIONS = frozenset({"", "create", "append"})


@dataclass(frozen=True)
class InterviewBatch:
    batch_id: str
    document_key: str
    title: str
    topic_summary: str
    follow_up_questions: tuple[str, ...]
    current_question_index: int
    workflow_status: str
    note_status: str
    pending_operation: str
    question_count: int
    roxy_related_count: int
    general_count: int
    created_at: str
    updated_at: str

    @property
    def akashic_related_count(self) -> int:
        """旧插件调用方读取的兼容别名。"""

        return self.roxy_related_count

    @property
    def current_question(self) -> str:
        if 0 <= self.current_question_index < len(self.follow_up_questions):
            return self.follow_up_questions[self.current_question_index]
        return ""

    @property
    def next_question(self) -> str:
        index = self.current_question_index + 1
        if 0 <= index < len(self.follow_up_questions):
            return self.follow_up_questions[index]
        return ""


class InterviewStateStore:
    """在插件 KV 中保存批次映射和一次一道的恢复指针。"""

    def __init__(self, kv_store: PluginKVStore) -> None:
        self._kv = kv_store
        self._lock = threading.RLock()

    def prepare(
        self,
        *,
        session_key: str,
        media_paths: list[Path],
        title: str,
        topic_summary: str,
        follow_up_questions: list[str],
        question_count: int,
        roxy_related_count: int,
        general_count: int,
    ) -> tuple[InterviewBatch, bool]:
        """幂等创建一个 prepared 批次，正文仍由 Session 与 Notes 拥有。"""

        session_hash = _session_hash(session_key)
        batch_id = _batch_id(session_hash, media_paths)
        with self._lock:
            state = self._load()
            batches = cast(dict[str, object], state["batches"])
            existing = batches.get(batch_id)
            if existing is not None:
                batch = _parse_batch(existing, batch_id=batch_id)
                if (
                    batch.workflow_status == "prepared"
                    and batch.note_status == "failed"
                    and not batch.pending_operation
                ):
                    pending = cast(dict[str, object], state["pending_by_session"])
                    pending_id = pending.get(session_hash)
                    if isinstance(pending_id, str) and pending_id:
                        raise ValueError(
                            "当前会话已有尚未核对的面经 Notes 写入，必须先完成状态核对"
                        )
                    raw = cast(dict[str, object], existing)
                    raw["note_status"] = "pending"
                    raw["pending_operation"] = "create"
                    raw["updated_at"] = _now()
                    pending[session_hash] = batch_id
                    self._save(state)
                    batch = _parse_batch(raw, batch_id=batch_id)
                return batch, False

            pending = cast(dict[str, object], state["pending_by_session"])
            pending_id = pending.get(session_hash)
            if isinstance(pending_id, str) and pending_id:
                raise ValueError(
                    "当前会话已有尚未核对的面经 Notes 写入，必须先完成状态核对"
                )

            now = _now()
            raw: dict[str, object] = {
                "batch_id": batch_id,
                "session_hash": session_hash,
                "document_key": f"interview:{batch_id}",
                "title": title,
                "topic_summary": topic_summary,
                "follow_up_questions": list(follow_up_questions),
                "current_question_index": 0,
                "workflow_status": "prepared",
                "note_status": "pending",
                "pending_operation": "create",
                "question_count": question_count,
                "roxy_related_count": roxy_related_count,
                "general_count": general_count,
                "created_at": now,
                "updated_at": now,
            }
            batches[batch_id] = raw
            pending[session_hash] = batch_id
            self._save(state)
            return _parse_batch(raw, batch_id=batch_id), True

    def current_for_session(self, session_key: str) -> InterviewBatch | None:
        """返回当前 session 的活动或待核对批次。"""

        session_hash = _session_hash(session_key)
        with self._lock:
            state = self._load()
            pending = cast(dict[str, object], state["pending_by_session"])
            pending_id = pending.get(session_hash)
            if isinstance(pending_id, str) and pending_id:
                batches = cast(dict[str, object], state["batches"])
                raw = batches.get(pending_id)
                if raw is None:
                    raise ValueError(
                        f"Interview pending 指针引用不存在批次: {pending_id}"
                    )
                return _parse_batch(raw, batch_id=pending_id)
            active = cast(dict[str, object], state["active_by_session"])
            batch_id = active.get(session_hash)
            if not isinstance(batch_id, str) or not batch_id:
                return None
            batches = cast(dict[str, object], state["batches"])
            raw = batches.get(batch_id)
            if raw is None:
                raise ValueError(f"Interview active 指针引用不存在批次: {batch_id}")
            return _parse_batch(raw, batch_id=batch_id)

    def record_note_result(
        self,
        *,
        session_key: str,
        document_key: str,
        tool_name: str,
        note_status: str,
    ) -> InterviewBatch | None:
        """只按 Apple Notes 的结构化回执推进工作流。"""

        session_hash = _session_hash(session_key)
        with self._lock:
            state = self._load()
            batches = cast(dict[str, object], state["batches"])
            match = _find_document_batch(
                batches,
                document_key=document_key,
                session_hash=session_hash,
            )
            if match is None:
                return None
            batch_id, raw = match
            pending = _required_string(raw, "pending_operation")

            if tool_name == "apple_notes_create":
                self._apply_create_result(state, raw, batch_id, note_status)
            elif tool_name == "apple_notes_append":
                self._apply_append_result(state, raw, batch_id, note_status)
            elif tool_name == "apple_notes_status" and pending:
                if pending == "create":
                    self._apply_create_result(state, raw, batch_id, note_status)
                elif pending == "append":
                    self._apply_append_result(state, raw, batch_id, note_status)
            else:
                return _parse_batch(raw, batch_id=batch_id)

            raw["updated_at"] = _now()
            self._save(state)
            return _parse_batch(raw, batch_id=batch_id)

    def _apply_create_result(
        self,
        state: dict[str, object],
        raw: dict[str, object],
        batch_id: str,
        note_status: str,
    ) -> None:
        raw["note_status"] = note_status
        if note_status == "committed":
            self._activate(state, raw, batch_id)
            raw["pending_operation"] = ""
            self._clear_pending(state, raw, batch_id)
        elif note_status == "outcome_unknown":
            raw["pending_operation"] = "create"
        else:
            raw["pending_operation"] = ""
            self._clear_pending(state, raw, batch_id)

    def _apply_append_result(
        self,
        state: dict[str, object],
        raw: dict[str, object],
        batch_id: str,
        note_status: str,
    ) -> None:
        raw["note_status"] = note_status
        if note_status == "committed":
            index = _required_int(raw, "current_question_index") + 1
            questions = _required_string_list(raw, "follow_up_questions")
            raw["current_question_index"] = index
            raw["pending_operation"] = ""
            if index >= len(questions):
                raw["workflow_status"] = "completed"
                active = cast(dict[str, object], state["active_by_session"])
                session_hash = _required_string(raw, "session_hash")
                if active.get(session_hash) == batch_id:
                    active[session_hash] = ""
            else:
                raw["workflow_status"] = "active"
        elif note_status == "outcome_unknown":
            raw["pending_operation"] = "append"
        else:
            raw["pending_operation"] = ""
            raw["workflow_status"] = "active"

    def _activate(
        self,
        state: dict[str, object],
        raw: dict[str, object],
        batch_id: str,
    ) -> None:
        active = cast(dict[str, object], state["active_by_session"])
        batches = cast(dict[str, object], state["batches"])
        session_hash = _required_string(raw, "session_hash")
        previous_id = active.get(session_hash)
        if isinstance(previous_id, str) and previous_id and previous_id != batch_id:
            previous = batches.get(previous_id)
            if isinstance(previous, dict):
                previous["workflow_status"] = "paused"
                previous["updated_at"] = _now()
        active[session_hash] = batch_id
        raw["workflow_status"] = "active"

    @staticmethod
    def _clear_pending(
        state: dict[str, object],
        raw: dict[str, object],
        batch_id: str,
    ) -> None:
        pending = cast(dict[str, object], state["pending_by_session"])
        session_hash = _required_string(raw, "session_hash")
        if pending.get(session_hash) == batch_id:
            pending[session_hash] = ""

    def _load(self) -> dict[str, object]:
        raw = self._kv.get(
            _STATE_KEY,
            {
                "version": _STATE_VERSION,
                "batches": {},
                "active_by_session": {},
                "pending_by_session": {},
            },
        )
        if not isinstance(raw, dict):
            raise ValueError("Interview Coach KV 根必须是对象")
        state = cast(dict[str, object], raw)
        if state.get("version") != _STATE_VERSION:
            raise ValueError("Interview Coach KV 版本不受支持")
        if not isinstance(state.get("batches"), dict):
            raise ValueError("Interview Coach batches 必须是对象")
        if not isinstance(state.get("active_by_session"), dict):
            raise ValueError("Interview Coach active_by_session 必须是对象")
        if not isinstance(state.get("pending_by_session"), dict):
            raise ValueError("Interview Coach pending_by_session 必须是对象")
        return state

    def _save(self, state: dict[str, object]) -> None:
        self._kv.set(_STATE_KEY, state)


def _parse_batch(value: object, *, batch_id: str) -> InterviewBatch:
    if not isinstance(value, dict):
        raise ValueError(f"Interview batch 不是对象: {batch_id}")
    raw = cast(dict[str, object], value)
    stored_id = _required_string(raw, "batch_id")
    if stored_id != batch_id:
        raise ValueError(f"Interview batch identity 不匹配: {batch_id}")
    workflow_status = _required_string(raw, "workflow_status")
    note_status = _required_string(raw, "note_status")
    pending = _required_string(raw, "pending_operation")
    if workflow_status not in _WORKFLOW_STATUSES:
        raise ValueError(f"Interview workflow_status 非法: {workflow_status}")
    if note_status not in _NOTE_STATUSES:
        raise ValueError(f"Interview note_status 非法: {note_status}")
    if pending not in _PENDING_OPERATIONS:
        raise ValueError(f"Interview pending_operation 非法: {pending}")
    questions = tuple(_required_string_list(raw, "follow_up_questions"))
    index = _required_int(raw, "current_question_index")
    if index < 0 or index > len(questions):
        raise ValueError(f"Interview current_question_index 越界: {index}")
    return InterviewBatch(
        batch_id=stored_id,
        document_key=_required_string(raw, "document_key"),
        title=_required_string(raw, "title"),
        topic_summary=_required_string(raw, "topic_summary"),
        follow_up_questions=questions,
        current_question_index=index,
        workflow_status=workflow_status,
        note_status=note_status,
        pending_operation=pending,
        question_count=_required_int(raw, "question_count"),
        roxy_related_count=_required_int(
            raw,
            "roxy_related_count"
            if "roxy_related_count" in raw
            else "akashic_related_count",
        ),
        general_count=_required_int(raw, "general_count"),
        created_at=_required_string(raw, "created_at"),
        updated_at=_required_string(raw, "updated_at"),
    )


def _find_document_batch(
    batches: dict[str, object],
    *,
    document_key: str,
    session_hash: str,
) -> tuple[str, dict[str, object]] | None:
    for batch_id, value in batches.items():
        if not isinstance(value, dict):
            raise ValueError(f"Interview batch 不是对象: {batch_id}")
        raw = cast(dict[str, object], value)
        if (
            raw.get("document_key") == document_key
            and raw.get("session_hash") == session_hash
        ):
            return batch_id, raw
    return None


def _required_string(raw: dict[str, object], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str):
        raise ValueError(f"Interview batch.{field} 必须是字符串")
    return value


def _required_int(raw: dict[str, object], field: str) -> int:
    value = raw.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"Interview batch.{field} 必须是整数")
    return value


def _required_string_list(raw: dict[str, object], field: str) -> list[str]:
    value = raw.get(field)
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise ValueError(f"Interview batch.{field} 必须是非空字符串数组")
    return cast(list[str], value)


def _session_hash(session_key: str) -> str:
    if not session_key:
        raise ValueError("Interview session_key 不能为空")
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()


def _batch_id(session_hash: str, media_paths: list[Path]) -> str:
    digest = hashlib.sha256()
    digest.update(session_hash.encode("ascii"))
    for path in media_paths:
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                file_digest.update(chunk)
        digest.update(file_digest.digest())
    return digest.hexdigest()[:32]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
