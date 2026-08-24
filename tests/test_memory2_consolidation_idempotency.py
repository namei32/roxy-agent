from typing import Any, cast
import asyncio

import pytest

from memory2.memorizer import Memorizer, _parse_history_entry_happened_at
from memory2.store import MemoryStore2


class _FakeEmbedder:
    model_id = "fake-embedding"
    cache_namespace = "fake-embedding:3"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [0.1, 0.2, 0.3]


def test_parse_history_entry_happened_at_from_prefix():
    assert (
        _parse_history_entry_happened_at("[2026-03-08 12:00] 用户确认信息")
        == "2026-03-08T12:00:00"
    )
    assert (
        _parse_history_entry_happened_at("[2026-03-08T12:01] 用户确认信息")
        == "2026-03-08T12:01:00"
    )
    assert (
        _parse_history_entry_happened_at("[2026-03-08] 用户确认信息")
        == "2026-03-08T00:00:00"
    )
    assert _parse_history_entry_happened_at("用户确认信息") is None


def test_upsert_consolidation_event_fills_missing_happened_at_on_duplicate(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")

    store.upsert_consolidation_event(
        source_ref="session@1",
        summary="[2026-03-08 12:00] same",
        embedding=[0.1, 0.2, 0.3],
    )
    store.upsert_consolidation_event(
        source_ref="session@2",
        summary="[2026-03-08 12:00] same",
        embedding=[0.1, 0.2, 0.3],
        happened_at="2026-03-08T12:00:00",
    )

    items = store.list_by_type("event")
    assert len(items) == 1
    assert items[0]["happened_at"] == "2026-03-08T12:00:00"


def test_save_from_consolidation_writes_happened_at(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    memorizer = Memorizer(store, cast(Any, _FakeEmbedder()))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="[2026-03-08 12:00] 用户确认信息",
            behavior_updates=[],
            source_ref="session@1-10",
            scope_channel="telegram",
            scope_chat_id="123",
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert len(items) == 1
    assert items[0]["happened_at"] == "2026-03-08T12:00:00"


def test_save_from_consolidation_skips_duplicate_source_ref(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    embedder = _FakeEmbedder()
    memorizer = Memorizer(store, cast(Any, embedder))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="[2026-03-08 12:00] second with different text",
            behavior_updates=[],
            source_ref="session@1-10",
            scope_channel="telegram",
            scope_chat_id="123",
        )
        await memorizer.save_from_consolidation(
            history_entry="[2026-03-08 12:00] first",
            behavior_updates=[],
            source_ref="session@1-10",
            scope_channel="telegram",
            scope_chat_id="123",
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert len(items) == 1
    assert items[0]["reinforcement"] == 1
    assert embedder.calls == 1


def test_embedding_cache_survives_memorizer_recreation(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    first_embedder = _FakeEmbedder()
    second_embedder = _FakeEmbedder()

    async def _run() -> None:
        await Memorizer(store, cast(Any, first_embedder)).save_item(
            summary="用户住在杭州",
            memory_type="profile",
            extra={"category": "personal_fact"},
            source_ref="source:1",
        )
        await Memorizer(store, cast(Any, second_embedder)).save_item(
            summary="用户住在杭州",
            memory_type="profile",
            extra={"category": "personal_fact"},
            source_ref="source:2",
        )

    asyncio.run(_run())

    assert first_embedder.calls == 1
    assert second_embedder.calls == 0


def test_semantic_duplicate_event_records_source_receipt_once(tmp_path):
    store = MemoryStore2(tmp_path / "memory2.db")
    embedder = _FakeEmbedder()
    memorizer = Memorizer(store, cast(Any, embedder))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="用户确认搬到杭州",
            behavior_updates=[],
            source_ref="session@first",
            scope_channel="cli",
            scope_chat_id="1",
        )
        await memorizer.save_from_consolidation(
            history_entry="用户说自己已经搬到杭州",
            behavior_updates=[],
            source_ref="session@semantic-duplicate",
            scope_channel="cli",
            scope_chat_id="1",
        )
        await memorizer.save_from_consolidation(
            history_entry="重放时文本可以不同",
            behavior_updates=[],
            source_ref="session@semantic-duplicate",
            scope_channel="cli",
            scope_chat_id="1",
        )

    asyncio.run(_run())

    items = store.list_by_type("event")
    assert len(items) == 1
    assert items[0]["reinforcement"] == 2
    assert embedder.calls == 2


def test_save_from_consolidation_exposes_storage_failure(tmp_path, monkeypatch):
    store = MemoryStore2(tmp_path / "memory2.db")

    def fail_upsert(**_kwargs):
        raise RuntimeError("memory database unavailable")

    monkeypatch.setattr(store, "upsert_consolidation_event", fail_upsert)
    memorizer = Memorizer(store, cast(Any, _FakeEmbedder()))

    async def _run() -> None:
        await memorizer.save_from_consolidation(
            history_entry="[2026-03-15 10:00] 存储失败测试",
            behavior_updates=[],
            source_ref="test@storage-failure",
            scope_channel="cli",
            scope_chat_id="1",
        )

    with pytest.raises(RuntimeError, match="memory database unavailable"):
        asyncio.run(_run())
