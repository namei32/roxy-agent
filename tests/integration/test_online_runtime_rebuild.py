from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import akasha.application.runtime as runtime_module
from akasha.application.rebuild import rebuild_memory
from akasha.application.runtime import OnlineMemoryRuntime
from akasha.domain.model import MemoryConfig
from akasha.infrastructure.loader import load_turns
from akasha.infrastructure.persistence import (
    load_memory_state,
    sha256_file,
)
from akasha.infrastructure.sparse_index import (
    BuildConfig,
    audit_source_embeddings,
)


def test_online_commit_matches_clean_rebuild(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions.db"
    online_index = tmp_path / "online-index.db"
    online_memory = tmp_path / "online-memory.db"
    replay_memory = tmp_path / "replay-memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    _append_turn(sessions, 2, "alpha follows", [0.9, 0.1])
    config = MemoryConfig()
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=online_index,
        memory_path=online_memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=config,
    )

    started = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        minutes=4
    )
    _, ticket = runtime.query_turn(
        text="alpha returns",
        dense=np.asarray([0.8, 0.2], dtype=np.float32),
        session_key="test:one",
        timestamp=started,
    )
    _append_turn(sessions, 4, "alpha returns", [0.8, 0.2])
    runtime.commit_from_source(
        user_message_id="message:4",
        assistant_message_id="message:5",
        ticket=ticket,
    )
    rebuild_memory(
        online_index,
        replay_memory,
        target_sequences=(),
    )

    online = _logical_state(online_memory, online_index, config)
    replay = _logical_state(replay_memory, online_index, config)
    assert online == replay
    assert _table_count(online_memory, "recall_runs") == 3
    assert _table_count(replay_memory, "recall_runs") == 3
    assert _table_count(replay_memory, "activation_runs") == 0
    runtime.close()


def test_message_feedback_matches_online_restore_and_clean_rebuild(
    tmp_path: Path,
) -> None:
    """Consume one canonical marker stream in all three execution paths."""

    # 1. Publish a correction that forgets one turn and reinforces another.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "online-index.db"
    online_memory = tmp_path / "online-memory.db"
    replay_memory = tmp_path / "replay-memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "old claim", [1.0, 0.0])
    _append_turn(sessions, 2, "correct claim", [0.0, 1.0])
    config = MemoryConfig()
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=online_memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=config,
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        minutes=4
    )
    _, ticket = runtime.query_turn(
        text="apply the correction",
        dense=np.asarray([0.7, 0.7], dtype=np.float32),
        session_key="test:one",
        timestamp=started,
    )
    _append_turn(
        sessions,
        4,
        "apply the correction",
        [0.7, 0.7],
        user_extra={
            "akasha_forget": {
                "schema_version": 1,
                "action": "forget",
                "target_turn_ids": ["message:0::message:1"],
            },
            "akasha_reinforce": {
                "schema_version": 1,
                "action": "remember",
                "target_turn_ids": ["message:2::message:3"],
                "boost": 3.0,
            },
        },
    )
    runtime.commit_from_source(
        user_message_id="message:4",
        assistant_message_id="message:5",
        ticket=ticket,
    )
    rebuild_memory(index, replay_memory, target_sequences=())

    # 2. Compare exact learned state and the persisted feedback audit.
    assert runtime.cycle.inhibited_nodes == {0}
    assert _logical_state(online_memory, index, config) == _logical_state(
        replay_memory,
        index,
        config,
    )
    with sqlite3.connect(online_memory) as connection:
        feedback_rows = connection.execute(
            """
            SELECT event_id, action, target_turn_node_id, boost
            FROM feedback_events
            ORDER BY event_id, action, target_turn_node_id
            """
        ).fetchall()
    assert feedback_rows == [
        (2, "forget", 0, 1.0),
        (2, "remember", 1, 3.0),
    ]

    # 3. A future cue may use the old turn as a hidden bridge but cannot emit it.
    query, future = runtime.query_turn(
        text="old claim",
        dense=np.asarray([1.0, 0.0], dtype=np.float32),
        session_key="test:one",
        timestamp=started + timedelta(minutes=2),
    )
    assert query.node_id == 3
    assert 0 in dict(future.evidence.seed)
    assert float(future.diffusion.reserve[0]) > 0.0
    assert 0 not in {
        item.node_id for item in future.completion.items
    }
    runtime.close()

    restored = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=online_memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=config,
    )
    assert restored.cycle.inhibited_nodes == {0}
    restored.close()


