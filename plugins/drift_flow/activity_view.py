"""Drift 日常的只读公开投影；不初始化、迁移或修复任何状态。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from agent.plugins.mobile_ui import MobileUiRpcInvalidRequest
from plugins.drift_flow.activity_store import (
    ARTIFACT_BYTES, ARTIFACT_KINDS, ARTIFACT_LIMIT,
    activity_snapshot, bounded_text, utc_now,
)

DAILY_LIMIT = 24
DAILY_SCHEMA = "roxy.drift.daily.v1"
STEP_LIMIT = 24
CURRENT_LIMIT = 64
_TABLES = {"drift_activities", "drift_activity_steps", "drift_artifacts"}
_STATES = {"running", "completed", "paused", "failed", "interrupted"}


def identity(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{32}", value) is None:
        raise MobileUiRpcInvalidRequest("日常或成果身份无效")
    return value


def _stored_identity(value: object) -> str:
    try:
        return identity(value)
    except MobileUiRpcInvalidRequest as error:
        raise ValueError("日常持久身份无效") from error


def session_scope(payload: dict[str, object], *extra: str) -> list[str]:
    if set(payload) != {"session_ids", *extra}:
        raise MobileUiRpcInvalidRequest("日常查询参数不匹配")
    values = payload["session_ids"]
    if not isinstance(values, list) or len(values) > 256:
        raise MobileUiRpcInvalidRequest("日常查询需要最多 256 个会话身份")
    if any(not isinstance(value, str) or not value or len(value) > 256 for value in values):
        raise MobileUiRpcInvalidRequest("日常查询会话身份无效")
    if len(set(values)) != len(values):
        raise MobileUiRpcInvalidRequest("日常查询会话身份不得重复")
    return list(values)


def _date(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("日常记录时间无效")
    return value


def _checked(value: dict[str, object], limit: int = 96 * 1024) -> dict[str, object]:
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")) > limit:
        raise ValueError("日常投影超过响应预算，拒绝返回不完整结果")
    return value


class DriftActivityReader:
    def __init__(self, workspace: Path) -> None:
        self.db_file = workspace / "drift" / "drift.db"

    @contextmanager
    def _read(self) -> Iterator[tuple[sqlite3.Connection, tuple[str, ...]] | None]:
        if not self.db_file.is_file():
            yield None
            return
        db = sqlite3.connect(self.db_file.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with activity_snapshot(self.db_file) as live:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} & _TABLES
            if not present:
                yield None
            elif present != _TABLES:
                raise ValueError("日常记录表不完整")
            else:
                yield db, live
        finally:
            db.close()

    @staticmethod
    def _scope(sessions: list[str] | None, alias: str = "a") -> tuple[str, tuple[str, ...]]:
        # None 仅由 Agent 的 workspace 只读工具使用，移动查询必须传显式列表。
        if sessions is None:
            return "1", ()
        if not sessions:
            return "0", ()
        return f"{alias}.session_key IN ({','.join('?' for _ in sessions)})", tuple(sessions)

    def overview(self, sessions: list[str] | None) -> dict[str, object]:
        result: dict[str, object] = {"schema": DAILY_SCHEMA, "observed_at": utc_now(),
                                     "available": False, "current": [], "recent": [],
                                     "limit": DAILY_LIMIT, "has_more": False}
        with self._read() as snapshot:
            if snapshot is None:
                return result
            db, live = snapshot
            scope, args = self._scope(sessions)
            in_live = f"a.id IN ({','.join('?' for _ in live)})" if live else "0"
            # 1. 当前项必须同时有持久记录与仍被执行 owner 持有的作用域。
            current = db.execute(
                f"SELECT a.* FROM drift_activities a WHERE {scope} AND a.status='running' "
                f"AND {in_live} ORDER BY a.started_at,a.id LIMIT ?",
                (*args, *live, CURRENT_LIMIT + 1),
            ).fetchall()
            if len(current) > CURRENT_LIMIT:
                raise ValueError("当前自主活动超过可完整展示的容量")
            # 2. 已接续的旧项仍可按 ID 读取；概览只列当前接续链末端。
            recent = db.execute(
                f"SELECT a.* FROM drift_activities a WHERE {scope} AND NOT (a.status='running' AND {in_live}) "
                "AND NOT EXISTS (SELECT 1 FROM drift_activities next WHERE next.continues_id=a.id) "
                "ORDER BY a.updated_at DESC,a.id DESC LIMIT ?",
                (*args, *live, DAILY_LIMIT + 1),
            ).fetchall()
            result.update(available=True, current=[self._activity(db, row, live) for row in current],
                          recent=[self._activity(db, row, live) for row in recent[:DAILY_LIMIT]],
                          has_more=len(recent) > DAILY_LIMIT)
        return _checked(result)

    def activity(self, activity_id: str, sessions: list[str] | None) -> dict[str, object]:
        activity_id = identity(activity_id)
        result: dict[str, object] = {"schema": "roxy.drift.activity.v1", "observed_at": utc_now(),
                                     "available": False, "item": None}
        with self._read() as snapshot:
            if snapshot is None:
                return result
            db, live = snapshot
            result["available"] = True
            scope, args = self._scope(sessions)
            row = db.execute(f"SELECT a.* FROM drift_activities a WHERE a.id=? AND {scope}", (activity_id, *args)).fetchone()
            if row is None:
                return result
            item = self._activity(db, row, live)
            steps = db.execute(
                "SELECT id,label,status,started_at,ended_at FROM drift_activity_steps "
                "WHERE activity_id=? ORDER BY id DESC LIMIT ?", (activity_id, STEP_LIMIT + 1),
            ).fetchall()
            projected = []
            for step in reversed(steps[:STEP_LIMIT]):
                state = step["status"]
                if state not in {"running", "completed", "failed", "interrupted"}:
                    raise ValueError("日常阶段状态无效")
                if state == "running" and activity_id not in live:
                    state = "interrupted"
                projected.append({"id": str(step["id"]), "label": bounded_text(step["label"], 80, "阶段标题"),
                                  "status": state, "started_at": _date(step["started_at"]),
                                  "ended_at": _date(step["ended_at"], optional=True)})
            item.update(steps=projected, steps_limit=STEP_LIMIT, steps_has_more=len(steps) > STEP_LIMIT)
            result["item"] = item
        return _checked(result)

    def artifact(self, artifact_id: str, sessions: list[str] | None) -> dict[str, object]:
        artifact_id = identity(artifact_id)
        result: dict[str, object] = {"schema": "roxy.drift.artifact.v1", "observed_at": utc_now(),
                                     "available": False, "item": None}
        with self._read() as snapshot:
            if snapshot is None:
                return result
            db, _live = snapshot
            result["available"] = True
            scope, args = self._scope(sessions)
            row = db.execute(
                f"SELECT f.* FROM drift_artifacts f JOIN drift_activities a ON a.id=f.activity_id WHERE f.id=? AND {scope}",
                (artifact_id, *args),
            ).fetchone()
            if row is None:
                return result
            item = self._artifact_meta(row)
            content = row["content"]
            if not isinstance(content, str):
                raise ValueError("成果正文类型无效")
            raw = content.encode("utf-8")
            if len(raw) != item["size_bytes"] or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("成果正文与保存的摘要不匹配")
            item["content"] = content
            result["item"] = item
        return _checked(result)

    def _activity(self, db: sqlite3.Connection, row: sqlite3.Row, live: tuple[str, ...]) -> dict[str, object]:
        activity_id = _stored_identity(row["id"])
        status = row["status"]
        if status not in _STATES:
            raise ValueError("日常活动状态无效")
        owner_lost = status == "running" and activity_id not in live
        if owner_lost:
            status = "interrupted"
        successor = db.execute("SELECT id FROM drift_activities WHERE continues_id=? ORDER BY started_at DESC LIMIT 1", (activity_id,)).fetchone()
        rows = db.execute(
            "SELECT id,activity_id,title,kind,sha256,size_bytes,created_at FROM drift_artifacts "
            "WHERE activity_id=? ORDER BY created_at,id LIMIT ?", (activity_id, ARTIFACT_LIMIT + 1),
        ).fetchall()
        if len(rows) > ARTIFACT_LIMIT:
            raise ValueError("单次活动成果数超过已声明容量")
        delivery = row["delivery_status"]
        if delivery not in {"none", "pending", "confirmed", "unconfirmed"}:
            raise ValueError("活动投递状态无效")
        return {"id": activity_id, "title": bounded_text(row["title"], 80, "活动标题"),
                "category": bounded_text(row["category"], 32, "活动类别"), "status": status,
                "phase": "上次活动已中断" if owner_lost else bounded_text(row["phase"], 80, "活动阶段"),
                "summary": bounded_text(row["summary"], 280, "公开摘要", empty=True),
                "started_at": _date(row["started_at"]), "updated_at": _date(row["updated_at"]),
                "ended_at": _date(row["ended_at"], optional=True), "owner_lost": owner_lost,
                "continues_id": _stored_identity(row["continues_id"]) if row["continues_id"] else None,
                "continued_by": _stored_identity(successor["id"]) if successor else None,
                "delivery_status": delivery, "artifacts": [self._artifact_meta(item) for item in rows]}

    @staticmethod
    def _artifact_meta(row: sqlite3.Row) -> dict[str, object]:
        kind = row["kind"]
        size = row["size_bytes"]
        digest = row["sha256"]
        if kind not in ARTIFACT_KINDS or type(size) is not int or not 0 < size <= ARTIFACT_BYTES:
            raise ValueError("成果种类或大小无效")
        if not isinstance(digest, str) or re.fullmatch(r"[a-f0-9]{64}", digest) is None:
            raise ValueError("成果摘要无效")
        return {"id": _stored_identity(row["id"]), "activity_id": _stored_identity(row["activity_id"]),
                "title": bounded_text(row["title"], 80, "成果标题"), "kind": kind,
                "sha256": digest, "size_bytes": size, "created_at": _date(row["created_at"])}
