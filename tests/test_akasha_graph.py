"""Exercise real SQLite graph browsing, source fencing, and zero-write behavior."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.plugins.mobile_ui import MobileUiRpcInvalidRequest
from plugins.akasha.graph_contract import GraphUnavailable, MAX_BYTES
from plugins.akasha.graph_queries import GraphQueries
from plugins.akasha.graph_reader import AkashaGraphReader
from plugins.akasha.infrastructure.persistence import _SCHEMA
from plugins.akasha.infrastructure.sparse_index.schema import SCHEMA as INDEX_SCHEMA
from plugins.akasha.plugin import AkashaPlugin
from tests.test_akasha_plugin import (
    _create_sessions,
    _append_turn,
    _engine,
    _Embedder,
    _event,
)


def node_id(kind: str, index: int) -> str:
    value = f"message:{index * 2}::message:{index * 2 + 1}"
    return kind + ":" + hashlib.sha256(value.encode()).hexdigest()


def graph_fixture(root: Path, count: int = 26) -> AkashaGraphReader:
    """Create a known overlapping topology using real production SQLite schemas."""

    root.mkdir(parents=True, exist_ok=True)
    sessions, memory, sparse = (
        root / name for name in ("sessions.db", "graph.db", "index.db")
    )
    _create_sessions(sessions)
    started = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with (
        closing(sqlite3.connect(memory)) as graph,
        closing(sqlite3.connect(sparse)) as index,
    ):
        graph.executescript(_SCHEMA)
        index.executescript(INDEX_SCHEMA)
        graph.execute("INSERT INTO metadata VALUES ('turn_count',?)", (str(count),))
        for i in range(count):
            user = f"记忆 {i}：徒步与阅读"
            assistant = f"第 {i} 次复盘，保留原始记录。"
            if i == count - 1:
                user = "北极星秘密项目 " + "🧭星\x00" * 2100
                assistant = (
                    '<img src=x onerror="window.attacked=true">' + "回复📖" * 1500
                )
            timestamp = started + timedelta(minutes=i)
            _append_turn(
                sessions,
                sequence=i * 2,
                user=user,
                assistant=assistant,
                started=timestamp,
            )
            values = (
                f"message:{i * 2}::message:{i * 2 + 1}",
                "test:one",
                i * 2,
                f"message:{i * 2}",
                f"message:{i * 2 + 1}",
                timestamp.isoformat(),
                (timestamp + timedelta(seconds=10)).isoformat(),
            )
            graph.execute(
                "INSERT INTO turn_nodes VALUES (?,?,?,?,?,?,?,?,?)", (i, *values, None)
            )
            index.execute(
                "INSERT INTO sparse_turns VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*values, user, assistant, "[]", "[]", 1.0, "fixture"),
            )
        if count:
            assert count >= 4
            edge = 0
            for offset, origin in enumerate((1, 3)):
                members = range(count) if offset == 0 else range(0, count, 2)
                hub = count + 1000 + offset
                graph.execute(
                    "INSERT INTO hub_nodes VALUES (?,?,?,?,?,?)",
                    (hub, origin, origin, 0.1, 0.5, len(members)),
                )
                for turn in members:
                    graph.execute(
                        "INSERT INTO hub_memberships VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            edge,
                            turn,
                            hub,
                            0.5,
                            0.25,
                            0,
                            0,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            1.0,
                            0.0,
                            0.0,
                        ),
                    )
                    edge += 1
            for i in range(count - 1):
                for source, target, kind in (
                    (i, i + 1, "temporal_forward"),
                    (i + 1, i, "temporal_backward"),
                ):
                    graph.execute(
                        "INSERT INTO temporal_edges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            edge,
                            kind,
                            source,
                            target,
                            0.5,
                            0.25,
                            0,
                            0,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            0.0,
                            1.0,
                            0.0,
                            0.0,
                        ),
                    )
                    edge += 1
        graph.commit()
        index.commit()
    return AkashaGraphReader(memory, sparse, sessions)


@pytest.fixture
def reader(tmp_path: Path) -> AkashaGraphReader:
    return graph_fixture(tmp_path)


def pinned(reader: AkashaGraphReader) -> str:
    return reader.query("graph.overview", {})["revision"]


def disk_state(reader: AkashaGraphReader) -> dict:
    return {
        p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in (reader.memory, reader.index, reader.sessions)
    }


def test_known_graph_counts_shared_members_direction_and_full_paging(reader):
    overview = reader.query("graph.overview", {})
    assert overview["totals"] == {"turns": 26, "groups": 2, "edges": 89}
    groups = {item["id"]: item for item in overview["nodes"]}
    assert groups[node_id("hub", 1)]["member_count"] == 26
    assert groups[node_id("hub", 1)]["shared_count"] == 13
    revision = overview["revision"]
    seen = []
    for page in range(4):
        result = reader.query(
            "graph.group",
            {"revision": revision, "node_id": node_id("hub", 1), "page": page},
        )
        assert len(result["nodes"]) <= 9
        seen.extend(node["id"] for node in result["nodes"] if node["kind"] == "turn")
        ids = {node["id"] for node in result["nodes"]}
        assert all(
            edge["source"] in ids and edge["target"] in ids for edge in result["edges"]
        )
        assert (
            len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
            < MAX_BYTES
        )
    assert seen == [node_id("turn", i) for i in range(26)]
    detail = reader.query(
        "graph.detail", {"revision": revision, "node_id": node_id("turn", 12)}
    )
    assert detail["node"]["group_count"] == 2
    assert {n["id"] for n in detail["related"]["nodes"] if n["kind"] == "hub"} == {
        node_id("hub", 1),
        node_id("hub", 3),
    }
    local = reader.query(
        "graph.neighbors",
        {"revision": revision, "node_id": node_id("turn", 12), "filter": "temporal"},
    )
    assert local["scope"]["total"] == 2
    assert all(edge["directed"] for edge in local["edges"])
    assert {(e["source"], e["target"], e["kind"]) for e in local["edges"]} == {
        (node_id("turn", 11), node_id("turn", 12), "temporal_forward"),
        (node_id("turn", 12), node_id("turn", 11), "temporal_backward"),
        (node_id("turn", 12), node_id("turn", 13), "temporal_forward"),
        (node_id("turn", 13), node_id("turn", 12), "temporal_backward"),
    }


def test_global_search_and_lossless_unicode_source_pages(reader):
    revision = pinned(reader)
    result = reader.query("graph.search", {"revision": revision, "query": "北极星"})
    assert [item["id"] for item in result["items"]] == [node_id("turn", 25)]
    for field in ("user", "assistant"):
        text, offset = "", 0
        while offset is not None:
            part = reader.query(
                "graph.source",
                {
                    "revision": revision,
                    "node_id": node_id("turn", 25),
                    "field": field,
                    "offset": offset,
                },
            )
            assert part["source"]["offset"] == offset
            text += part["source"]["text"]
            offset = part["source"]["next_offset"]
            assert len(json.dumps(part, ensure_ascii=False).encode()) < MAX_BYTES
        with closing(sqlite3.connect(reader.sessions)) as db:
            expected = db.execute(
                "SELECT content FROM messages WHERE id=?",
                (f"message:{50 if field == 'user' else 51}",),
            ).fetchone()[0]
        assert text == expected
        assert len(text) == part["source"]["total_chars"]


def test_browsing_has_no_write_attempt_and_mutant_is_denied(reader, monkeypatch):
    import plugins.akasha.graph_reader as module

    writes, original = [], module.read_authorizer

    def audit(action, a, b, db, trigger):
        result = original(action, a, b, db, trigger)
        if result != sqlite3.SQLITE_OK:
            writes.append((action, a, b, db))
        return result

    monkeypatch.setattr(module, "read_authorizer", audit)
    before = disk_state(reader)
    revision = pinned(reader)
    reader.query("graph.search", {"revision": revision, "query": "复盘"})
    reader.query("graph.detail", {"revision": revision, "node_id": node_id("turn", 12)})
    reader.query(
        "graph.neighbors",
        {"revision": revision, "node_id": node_id("hub", 1), "page": 1},
    )
    assert writes == [] and disk_state(reader) == before

    def mutant(self, method, payload):
        self.db.execute("UPDATE sessions.messages SET content='mutant'")

    monkeypatch.setattr(GraphQueries, "query", mutant)
    with pytest.raises(GraphUnavailable):
        reader.query("graph.overview", {})
    assert writes and writes[0][0] == sqlite3.SQLITE_UPDATE
    assert disk_state(reader) == before


def test_atomic_publication_rejects_old_version_and_preserves_hub_identity(reader):
    revision = pinned(reader)
    candidate = reader.memory.with_suffix(".candidate")
    shutil.copyfile(reader.memory, candidate)
    with closing(sqlite3.connect(candidate)) as db, db:
        db.execute("UPDATE hub_nodes SET node_id=node_id+100")
        db.execute("UPDATE hub_memberships SET hub_node_id=hub_node_id+100")
    os.replace(candidate, reader.memory)
    old = reader.query(
        "graph.detail", {"revision": revision, "node_id": node_id("hub", 1)}
    )
    assert old["status"] == "stale"
    assert "node" not in old
    fresh = reader.query(
        "graph.detail", {"revision": pinned(reader), "node_id": node_id("hub", 1)}
    )
    assert fresh["node"]["id"] == node_id("hub", 1)
    assert fresh["node"]["member_count"] == 26


def test_publication_during_query_does_not_return_mixed_state(reader, monkeypatch):
    original = GraphQueries.query

    def replace_during_query(self, method, payload):
        result = original(self, method, payload)
        candidate = reader.memory.with_suffix(".next")
        shutil.copyfile(reader.memory, candidate)
        os.replace(candidate, reader.memory)
        return result

    monkeypatch.setattr(GraphQueries, "query", replace_during_query)
    with pytest.raises(GraphUnavailable, match="发布"):
        reader.query("graph.overview", {})


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM messages WHERE id='message:4'",
        "UPDATE messages SET content='changed' WHERE id='message:4'",
        "UPDATE messages SET extra='{\"skip_post_memory\":true}' WHERE id='message:4'",
        "UPDATE messages SET role='assistant' WHERE id='message:4'",
        "UPDATE sessions SET metadata='{\"skip_post_memory\":true}'",
    ],
)
def test_deleted_or_changed_source_blocks_the_whole_graph(reader, mutation):
    with closing(sqlite3.connect(reader.sessions)) as db, db:
        db.execute(mutation)
    with pytest.raises(GraphUnavailable, match="来源已变化"):
        reader.query("graph.overview", {})


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM messages WHERE id='extra-input'",
        "UPDATE messages SET extra=json_set(extra, '$.skip_post_memory', json('true')) WHERE id='extra-input'",
    ],
)
def test_multi_input_source_is_complete_and_partial_revoke_is_unavailable(
    reader, mutation
):
    with closing(sqlite3.connect(reader.sessions)) as db, db:
        db.execute("UPDATE messages SET seq=100 WHERE id='message:1'")
        db.execute(
            "UPDATE messages SET extra=? WHERE id='message:0'",
            (json.dumps({"control_turn_id": "multi", "turn_input_ordinal": 0}),),
        )
        db.execute(
            "UPDATE messages SET extra=? WHERE id='message:1'",
            (
                json.dumps(
                    {
                        "control_turn_id": "multi",
                        "turn_terminal": True,
                        "turn_input_count": 2,
                    }
                ),
            ),
        )
        db.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)",
            (
                "extra-input",
                "test:one",
                1,
                "user",
                "第二条补充",
                None,
                json.dumps({"control_turn_id": "multi", "turn_input_ordinal": 1}),
                "2026-09-01T00:00:01+00:00",
            ),
        )
    with closing(sqlite3.connect(reader.index)) as db, db:
        db.execute(
            "UPDATE sparse_turns SET user_text=user_text||char(10)||char(10)||'第二条补充' WHERE user_seq=0"
        )
    result = reader.query(
        "graph.detail", {"revision": pinned(reader), "node_id": node_id("turn", 0)}
    )
    assert result["sources"]["user"]["text"].endswith("\n\n第二条补充")
    with closing(sqlite3.connect(reader.sessions)) as db, db:
        db.execute(mutation)
    with pytest.raises(GraphUnavailable):
        reader.query("graph.overview", {})


@pytest.mark.parametrize(
    "payload", [{"page": True}, {"page": -1}, {"page": "1"}, {"sql": "SELECT 1"}]
)
def test_invalid_inputs_fail_before_opening_files(tmp_path, payload):
    reader = AkashaGraphReader(tmp_path / "m", tmp_path / "i", tmp_path / "s")
    with pytest.raises(ValueError):
        reader.query("graph.overview", payload)
    assert list(tmp_path.iterdir()) == []


def test_missing_sidecar_and_disabled_owner_are_explicit(reader):
    reader.index.unlink()
    plugin = AkashaPlugin()
    plugin.context = SimpleNamespace(
        memory_engine=SimpleNamespace(
            describe=lambda: SimpleNamespace(name="akasha"), inspect_graph=reader.query
        )
    )
    result = plugin.mobile_ui_query("graph.overview", {}, session_id=None, turn_id=None)
    assert result["status"] == "unavailable"
    plugin.context.memory_engine = None
    assert (
        plugin.mobile_ui_query("graph.overview", {}, session_id=None, turn_id=None)[
            "status"
        ]
        == "disabled"
    )
    with pytest.raises(MobileUiRpcInvalidRequest):
        plugin.mobile_ui_query("graph.rebuild", {}, session_id=None, turn_id=None)


def test_thousand_turn_graph_remains_bounded_and_unexpanded_memory_is_searchable(
    tmp_path,
):
    reader = graph_fixture(tmp_path, 1000)
    revision = pinned(reader)
    page = reader.query(
        "graph.neighbors",
        {"revision": revision, "node_id": node_id("hub", 1), "page": 99},
    )
    assert page["scope"] == {
        "page": 99,
        "page_size": 10,
        "total": 1000,
        "start": 990,
        "end": 1000,
        "next_page": None,
    }
    assert len(page["nodes"]) == 11 and len(page["edges"]) < 200
    assert reader.query("graph.search", {"revision": revision, "query": "北极星"})[
        "items"
    ][0]["id"] == node_id("turn", 999)


@pytest.mark.asyncio
async def test_real_online_engine_graph_query_does_not_change_learning_or_pending(
    tmp_path, monkeypatch
):
    _create_sessions(tmp_path / "sessions.db")
    monkeypatch.setattr("plugins.akasha.engine.Embedder", _Embedder)
    engine = _engine(tmp_path)
    started = datetime(2026, 9, 1, tzinfo=timezone.utc)
    for i in range(4):
        timestamp = started + timedelta(minutes=i)
        user, assistant = f"alpha input {i}", f"alpha answer {i}"
        _append_turn(
            tmp_path / "sessions.db",
            sequence=i * 2,
            user=user,
            assistant=assistant,
            started=timestamp,
        )
        await engine._on_turn_committed(
            _event(sequence=i * 2, user=user, assistant=assistant, started=timestamp)
        )
        await engine._wait_for_publication()
    before = disk_state(engine._graph_reader)
    pending = dict(engine._pending)
    result = engine.inspect_graph("graph.overview", {})
    assert result["totals"]["turns"] == 4
    assert result["included_through"]["id"] == node_id("turn", 3)
    assert before == disk_state(engine._graph_reader) and engine._pending == pending
    engine._source_invalidated_error = RuntimeError("source revoked")
    with pytest.raises(GraphUnavailable):
        engine.inspect_graph("graph.overview", {})
    await engine.aclose()
    for item in engine.closeables:
        if item is not engine and hasattr(item, "close"):
            item.close()