def test_staged_commit_publishes_the_same_logical_state(
    tmp_path: Path,
) -> None:
    """Keep graph state unchanged until an explicitly staged suffix publishes."""

    # 1. Restore one turn and durably stage the next source pair.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "online-index.db"
    memory = tmp_path / "online-memory.db"
    replay_memory = tmp_path / "replay-memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    _append_turn(sessions, 2, "alpha follows", [0.9, 0.1])
    staged = runtime.stage_from_source(
        user_message_id="message:2",
        assistant_message_id="message:3",
        ticket=None,
    )

    assert runtime.cycle.state_version == 1
    assert _table_count(index, "sparse_turns") == 2
    assert _table_count(memory, "turn_nodes") == 1

    # 2. Publish the suffix and compare it with a clean replay.
    runtime.publish_staged(staged)
    rebuild_memory(index, replay_memory, target_sequences=())

    assert runtime.cycle.state_version == 2
    assert _logical_state(memory, index, MemoryConfig()) == _logical_state(
        replay_memory,
        index,
        MemoryConfig(),
    )
    runtime.close()


def test_failed_publication_restores_the_durable_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback mutated in-memory state from the durable database truth."""

    # 1. Stage a real suffix from a published one-turn prefix.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "online-index.db"
    memory = tmp_path / "online-memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    durable_hash = sha256_file(memory)
    _append_turn(
        sessions,
        2,
        "alpha follows",
        [0.9, 0.1],
        user_extra={
            "akasha_forget": {
                "schema_version": 1,
                "action": "forget",
                "target_turn_ids": ["message:0::message:1"],
            }
        },
    )
    staged = runtime.stage_from_source(
        user_message_id="message:2",
        assistant_message_id="message:3",
        ticket=None,
    )

    # 2. Fail the durable boundary after learning mutated the live cache.
    def fail_write(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise OSError("injected snapshot failure")

    monkeypatch.setattr(runtime_module, "write_memory_database", fail_write)
    with pytest.raises(OSError, match="injected snapshot failure"):
        runtime.publish_staged(staged)

    assert runtime.cycle.state_version == 1
    assert runtime.cycle.graph.current_event == 0
    assert runtime.cycle.inhibited_nodes == set()
    assert [turn.turn_id for turn in runtime.cycle.turns] == [
        "message:0::message:1"
    ]
    assert sha256_file(memory) == durable_hash

    # 3. The same staged write can retry from the restored version.
    monkeypatch.undo()
    runtime.publish_staged(staged)
    assert runtime.cycle.state_version == 2
    assert runtime.cycle.inhibited_nodes == {0}
    runtime.close()


def test_first_publication_failure_restores_an_empty_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback the first turn when no durable memory database exists yet."""

    sessions = tmp_path / "sessions.db"
    index = tmp_path / "online-index.db"
    memory = tmp_path / "online-memory.db"
    _create_sessions(sessions)
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    staged = runtime.stage_from_source(
        user_message_id="message:0",
        assistant_message_id="message:1",
        ticket=None,
    )

    def fail_write(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise OSError("injected first snapshot failure")

    monkeypatch.setattr(runtime_module, "write_memory_database", fail_write)
    with pytest.raises(OSError, match="injected first snapshot failure"):
        runtime.publish_staged(staged)

    assert runtime.cycle.state_version == 0
    assert runtime.cycle.graph.current_event == -1
    assert not runtime.cycle.turns
    assert not memory.exists()
    runtime.close()


def test_restart_recovers_a_staged_unpublished_suffix(
    tmp_path: Path,
) -> None:
    """Recover a durable sparse suffix after the graph publisher disappears."""

    # 1. Stage one source pair and simulate exit before graph publication.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "online-index.db"
    memory = tmp_path / "online-memory.db"
    replay_memory = tmp_path / "replay-memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    _append_turn(sessions, 2, "alpha follows", [0.9, 0.1])
    runtime.stage_from_source(
        user_message_id="message:2",
        assistant_message_id="message:3",
        ticket=None,
    )
    runtime.close()

    # 2. Startup catch-up must converge to the same clean replay state.
    replacement = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    rebuild_memory(index, replay_memory, target_sequences=())

    assert replacement.cycle.state_version == 2
    assert _logical_state(memory, index, MemoryConfig()) == _logical_state(
        replay_memory,
        index,
        MemoryConfig(),
    )
    replacement.close()


def test_runtime_loaders_do_not_run_full_database_integrity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load owned sidecars without scanning every SQLite page."""

    # 1. Build real sparse and memory sidecars before observing read statements.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "index.db"
    memory = tmp_path / "memory.db"
    config = MemoryConfig()
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=config,
    )
    runtime.close()

    # 2. Trace real SQLite reads and reject a return to full integrity scans.
    statements: list[str] = []
    original_connect = sqlite3.connect

    def traced_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    turns = load_turns(index)
    load_memory_state(
        memory,
        turns=turns,
        config=config,
        source_index_sha256=sha256_file(index),
    )

    assert not any(
        statement.strip().upper().startswith("PRAGMA INTEGRITY_CHECK")
        for statement in statements
    )


def test_online_commit_without_ticket_persists_recall_run(
    tmp_path: Path,
) -> None:
    """Recompute a missing live ticket without losing its recall audit."""

    # 1. Restore one source turn, then append a second without a pending query.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "index.db"
    memory = tmp_path / "memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    _append_turn(sessions, 2, "alpha follows", [0.9, 0.1])

    # 2. The causal fallback still evaluates and persists one recall run.
    runtime.commit_from_source(
        user_message_id="message:2",
        assistant_message_id="message:3",
        ticket=None,
    )
    assert _table_count(memory, "recall_runs") == 2
    runtime.close()


def test_online_runtime_rejects_a_second_writer(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "online-index.db"
    memory = tmp_path / "online-memory.db"
    _create_sessions(sessions)
    first = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )

    with pytest.raises(RuntimeError, match="already has a writer"):
        OnlineMemoryRuntime(
            sessions_path=sessions,
            index_path=index,
            memory_path=memory,
            embedding_model="text-embedding-v4",
            embedding_dimension=2,
            config=MemoryConfig(),
        )

    first.close()
    replacement = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    replacement.close()


def test_online_runtime_ignores_sparse_index_page_identity(
    tmp_path: Path,
) -> None:
    """Restore from equal sparse evidence after SQLite page metadata changes."""

    # 1. Persist one valid snapshot and change only SQLite file metadata.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "online-index.db"
    memory = tmp_path / "online-memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    first = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    first.close()
    original_hash = sha256_file(index)
    with sqlite3.connect(index) as connection:
        connection.execute("PRAGMA user_version = 1")
    assert sha256_file(index) != original_hash

    # 2. Restore through config, count, and exact turn-message bindings.
    replacement = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    assert [turn.turn_id for turn in replacement.cycle.turns] == [
        "message:0::message:1"
    ]
    replacement.close()


def test_online_runtime_interprets_naive_host_time_like_replay(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions.db"
    _create_sessions(sessions)
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=tmp_path / "index.db",
        memory_path=tmp_path / "memory.db",
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )

    cue, _ = runtime.query_turn(
        text="local time",
        dense=np.asarray([1.0, 0.0], dtype=np.float32),
        session_key="test:one",
        timestamp=datetime(2026, 1, 1, 8),
    )

    assert cue.started_at == "2026-01-01T00:00:00+00:00"
    runtime.close()


def test_concurrent_queries_share_one_unchanged_published_graph(
    tmp_path: Path,
) -> None:
    """Serialize reversible previews inside the runtime state owner."""

    sessions = tmp_path / "sessions.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "alpha start", [1.0, 0.0])
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=tmp_path / "index.db",
        memory_path=tmp_path / "memory.db",
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=MemoryConfig(),
    )
    published_graph = id(runtime.cycle.graph)
    timestamp = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(
        minutes=2
    )

    def query(session: str) -> tuple[int, int]:
        turn, ticket = runtime.query_turn(
            text="alpha concurrent",
            dense=np.asarray([1.0, 0.0], dtype=np.float32),
            session_key=session,
            timestamp=timestamp,
        )
        return turn.node_id, ticket.prepared_state.current_event

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(
            executor.map(query, ("test:a", "test:b"))
        )

    assert results == ((1, 1), (1, 1))
    assert id(runtime.cycle.graph) == published_graph
    assert runtime.cycle.state_version == 1
    assert runtime.cycle.graph.current_event == 0
    assert runtime.cycle.graph.turn_count == 1
    runtime.close()


def test_interrupted_history_matches_online_non_learning(
    tmp_path: Path,
) -> None:
    """Keep a persisted interrupted marker out of online and replay state."""

    # 1. Mix one completed turn with one legacy interrupted marker.
    sessions = tmp_path / "sessions.db"
    index = tmp_path / "index.db"
    online_memory = tmp_path / "online-memory.db"
    replay_memory = tmp_path / "replay-memory.db"
    _create_sessions(sessions)
    _append_turn(sessions, 0, "completed", [1.0, 0.0])
    _append_interrupted_turn(sessions, 2, "unfinished")

    # 2. Restore online state and rebuild from its filtered causal index.
    config = MemoryConfig()
    runtime = OnlineMemoryRuntime(
        sessions_path=sessions,
        index_path=index,
        memory_path=online_memory,
        embedding_model="text-embedding-v4",
        embedding_dimension=2,
        config=config,
    )
    audit = audit_source_embeddings(
        sessions,
        BuildConfig(
            embedding_model="text-embedding-v4",
            embedding_dimension=2,
        ),
    )
    rebuild_memory(index, replay_memory, target_sequences=())

    assert audit.complete
    assert audit.excluded_interrupted_turns == 1
    assert [turn.turn_id for turn in runtime.cycle.turns] == [
        "message:0::message:1"
    ]
    assert _logical_state(online_memory, index, config) == _logical_state(
        replay_memory,
        index,
        config,
    )
    runtime.close()


def _logical_state(
    memory: Path,
    index: Path,
    config: MemoryConfig,
) -> dict[str, object]:
    turns = load_turns(index)
    graph, events, evidence, context, recalls, burst_members = (
        load_memory_state(
            memory,
            turns=turns,
            config=config,
            source_index_sha256=sha256_file(index),
        )
    )
    return {
        "turns": [turn.turn_id for turn in turns],
        "source": graph.source,
        "target": graph.target,
        "kind": graph.kind,
        "weights": graph.weight,
        "hubs": [asdict(hub) for hub in graph.hubs],
        "events": [asdict(event) for event in events],
        "evidence": [
            {
                **asdict(item),
                "channels": {
                    name: sorted(members)
                    for name, members in item.channels.items()
                },
            }
            for item in evidence
        ],
        "recalls": [asdict(item) for item in recalls],
        "context": {
            "members": context.members,
            "dense": (
                None
                if context.dense is None
                else context.dense.tolist()
            ),
            "terms": context.terms,
        },
        "burst_members": burst_members,
    }


def _table_count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {table}"
        ).fetchone()
    if row is None:
        raise RuntimeError(f"{table} count query returned no row")
    return int(row[0])


def _create_sessions(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE sessions (key TEXT PRIMARY KEY, metadata TEXT);
                INSERT INTO sessions VALUES ('test:one', '{}');
                CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                extra TEXT,
                ts TEXT NOT NULL,
                UNIQUE(session_key, seq)
            );
            CREATE TABLE message_embeddings (
                message_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                embedding BLOB NOT NULL,
                dim INTEGER NOT NULL,
                PRIMARY KEY(message_id, model)
            );
            """
        )


