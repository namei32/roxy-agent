"""Read pinned SQLite projections without acquiring a memory writer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from session.memory_policy import excludes_memory

from .graph_contract import GraphUnavailable, public_id, response, validate_request
from .graph_queries import GraphQueries


def file_signature(path: Path) -> tuple[int, ...]:
    value = path.stat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def read_authorizer(
    action: int,
    arg1: str | None,
    arg2: str | None,
    database: str | None,
    trigger: str | None,
) -> int:
    """Deny mutations, attachment changes, and pragmas after read setup."""

    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_TRANSACTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def text_page(text: str, start: int, count: int) -> str:
    return text[start : start + count]


def source_excluded(session_key: str, raw: str | None) -> bool:
    metadata = json.loads(raw) if raw else {}
    if not isinstance(metadata, dict):
        raise ValueError("session metadata must be an object")
    return excludes_memory(session_key, metadata)


def message_skipped(raw: str | None) -> bool:
    extra = json.loads(raw) if raw else {}
    if not isinstance(extra, dict):
        raise ValueError("message extra must be an object")
    return bool(extra.get("skip_post_memory"))


class AkashaGraphReader:
    """Own only ephemeral query connections and a content-revision digest."""

    def __init__(self, memory: Path, index: Path, sessions: Path) -> None:
        self.memory, self.index, self.sessions = memory, index, sessions
        self._revision_cache: tuple[tuple[int, ...], str] | None = None

    def query(
        self, method: str, payload: dict[str, object], *, allow_empty: bool = False
    ) -> dict[str, object]:
        """Read one consistent publication, rejecting stale navigation."""

        # 1. Validate before I/O; a new runtime may have no published graph yet.
        validate_request(method, payload)
        if allow_empty and not self.memory.exists():
            return response("empty", message="尚无已发布的记忆图")
        try:
            signature = file_signature(self.memory)
            other_files = (
                file_signature(self.index)[:2],
                file_signature(self.sessions)[:2],
            )
            revision = self._revision(signature)
            with closing(self._connect()) as connection:
                # 2. BEGIN pins each attached read snapshot before validation.
                _ = connection.execute("BEGIN")
                metadata = dict(connection.execute("SELECT key, value FROM metadata"))
                connection.execute(
                    "SELECT COUNT(*) FROM sparse.sparse_turns"
                ).fetchone()
                connection.execute("SELECT COUNT(*) FROM sessions.messages").fetchone()
                self._validate(connection, metadata)
                if payload.get("revision", revision) != revision:
                    result = response(
                        "stale", revision=revision, message="记忆图已更新，请刷新后继续"
                    )
                else:
                    result = GraphQueries(connection, revision).query(method, payload)
                # 3. Atomic file replacement must not mix two publications.
                if signature != file_signature(self.memory) or other_files != (
                    file_signature(self.index)[:2],
                    file_signature(self.sessions)[:2],
                ):
                    raise GraphUnavailable("记忆图正在发布，请刷新后重试")
                return result
        except (OSError, sqlite3.DatabaseError) as exc:
            raise GraphUnavailable("记忆数据暂不可用，请稍后刷新") from exc

    def _revision(self, signature: tuple[int, ...]) -> str:
        if self._revision_cache and self._revision_cache[0] == signature:
            return self._revision_cache[1]
        digest = hashlib.sha256()
        with self.memory.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if signature != file_signature(self.memory):
            raise GraphUnavailable("记忆图正在发布，请刷新后重试")
        revision = digest.hexdigest()
        self._revision_cache = (signature, revision)
        return revision

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.memory.resolve().as_uri() + "?mode=ro", uri=True, timeout=2
        )
        try:
            connection.row_factory = sqlite3.Row
            for name, path in (("sparse", self.index), ("sessions", self.sessions)):
                _ = connection.execute(
                    f"ATTACH DATABASE ? AS {name}",
                    (path.resolve().as_uri() + "?mode=ro",),
                )
            _ = connection.execute("PRAGMA query_only = ON")
            if (
                connection.execute("PRAGMA application_id").fetchone()[0] != 1095452754
                or connection.execute("PRAGMA user_version").fetchone()[0] != 2
            ):
                raise GraphUnavailable("记忆图版本不受支持")
            connection.create_function("graph_id", 2, public_id, deterministic=True)
            connection.create_function("graph_length", 1, len, deterministic=True)
            connection.create_function(
                "graph_excluded", 2, source_excluded, deterministic=True
            )
            connection.create_function(
                "graph_skipped", 1, message_skipped, deterministic=True
            )
            connection.create_function(
                "graph_text_page",
                3,
                text_page,
                deterministic=True,
            )
            deadline = time.monotonic() + 5
            connection.set_progress_handler(
                lambda: int(time.monotonic() > deadline), 1000
            )
            connection.set_authorizer(read_authorizer)
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _validate(connection: sqlite3.Connection, metadata: dict[str, str]) -> None:
        """Validate the published prefix and all source anchors and text."""

        # 1. Staged sparse suffixes are allowed; every published binding is exact.
        count = connection.execute("SELECT COUNT(*) FROM turn_nodes").fetchone()[0]
        if metadata.get("turn_count") != str(count):
            raise GraphUnavailable("记忆图发布信息不完整")
        bad = connection.execute(_INVALID_SOURCE).fetchone()
        if bad is not None:
            raise GraphUnavailable("记忆来源已变化，等待有效图发布后再查看")
        # 2. Relationship endpoints must exist even outside the selected page.
        bad_edge = connection.execute("""
            SELECT 1 FROM hub_memberships e
            LEFT JOIN turn_nodes t ON t.node_id=e.turn_node_id
            LEFT JOIN hub_nodes h ON h.node_id=e.hub_node_id
            WHERE t.node_id IS NULL OR h.node_id IS NULL
            UNION ALL
            SELECT 1 FROM temporal_edges e
            LEFT JOIN turn_nodes s ON s.node_id=e.source_node_id
            LEFT JOIN turn_nodes t ON t.node_id=e.target_node_id
            WHERE s.node_id IS NULL OR t.node_id IS NULL
            UNION ALL
            SELECT 1 FROM hub_nodes h LEFT JOIN turn_nodes t ON t.node_id=h.created_event
            WHERE t.node_id IS NULL LIMIT 1
        """).fetchone()
        if bad_edge is not None:
            raise GraphUnavailable("记忆关系缺少有效来源")


_INVALID_SOURCE = """
SELECT 1 FROM turn_nodes t
LEFT JOIN sparse.sparse_turns s ON s.turn_id=t.turn_id
LEFT JOIN sessions.messages u ON u.id=t.user_message_id
LEFT JOIN sessions.messages a ON a.id=t.assistant_message_id
LEFT JOIN sessions.sessions ss ON ss.key=t.session_key
WHERE s.turn_id IS NULL OR u.id IS NULL OR a.id IS NULL OR ss.key IS NULL
 OR s.user_message_id IS NOT t.user_message_id
 OR s.assistant_message_id IS NOT t.assistant_message_id
 OR s.session_key IS NOT t.session_key OR s.user_seq IS NOT t.user_seq
 OR u.session_key IS NOT t.session_key OR a.session_key IS NOT t.session_key
 OR u.role IS NOT 'user' OR a.role IS NOT 'assistant'
 OR u.seq IS NOT t.user_seq OR u.ts IS NOT t.started_at OR a.ts IS NOT t.committed_at
 OR s.started_at IS NOT t.started_at OR s.committed_at IS NOT t.committed_at
 OR s.assistant_text IS NOT COALESCE(a.content, '')
 OR graph_excluded(t.session_key, ss.metadata)
 OR graph_skipped(u.extra) OR graph_skipped(a.extra)
 OR s.user_text IS NOT CASE
    WHEN json_extract(u.extra, '$.control_turn_id') IS NULL THEN COALESCE(u.content, '')
    ELSE (SELECT group_concat(content, char(10)||char(10)) FROM (
        SELECT COALESCE(m.content, '') AS content FROM sessions.messages m
        WHERE m.session_key=t.session_key AND m.role='user'
          AND json_extract(m.extra, '$.control_turn_id')=json_extract(u.extra, '$.control_turn_id')
        ORDER BY json_extract(m.extra, '$.turn_input_ordinal')
    )) END
 OR (json_extract(u.extra, '$.control_turn_id') IS NOT NULL AND (
    json_extract(a.extra, '$.control_turn_id') IS NOT json_extract(u.extra, '$.control_turn_id')
    OR json_extract(a.extra, '$.turn_terminal') IS NOT 1
    OR json_extract(a.extra, '$.turn_input_count') IS NOT (
        SELECT COUNT(*) FROM sessions.messages m WHERE m.session_key=t.session_key
          AND m.role='user'
          AND json_extract(m.extra, '$.control_turn_id')=json_extract(u.extra, '$.control_turn_id')
    ) OR EXISTS (
        SELECT 1 FROM sessions.messages m WHERE m.session_key=t.session_key
          AND m.role='user'
          AND json_extract(m.extra, '$.control_turn_id')=json_extract(u.extra, '$.control_turn_id')
          AND graph_skipped(m.extra)
    )))
LIMIT 1
"""
