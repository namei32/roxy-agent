"""Drift 执行 owner 的公开活动记录；不改变旧的选择与连续性记录。"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

ARTIFACT_BYTES = 32 * 1024
ARTIFACT_LIMIT = 8
ARTIFACT_KINDS = frozenset({"note", "story", "checklist", "other"})
_live: set[tuple[str, str]] = set()
_live_lock = threading.RLock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bounded_text(value: object, limit: int, field: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise ValueError(f"{field} 必须是最多 {limit} 字的文本")
    return value.strip()


def activity_is_live(path: Path, activity_id: str) -> bool:
    with _live_lock:
        return (str(path.resolve()), activity_id) in _live


def live_activity_ids(path: Path) -> tuple[str, ...]:
    resolved = str(path.resolve())
    with _live_lock:
        return tuple(identity for owner_path, identity in _live if owner_path == resolved)


@contextmanager
def activity_snapshot(path: Path) -> Iterator[tuple[str, ...]]:
    """在 SQLite 读取快照建立期间固定 owner 集合，避免结束竞态误报中断。"""
    with _live_lock:
        yield live_activity_ids(path)


class DriftActivityStore:
    """唯一 writer 接收执行事实；UI 使用独立的只读 reader。"""

    def __init__(self, db_file: Path) -> None:
        self.db_file = db_file

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_file, timeout=3)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """由已激活的 Drift owner 增加新表，保留所有旧表与记录。"""
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS drift_activities (
                    id TEXT PRIMARY KEY, session_key TEXT NOT NULL,
                    skill_name TEXT NOT NULL, title TEXT NOT NULL,
                    category TEXT NOT NULL, status TEXT NOT NULL,
                    phase TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    ended_at TEXT, continues_id TEXT,
                    delivery_status TEXT NOT NULL DEFAULT 'none'
                );
                CREATE INDEX IF NOT EXISTS drift_activities_session_time
                    ON drift_activities(session_key, started_at);
                CREATE INDEX IF NOT EXISTS drift_activities_continues
                    ON drift_activities(continues_id);
                CREATE TABLE IF NOT EXISTS drift_activity_steps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    activity_id TEXT NOT NULL, call_id TEXT NOT NULL,
                    label TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, ended_at TEXT,
                    UNIQUE(activity_id, call_id)
                );
                CREATE TABLE IF NOT EXISTS drift_artifacts (
                    id TEXT PRIMARY KEY, activity_id TEXT NOT NULL,
                    call_id TEXT NOT NULL, title TEXT NOT NULL,
                    kind TEXT NOT NULL, content TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(activity_id, call_id)
                );
            """)

    @contextmanager
    def execution(self) -> Iterator[str]:
        """进程内执行存活证据；异常与取消都必须释放，不靠计时猜测。"""
        activity_id = uuid4().hex
        key = (str(self.db_file.resolve()), activity_id)
        with _live_lock:
            _live.add(key)
        try:
            yield activity_id
        finally:
            with _live_lock:
                _live.discard(key)

    def start(self, activity_id: str, *, session_key: str, skill: str,
              title: str, category: str, continuing: bool) -> None:
        """选择成功后登记公开活动；idle 不进入这里。"""
        self._require_live(activity_id)
        title = bounded_text(title, 80, "活动标题")
        category = bounded_text(category, 32, "活动类别")
        now = utc_now()
        with self._connection() as db:
            existing = db.execute("SELECT session_key,skill_name FROM drift_activities WHERE id=?", (activity_id,)).fetchone()
            if existing is not None:
                if (existing["session_key"], existing["skill_name"]) != (session_key, skill):
                    raise RuntimeError("活动身份不能更换会话或技能 owner")
                return
            previous = None
            if continuing:
                live = live_activity_ids(self.db_file)
                owner_lost = f"id NOT IN ({','.join('?' for _ in live)})" if live else "1"
                previous = db.execute(
                    "SELECT id FROM drift_activities a WHERE session_key=? AND skill_name=? "
                    f"AND (status IN ('paused','failed','interrupted') OR (status='running' AND {owner_lost})) "
                    "AND NOT EXISTS (SELECT 1 FROM drift_activities b "
                    "WHERE b.continues_id=a.id) ORDER BY started_at DESC, id DESC LIMIT 1",
                    (session_key, skill, *live),
                ).fetchone()
            db.execute(
                "INSERT INTO drift_activities "
                "(id,session_key,skill_name,title,category,status,phase,started_at,updated_at,continues_id) "
                "VALUES (?,?,?,?,?,'running','准备活动',?,?,?)",
                (activity_id, session_key, skill, title, category, now, now,
                 previous["id"] if previous else None),
            )

    def has_activity(self, activity_id: str) -> bool:
        with self._connection() as db:
            return db.execute("SELECT 1 FROM drift_activities WHERE id=?", (activity_id,)).fetchone() is not None

    def start_step(self, activity_id: str, call_id: str, label: str) -> None:
        """仅保存公开阶段，不保存参数、内部推理或工具返回正文。"""
        self._require_live(activity_id)
        now = utc_now()
        with self._connection() as db:
            cursor = db.execute(
                "UPDATE drift_activities SET phase=?,updated_at=? WHERE id=? AND status='running'",
                (label, now, activity_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("活动执行 owner 不存在或已经结束")
            db.execute(
                "INSERT INTO drift_activity_steps (activity_id,call_id,label,status,started_at) "
                "VALUES (?,?,?,'running',?)",
                (activity_id, call_id, label, now),
            )

    def finish_step(self, activity_id: str, call_id: str, *, failed: bool) -> None:
        self._require_live(activity_id)
        now = utc_now()
        with self._connection() as db:
            db.execute(
                "UPDATE drift_activity_steps SET status=?,ended_at=? WHERE activity_id=? "
                "AND call_id=? AND status='running'",
                ("failed" if failed else "completed", now, activity_id, call_id),
            )
            db.execute("UPDATE drift_activities SET updated_at=? WHERE id=?", (now, activity_id))

    def finish(self, activity_id: str, *, status: str, summary: str = "",
               message_staged: bool = False) -> None:
        """封口执行事实；消息是否送达另由原 outbound 提交后确认。"""
        if status not in {"completed", "paused", "failed", "interrupted"}:
            raise ValueError("无效的活动终态")
        self._require_live(activity_id)
        summary = bounded_text(summary, 280, "公开活动摘要", empty=True)
        now = utc_now()
        phase = {"completed": "本轮已结束", "paused": "进度已保存", "failed": "本轮未能完成", "interrupted": "活动已中断"}[status]
        with self._connection() as db:
            db.execute(
                "UPDATE drift_activities SET status=?,phase=?,summary=?,updated_at=?,ended_at=?,"
                "delivery_status=? WHERE id=? AND status='running'",
                (status, phase, summary, now, now, "pending" if message_staged else "none", activity_id),
            )
            db.execute(
                "UPDATE drift_activity_steps SET status='interrupted',ended_at=? "
                "WHERE activity_id=? AND status='running'", (now, activity_id),
            )

    def record_delivery(self, activity_id: str, *, delivered: bool) -> None:
        """只记录确认情况；来信关联仍须匹配 SessionDB 中真实送达的消息。"""
        with self._connection() as db:
            db.execute(
                "UPDATE drift_activities SET delivery_status=?,updated_at=? WHERE id=?",
                ("confirmed" if delivered else "unconfirmed", utc_now(), activity_id),
            )

    def publish_artifact(self, activity_id: str, call_id: str, *, title: str,
                         kind: str, content: str) -> dict[str, object]:
        """当前执行显式发布不可变成果，同一个工具调用只能对应同一内容。"""
        self._require_live(activity_id)
        title = bounded_text(title, 80, "成果标题")
        call_id = bounded_text(call_id, 256, "成果调用身份")
        if kind not in ARTIFACT_KINDS:
            raise ValueError("成果种类必须为 note、story、checklist 或 other")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("成果正文不能为空")
        if any(ord(character) < 32 and character not in "\t\r\n" for character in content):
            raise ValueError("成果正文必须是可阅读文本，不能包含二进制控制符")
        encoded = content.encode("utf-8")
        if len(encoded) > ARTIFACT_BYTES:
            raise ValueError("成果正文超过 32 KiB，未保存；请缩小单份成果")
        digest = hashlib.sha256(encoded).hexdigest()
        with self._connection() as db:
            # 1. 先核对调用重放，再检查本次执行和容量。
            previous = db.execute(
                "SELECT id,title,kind,sha256 FROM drift_artifacts WHERE activity_id=? AND call_id=?",
                (activity_id, call_id),
            ).fetchone()
            if previous:
                if (previous["title"], previous["kind"], previous["sha256"]) != (title, kind, digest):
                    raise ValueError("同一成果调用不能替换已经保存的内容")
                return {"artifact_id": previous["id"], "activity_id": activity_id, "sha256": digest}
            active = db.execute("SELECT status FROM drift_activities WHERE id=?", (activity_id,)).fetchone()
            if active is None or active["status"] != "running":
                raise RuntimeError("成果必须属于正在执行的活动")
            count = db.execute("SELECT count(*) FROM drift_artifacts WHERE activity_id=?", (activity_id,)).fetchone()[0]
            if count >= ARTIFACT_LIMIT:
                raise ValueError("本次活动最多保存 8 份成果，已有成果保持不变")
            # 2. 追加完整正文；无覆盖、删除或任意文件路径读取。
            artifact_id = uuid4().hex
            db.execute(
                "INSERT INTO drift_artifacts (id,activity_id,call_id,title,kind,content,sha256,size_bytes,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (artifact_id, activity_id, call_id, title, kind, content, digest, len(encoded), utc_now()),
            )
            return {"artifact_id": artifact_id, "activity_id": activity_id, "sha256": digest}

    def _require_live(self, activity_id: str) -> None:
        if not activity_is_live(self.db_file, activity_id):
            raise RuntimeError("活动执行作用域已经结束或不存在")


def activity_source_refs(activity_id: str) -> list[dict[str, str]]:
    """来源由执行上下文注入，模型的发送参数没有改写这项身份的入口。"""
    if not activity_id:
        return []
    return [{"kind": "drift_activity", "id": activity_id, "source_name": "自主活动"}]


PUBLIC_PHASES = {
    "read_file": "阅读工作材料", "list_dir": "查阅工作资料", "recall_memory": "查阅记忆",
    "fetch_messages": "核对原对话", "search_messages": "寻找对话线索", "web_fetch": "阅读网页",
    "web_search": "寻找资料", "write_file": "保存工作文件", "edit_file": "更新工作文件",
    "shell": "处理活动资料", "write_stdin": "继续处理活动资料", "task_stop": "结束工具任务",
    "mount_server": "准备所需工具", "leave_artifact": "保存一份成果",
    "message_push": "准备一封来信", "finish_drift": "记录本轮结果",
}