def _append_turn(
    path: Path,
    sequence: int,
    text: str,
    raw_vector: list[float],
    user_extra: dict[str, object] | None = None,
) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    user_time = base + timedelta(minutes=sequence)
    assistant_time = user_time + timedelta(seconds=10)
    vector = np.asarray(raw_vector, dtype=np.float32)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"message:{sequence}",
                    "test:one",
                    sequence,
                    "user",
                    text,
                    (
                        None
                        if user_extra is None
                        else json.dumps(user_extra)
                    ),
                    user_time.isoformat(),
                ),
                (
                    f"message:{sequence + 1}",
                    "test:one",
                    sequence + 1,
                    "assistant",
                    f"answer {text}",
                    None,
                    assistant_time.isoformat(),
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO message_embeddings VALUES (?, ?, ?, ?, ?)",
            [
                (
                    f"message:{sequence}",
                    hashlib.sha256(text.encode()).hexdigest(),
                    "text-embedding-v4",
                    vector.tobytes(),
                    vector.size,
                ),
                (
                    f"message:{sequence + 1}",
                    hashlib.sha256(
                        f"answer {text}".encode()
                    ).hexdigest(),
                    "text-embedding-v4",
                    vector.tobytes(),
                    vector.size,
                ),
            ],
        )


def _append_interrupted_turn(
    path: Path,
    sequence: int,
    text: str,
) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    user_time = base + timedelta(minutes=sequence)
    with sqlite3.connect(path) as connection:
        connection.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"message:{sequence}",
                    "test:one",
                    sequence,
                    "user",
                    text,
                    None,
                    user_time.isoformat(),
                ),
                (
                    f"message:{sequence + 1}",
                    "test:one",
                    sequence + 1,
                    "assistant",
                    "[interrupted]",
                    None,
                    (user_time + timedelta(seconds=10)).isoformat(),
                ),
            ],
        )
