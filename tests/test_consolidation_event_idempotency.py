from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from core.memory.events import ConsolidationCommitted
from memory2.memorizer import Memorizer
from memory2.store import MemoryStore2
from plugins.default_memory.engine import DefaultMemoryEngine


class _CommitMarkerStore:
    def __init__(self) -> None:
        self.completed: dict[str, str] = {}
        self.extractions: dict[str, tuple[str, dict[str, object] | None]] = {}

    def begin_consolidation_job(self, *, source_ref: str, digest: str) -> None:
        existing = self.extractions.get(source_ref)
        if existing is not None and existing[0] != digest:
            raise ValueError("digest conflict")

    def load_consolidation_extraction(
        self, *, source_ref: str, digest: str
    ) -> tuple[bool, dict[str, object] | None]:
        existing = self.extractions.get(source_ref)
        if existing is None:
            return False, None
        if existing[0] != digest:
            raise ValueError("digest conflict")
        return True, existing[1]

    def save_consolidation_extraction(
        self,
        *,
        source_ref: str,
        digest: str,
        extraction: dict[str, object] | None,
    ) -> dict[str, object] | None:
        existing = self.extractions.get(source_ref)
        if existing is not None:
            if existing[0] != digest:
                raise ValueError("digest conflict")
            return existing[1]
        self.extractions[source_ref] = (digest, extraction)
        return extraction

    def has_completed_consolidation_commit(self, *, source_ref: str, digest: str) -> bool:
        existing = self.completed.get(source_ref)
        if existing is None:
            return False
        if existing != digest:
            raise ValueError("digest conflict")
        return True

    def mark_consolidation_commit_completed(self, *, source_ref: str, digest: str) -> None:
        existing = self.completed.get(source_ref)
        if existing is not None and existing != digest:
            raise ValueError("digest conflict")
        self.completed[source_ref] = digest


class _ProbeEngine(DefaultMemoryEngine):
    def __init__(self) -> None:
        self._v2_store = _CommitMarkerStore()
        self._memorizer = object()
        self.provider_calls = 0
        self.saved_entries = 0

    def _require_memorizer(self):
        return self._memorizer

    async def _save_from_consolidation(self, **kwargs):
        self.saved_entries += 1

    async def _extract_implicit_long_term(self, **kwargs):
        self.provider_calls += 1
        return None


class _FailCompletionOnceStore(MemoryStore2):
    def __init__(self, db_path) -> None:
        super().__init__(db_path, vec_dim=3)
        self.fail_completion_once = True

    def mark_consolidation_commit_completed(
        self, *, source_ref: str, digest: str
    ) -> None:
        if self.fail_completion_once:
            self.fail_completion_once = False
            raise RuntimeError("injected completion marker failure")
        super().mark_consolidation_commit_completed(
            source_ref=source_ref,
            digest=digest,
        )


class _CountingEmbedder:
    model_id = "test-embedding"
    cache_namespace = "test-embedding:3"

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, _text: str) -> list[float]:
        self.calls += 1
        await asyncio.sleep(0)
        return [0.1, 0.2, 0.3]


class _ImplicitProbeEngine(DefaultMemoryEngine):
    def __init__(self, store: MemoryStore2, embedder: _CountingEmbedder) -> None:
        self._v2_store = store
        self._memorizer = Memorizer(store, cast(Any, embedder))
        self.provider_calls = 0

    async def _extract_implicit_long_term(self, **_kwargs):
        self.provider_calls += 1
        await asyncio.sleep(0.01)
        return {
            "profile": [
                {
                    "summary": "用户住在杭州",
                    "category": "personal_fact",
                }
            ],
            "preference": [],
            "procedure": [],
        }


def test_consolidation_event_replay_skips_implicit_provider_after_success() -> None:
    engine = _ProbeEngine()
    event = ConsolidationCommitted(
        history_entry_payloads=[],
        source_ref="session:compaction:1",
        scope_channel="web",
        scope_chat_id="chat",
        conversation="conversation",
    )

    asyncio.run(engine._on_consolidation_committed(event))
    asyncio.run(engine._on_consolidation_committed(event))

    assert engine.provider_calls == 1
    assert engine.saved_entries == 0
    assert engine._v2_store.completed


def test_consolidation_replay_after_completion_failure_reuses_durable_stages(
    tmp_path,
) -> None:
    store = _FailCompletionOnceStore(tmp_path / "memory2.db")
    embedder = _CountingEmbedder()
    engine = _ImplicitProbeEngine(store, embedder)
    event = ConsolidationCommitted(
        history_entry_payloads=[],
        source_ref="session:compaction:failure",
        scope_channel="web",
        scope_chat_id="chat",
        conversation="USER: 我住在杭州",
    )

    with pytest.raises(RuntimeError, match="completion marker failure"):
        asyncio.run(engine._on_consolidation_committed(event))
    asyncio.run(engine._on_consolidation_committed(event))

    items = store.list_by_type("profile")
    assert engine.provider_calls == 1
    assert embedder.calls == 1
    assert len(items) == 1
    assert items[0]["reinforcement"] == 1


def test_concurrent_consolidation_delivery_runs_once(tmp_path) -> None:
    store = MemoryStore2(tmp_path / "memory2.db", vec_dim=3)
    embedder = _CountingEmbedder()
    engine = _ImplicitProbeEngine(store, embedder)
    event = ConsolidationCommitted(
        history_entry_payloads=[],
        source_ref="session:compaction:concurrent",
        scope_channel="web",
        scope_chat_id="chat",
        conversation="USER: 我住在杭州",
    )

    async def _run() -> None:
        await asyncio.gather(
            engine._on_consolidation_committed(event),
            engine._on_consolidation_committed(event),
        )

    asyncio.run(_run())

    items = store.list_by_type("profile")
    assert engine.provider_calls == 1
    assert embedder.calls == 1
    assert len(items) == 1
    assert items[0]["reinforcement"] == 1
