from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
import tomllib
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import httpx
import pytest

from agent.config_models import (
    Config,
    MemoryConfig,
    MemoryEmbeddingConfig,
)
from agent.migrations import memory_engine as migration
from agent.migrations import memory_engine_cli as migration_cli
from core.net.http import HttpRequester, RequestBudget, RetryPolicy


class _FakeEmbedder:
    def __init__(
        self,
        host: Config,
        *,
        fail_on_call: int | None = None,
        zero: bool = False,
    ) -> None:
        self.model_id = host.memory.embedding.model
        self.cache_namespace = str(migration._target_identity(host)["cacheNamespace"])
        self.fail_on_call = fail_on_call
        self.zero = zero
        self.calls: list[list[str]] = []
        self.closed = False

    @property
    def stats(self) -> dict[str, int | str]:
        return {
            "model": self.model_id,
            "request_count": len(self.calls),
            "text_count": sum(len(batch) for batch in self.calls),
        }

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail_on_call == len(self.calls):
            raise RuntimeError("injected provider failure")
        if self.zero:
            return [[0.0, 0.0] for _ in texts]
        return [_vector(text) for text in texts]

    async def aclose(self) -> None:
        self.closed = True


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    first = 1.0 + digest[0] / 255.0
    second = 1.0 + digest[1] / 255.0
    norm = (first * first + second * second) ** 0.5
    return [first / norm, second / norm]


def _host() -> Config:
    return Config(
        provider="openai",
        model="chat-model",
        api_key="chat-key",
        base_url="https://chat.example/v1",
        system_prompt="system",
        memory=MemoryConfig(
            enabled=True,
            engine="akasha",
            embedding=MemoryEmbeddingConfig(
                model_ref="embedding-main",
                model="embedding-model",
                api_key="embedding-key",
                base_url="https://embedding.example/v1",
                output_dimensionality=2,
            ),
        ),
    )


def _workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    turns: int = 1,
) -> tuple[Path, Path, Config]:
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            [
                "[runtime]",
                f'workspace = "{workspace}"',
                "",
                "[memory]",
                "enabled = true",
                'engine = "default"',
                "",
                "[memory.embedding]",
                'model_ref = "embedding-main"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    _create_sessions(workspace / "sessions.db", turns=turns)
    _create_memory2(memory / "memory2.db")
    (memory / "MEMORY.md").write_text("# stable profile\n", encoding="utf-8")
    (memory / "SELF.md").write_text("# self\n", encoding="utf-8")
    (memory / "PENDING.md").write_text("- pending fact\n", encoding="utf-8")
    host = _host()
    monkeypatch.setattr(migration.Config, "load", lambda *_args, **_kwargs: host)
    return workspace, config_path, host


def _create_sessions(path: Path, *, turns: int) -> None:
    started = datetime(2026, 9, 1, tzinfo=timezone.utc)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE sessions (
                key TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_consolidated INTEGER NOT NULL DEFAULT 0,
                metadata TEXT
            );
            CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                seq INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_chain TEXT,
                extra TEXT,
                ts TEXT NOT NULL,
                UNIQUE(session_key, seq)
            );
            """)
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, 0, ?)",
            ("web:test", started.isoformat(), started.isoformat(), "{}"),
        )
        for turn in range(turns):
            user_time = started + timedelta(minutes=turn * 2)
            assistant_time = user_time + timedelta(seconds=10)
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, 'user', ?, NULL, '{}', ?)",
                (
                    f"u-{turn}",
                    "web:test",
                    turn * 2,
                    f"question {turn}",
                    user_time.isoformat(),
                ),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, 'assistant', ?, NULL, '{}', ?)",
                (
                    f"a-{turn}",
                    "web:test",
                    turn * 2 + 1,
                    f"answer {turn}",
                    assistant_time.isoformat(),
                ),
            )


def _create_memory2(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("""
            CREATE TABLE memory_items (
                id TEXT PRIMARY KEY,
                memory_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                reinforcement INTEGER NOT NULL,
                emotional_weight INTEGER NOT NULL,
                extra_json TEXT,
                source_ref TEXT,
                happened_at TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        connection.execute(
            "INSERT INTO memory_items VALUES (?, ?, ?, 1, 0, '{}', ?, ?, 'active', ?, ?)",
            (
                "memory-1",
                "preference",
                "用户偏好简洁回答",
                "session:web:test:0",
                "2026-09-01T00:00:00+00:00",
                "2026-09-01T00:00:00+00:00",
                "2026-09-01T00:00:00+00:00",
            ),
        )


def _source_rows(
    path: Path,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with closing(sqlite3.connect(path)) as connection:
        sessions = connection.execute(
            "SELECT key, created_at, updated_at, last_consolidated, metadata "
            "FROM sessions ORDER BY key"
        ).fetchall()
        messages = connection.execute(
            "SELECT id, session_key, seq, role, content, tool_chain, extra, ts "
            "FROM messages ORDER BY session_key, seq"
        ).fetchall()
    return sessions, messages


def _protected_bytes(workspace: Path) -> dict[str, bytes]:
    return {
        relative: (workspace / relative).read_bytes()
        for relative in (
            "memory/memory2.db",
            "memory/MEMORY.md",
            "memory/SELF.md",
            "memory/PENDING.md",
        )
    }


def test_prepare_cli_owns_http_resources_for_real_embedder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workspace, config_path, _ = _workspace(tmp_path, monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        inputs = cast(list[str], payload["input"])
        return httpx.Response(
            200,
            request=request,
            json={
                "data": [
                    {"index": index, "embedding": _vector(value)}
                    for index, value in enumerate(inputs)
                ]
            },
        )

    requester = HttpRequester(
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_policy=RetryPolicy(max_attempts=1),
        default_timeout_s=1.0,
        default_budget=RequestBudget(total_timeout_s=2.0),
    )

    class Resources:
        external_default = requester
        closed = False

        async def aclose(self) -> None:
            await requester.client.aclose()
            self.closed = True

    resources = Resources()
    monkeypatch.setattr(migration_cli, "SharedHttpResources", lambda: resources)

    result = migration_cli.run_memory_migration_cli(
        [
            "prepare",
            "--config",
            str(config_path),
            "--workspace",
            str(workspace),
            "--operation-id",
            "cli-http-lifecycle",
            "--confirm",
            migration.SEND_HISTORY_CONFIRMATION,
        ]
    )

    assert result == 0
    assert resources.closed is True
    output = capsys.readouterr().out
    payload = json.loads(output[output.index("{\n") :])
    assert payload["phase"] == "prepared"
    assert payload["providerStats"]["text_count"] == 2


@pytest.mark.asyncio
async def test_assess_is_read_only_when_embedding_table_does_not_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, _ = _workspace(tmp_path, monkeypatch)
    sessions_path = workspace / "sessions.db"
    source_before = sessions_path.read_bytes()
    protected_before = _protected_bytes(workspace)

    result = migration.assess_memory_migration(
        config_path=config_path,
        workspace=workspace,
    )

    assert result["status"] == "assessed"
    source = cast(dict[str, object], result["source"])
    assert source["requiredMessages"] == 2
    assert source["embeddingIssues"] == 2
    assert sessions_path.read_bytes() == source_before
    assert _protected_bytes(workspace) == protected_before
    with closing(sqlite3.connect(sessions_path)) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_prepare_candidate_config_is_loadable_by_real_config_loader(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    memory = workspace / "memory"
    memory.mkdir(parents=True)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[llm]
main = "main"

[llm.runtimes.main]
provider = "openai"
model = "chat-model"
api_key = "chat-key"
base_url = "https://chat.example/v1"

[memory]
enabled = true
engine = "default"

[memory.embedding]
model = "embedding-model"
api_key = "embedding-key"
base_url = "https://embedding.example/v1"
output_dimensionality = 2
""".strip() + "\n",
        encoding="utf-8",
    )
    _create_sessions(workspace / "sessions.db", turns=1)
    _create_memory2(memory / "memory2.db")
    (memory / "MEMORY.md").write_text("# stable profile\n", encoding="utf-8")
    (memory / "SELF.md").write_text("# self\n", encoding="utf-8")
    (memory / "PENDING.md").write_text("", encoding="utf-8")
    host = Config.load(config_path, workspace=workspace)

    prepared = await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="real-config-loader",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )

    candidate = (
        workspace
        / "backups/memory-engine-migrations/real-config-loader/config.candidate.toml"
    )
    assert prepared["phase"] == "prepared"
    assert candidate.is_file()
    assert Config.load(candidate, workspace=workspace).memory.engine == "akasha"


@pytest.mark.asyncio
async def test_prepare_apply_verify_and_revert_preserve_all_authoritative_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    sessions_path = workspace / "sessions.db"
    source_before = _source_rows(sessions_path)
    protected_before = _protected_bytes(workspace)
    original_config = config_path.read_bytes()
    client = _FakeEmbedder(host)

    prepared = await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="test-switch",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=client,
    )

    assert prepared["phase"] == "prepared"
    assert _source_rows(sessions_path) == source_before
    assert _protected_bytes(workspace) == protected_before
    with closing(sqlite3.connect(sessions_path)) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )
    review = json.loads(
        (
            workspace
            / "backups/memory-engine-migrations/test-switch/legacy-memory-review.json"
        ).read_text(encoding="utf-8")
    )
    assert review["items"][0]["summary"] == "用户偏好简洁回答"
    assert "embedding" not in review["items"][0]

    applied = migration.apply_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="test-switch",
        confirmation=migration.APPLY_CONFIRMATION,
    )

    assert applied["phase"] == "applied"
    assert _source_rows(sessions_path) == source_before
    assert _protected_bytes(workspace) == protected_before
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["memory"]["enabled"] is True
    assert parsed["memory"]["engine"] == "akasha"
    assert (workspace / "memory/akasha-v2-index.db").is_file()
    assert (workspace / "memory/akasha.db").is_file()
    with closing(sqlite3.connect(sessions_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE model='embedding-model'"
        ).fetchone() == (2,)

    verified = migration.verify_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="test-switch",
    )
    assert verified["status"] == "verified"
    assert verified["indexedTurns"] == 1

    # A later chat remains authoritative and must survive reverting the engine.
    with closing(sqlite3.connect(sessions_path)) as connection, connection:
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, 'user', ?, NULL, '{}', ?)",
            (
                "later-user",
                "web:test",
                2,
                "later unmatched message",
                "2026-09-02T00:00:00+00:00",
            ),
        )
    source_before_revert = _source_rows(sessions_path)
    reverted = migration.revert_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="test-switch",
        confirmation=migration.REVERT_CONFIRMATION,
    )

    assert reverted["phase"] == "reverted"
    assert config_path.read_bytes() == original_config
    assert _source_rows(sessions_path) == source_before_revert
    assert _protected_bytes(workspace) == protected_before
    assert not (workspace / "memory/akasha-v2-index.db").exists()
    assert not (workspace / "memory/akasha.db").exists()
    with closing(sqlite3.connect(sessions_path)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE model='embedding-model'"
        ).fetchone() == (2,)


@pytest.mark.asyncio
async def test_verify_accepts_runtime_noop_sparse_index_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="live-index-rewrite",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    migration.apply_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="live-index-rewrite",
        confirmation=migration.APPLY_CONFIRMATION,
    )
    index = workspace / "memory/akasha-v2-index.db"
    before = migration._sha256_file(index)

    # OnlineMemoryRuntime does this on startup.  The source has no new turns,
    # but SQLite still rewrites compact state and changes the physical file.
    result = migration.build_sparse_index(
        workspace / "sessions.db",
        index,
        migration.BuildConfig(
            embedding_model=host.memory.embedding.model,
            embedding_dimension=host.memory.embedding.output_dimensionality,
        ),
    )

    assert result.indexed_turns == 0
    assert result.skipped_existing_turns == 1
    assert migration._sha256_file(index) != before
    verified = migration.verify_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="live-index-rewrite",
    )
    assert verified["status"] == "verified"
    assert verified["indexedTurns"] == 1


@pytest.mark.asyncio
async def test_prepare_resumes_after_provider_failure_without_reembedding_patch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch, turns=6)
    first = _FakeEmbedder(host, fail_on_call=2)

    with pytest.raises(RuntimeError, match="injected provider failure"):
        await migration.prepare_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="resume",
            confirmation=migration.SEND_HISTORY_CONFIRMATION,
            embedder=first,
        )
    assert sum(len(batch) for batch in first.calls[:-1]) == 10

    resumed = _FakeEmbedder(host)
    result = await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="resume",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=resumed,
    )

    assert result["phase"] == "prepared"
    assert sum(len(batch) for batch in resumed.calls) == 2
    with closing(
        sqlite3.connect(
            workspace / "backups/memory-engine-migrations/resume/embedding-patch.db"
        )
    ) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM message_embeddings"
        ).fetchone() == (12,)


@pytest.mark.asyncio
async def test_prepare_reuses_valid_formal_embeddings_and_patches_only_gaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(
        tmp_path,
        monkeypatch,
        turns=2,
    )
    sessions = workspace / "sessions.db"
    migration._ensure_embedding_schema(sessions)
    existing = migration.RequiredEmbeddingMessage(
        message_id="u-0",
        session_key="web:test",
        seq=0,
        role="user",
        content="question 0",
    )
    migration._upsert_embeddings(
        sessions,
        [existing],
        [_vector(existing.content)],
        model=host.memory.embedding.model,
    )
    client = _FakeEmbedder(host)

    prepared = await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="partial-existing",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=client,
    )

    assert prepared["phase"] == "prepared"
    assert sum(len(batch) for batch in client.calls) == 3
    migration.apply_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="partial-existing",
        confirmation=migration.APPLY_CONFIRMATION,
    )
    with closing(sqlite3.connect(sessions)) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE model = ?",
            (host.memory.embedding.model,),
        ).fetchone() == (4,)


@pytest.mark.asyncio
async def test_prepare_resume_rejects_candidate_config_tamper_before_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch, turns=6)
    with pytest.raises(RuntimeError, match="injected provider failure"):
        await migration.prepare_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="resume-tamper",
            confirmation=migration.SEND_HISTORY_CONFIRMATION,
            embedder=_FakeEmbedder(host, fail_on_call=2),
        )
    candidate = (
        workspace
        / "backups/memory-engine-migrations/resume-tamper/config.candidate.toml"
    )
    candidate.write_text(
        candidate.read_text(encoding="utf-8") + "\n# drift\n",
        encoding="utf-8",
    )
    resumed = _FakeEmbedder(host)

    with pytest.raises(migration.MemoryEngineMigrationError, match="候选配置已漂移"):
        await migration.prepare_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="resume-tamper",
            confirmation=migration.SEND_HISTORY_CONFIRMATION,
            embedder=resumed,
        )

    assert resumed.calls == []


@pytest.mark.asyncio
async def test_custom_classic_memory_database_is_exported_and_backed_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    plugin_data = workspace / "plugin-data/default_memory-builtin"
    plugin_data.mkdir(parents=True)
    (plugin_data / "config.local.toml").write_text(
        'db_path = "state/custom-memory.db"\n',
        encoding="utf-8",
    )
    custom = workspace / "state/custom-memory.db"
    custom.parent.mkdir(parents=True)
    _create_memory2(custom)
    with closing(sqlite3.connect(custom)) as connection, connection:
        connection.execute(
            "UPDATE memory_items SET summary = 'custom canonical memory'"
        )

    prepared = await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="custom-memory2",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    operation = workspace / "backups/memory-engine-migrations/custom-memory2"
    review = json.loads(
        (operation / "legacy-memory-review.json").read_text(encoding="utf-8")
    )
    assert review["items"][0]["summary"] == "custom canonical memory"

    applied = migration.apply_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="custom-memory2",
        confirmation=migration.APPLY_CONFIRMATION,
    )
    rollback = cast(dict[str, object], applied["rollback"])
    protected = cast(dict[str, object], rollback["protected"])
    assert "state/custom-memory.db" in protected
    assert (operation / "rollback/protected/state/custom-memory.db").is_file()


@pytest.mark.asyncio
async def test_apply_rejects_source_drift_before_formal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="drift",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    with closing(sqlite3.connect(workspace / "sessions.db")) as connection, connection:
        connection.execute(
            "INSERT INTO messages VALUES ('later', 'web:test', 2, 'user', "
            "'new', NULL, '{}', '2026-09-03T00:00:00+00:00')"
        )

    with pytest.raises(migration.MemoryEngineMigrationError, match="新消息"):
        migration.apply_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="drift",
            confirmation=migration.APPLY_CONFIRMATION,
        )

    with closing(sqlite3.connect(workspace / "sessions.db")) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )
    assert (
        tomllib.loads(config_path.read_text(encoding="utf-8"))["memory"]["engine"]
        == "default"
    )


@pytest.mark.asyncio
async def test_apply_rejects_running_workspace_before_formal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="locked",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    lock_path = workspace / ".instance.lock"
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(migration.MemoryEngineMigrationError, match="仍在运行"):
            migration.apply_memory_migration(
                config_path=config_path,
                workspace=workspace,
                operation_id="locked",
                confirmation=migration.APPLY_CONFIRMATION,
            )
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    with closing(sqlite3.connect(workspace / "sessions.db")) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_apply_rejects_symlinked_runtime_lock_before_formal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="unsafe-lock",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    outside = tmp_path / "outside-lock"
    outside.write_text("do not open through workspace", encoding="utf-8")
    (workspace / ".instance.lock").symlink_to(outside)

    with pytest.raises(migration.MemoryEngineMigrationError, match="安全的普通文件"):
        migration.apply_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="unsafe-lock",
            confirmation=migration.APPLY_CONFIRMATION,
        )

    with closing(sqlite3.connect(workspace / "sessions.db")) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_apply_failure_restores_config_and_sidecars_then_retry_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    original_config = config_path.read_bytes()
    source_before = _source_rows(workspace / "sessions.db")
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="fault",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )

    def fail(point: str) -> None:
        if point == "after_sidecar_publish":
            raise RuntimeError("injected publish failure")

    with pytest.raises(RuntimeError, match="injected publish failure"):
        migration.apply_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="fault",
            confirmation=migration.APPLY_CONFIRMATION,
            fault_hook=fail,
        )

    assert config_path.read_bytes() == original_config
    assert not (workspace / "memory/akasha-v2-index.db").exists()
    assert not (workspace / "memory/akasha.db").exists()
    assert _source_rows(workspace / "sessions.db") == source_before

    result = migration.apply_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="fault",
        confirmation=migration.APPLY_CONFIRMATION,
    )
    assert result["phase"] == "applied"


@pytest.mark.asyncio
async def test_apply_failure_after_config_publish_restores_classic_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    original_config = config_path.read_bytes()
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="config-fault",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )

    def fail(point: str) -> None:
        if point == "after_config_publish":
            raise RuntimeError("injected config publish failure")

    with pytest.raises(RuntimeError, match="injected config publish failure"):
        migration.apply_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="config-fault",
            confirmation=migration.APPLY_CONFIRMATION,
            fault_hook=fail,
        )

    assert config_path.read_bytes() == original_config
    assert not (workspace / "memory/akasha-v2-index.db").exists()
    assert not (workspace / "memory/akasha.db").exists()
    with closing(sqlite3.connect(workspace / "sessions.db")) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE model='embedding-model'"
        ).fetchone() == (2,)


@pytest.mark.asyncio
async def test_interrupted_revert_is_resumable_from_original_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    original_config = config_path.read_bytes()
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="revert-fault",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    migration.apply_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="revert-fault",
        confirmation=migration.APPLY_CONFIRMATION,
    )

    def fail(point: str) -> None:
        if point == "after_revert_config":
            raise RuntimeError("injected revert interruption")

    with pytest.raises(RuntimeError, match="injected revert interruption"):
        migration.revert_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="revert-fault",
            confirmation=migration.REVERT_CONFIRMATION,
            fault_hook=fail,
        )
    assert config_path.read_bytes() == original_config
    assert (workspace / "memory/akasha-v2-index.db").is_file()

    reverted = migration.revert_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="revert-fault",
        confirmation=migration.REVERT_CONFIRMATION,
    )
    assert reverted["phase"] == "reverted"
    assert not (workspace / "memory/akasha-v2-index.db").exists()
    assert not (workspace / "memory/akasha.db").exists()


@pytest.mark.asyncio
async def test_zero_vectors_fail_prepare_without_touching_formal_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    source_before = (workspace / "sessions.db").read_bytes()

    with pytest.raises(migration.MemoryEngineMigrationError, match="零向量"):
        await migration.prepare_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="zero",
            confirmation=migration.SEND_HISTORY_CONFIRMATION,
            embedder=_FakeEmbedder(host, zero=True),
        )

    assert (workspace / "sessions.db").read_bytes() == source_before
    with closing(sqlite3.connect(workspace / "sessions.db")) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_revert_restores_preexisting_sidecars_from_verified_backups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    index = workspace / "memory/akasha-v2-index.db"
    graph = workspace / "memory/akasha.db"
    for path, marker in ((index, "old-index"), (graph, "old-graph")):
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE old_state (marker TEXT PRIMARY KEY)")
            connection.execute("INSERT INTO old_state VALUES (?)", (marker,))

    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="existing-sidecars",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    migration.apply_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="existing-sidecars",
        confirmation=migration.APPLY_CONFIRMATION,
    )
    migration.revert_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="existing-sidecars",
        confirmation=migration.REVERT_CONFIRMATION,
    )

    for path, marker in ((index, "old-index"), (graph, "old-graph")):
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("SELECT marker FROM old_state").fetchone() == (
                marker,
            )


@pytest.mark.asyncio
async def test_prepared_artifact_tamper_fails_before_formal_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="tamper",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    candidate = (
        workspace
        / "backups/memory-engine-migrations/tamper/akasha-v2-index.candidate.db"
    )
    candidate.write_bytes(candidate.read_bytes() + b"tampered")

    with pytest.raises(migration.MemoryEngineMigrationError, match="artifact"):
        migration.apply_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="tamper",
            confirmation=migration.APPLY_CONFIRMATION,
        )

    with closing(sqlite3.connect(workspace / "sessions.db")) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )


@pytest.mark.asyncio
async def test_apply_full_source_digest_detects_non_builder_column_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace, config_path, host = _workspace(tmp_path, monkeypatch)
    await migration.prepare_memory_migration(
        config_path=config_path,
        workspace=workspace,
        operation_id="full-row-drift",
        confirmation=migration.SEND_HISTORY_CONFIRMATION,
        embedder=_FakeEmbedder(host),
    )
    with closing(sqlite3.connect(workspace / "sessions.db")) as connection, connection:
        connection.execute(
            "UPDATE messages SET tool_chain = ? WHERE id = ?",
            ('[{"tool":"changed"}]', "u-0"),
        )

    with pytest.raises(migration.MemoryEngineMigrationError, match="变化"):
        migration.apply_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id="full-row-drift",
            confirmation=migration.APPLY_CONFIRMATION,
        )

    with closing(sqlite3.connect(workspace / "sessions.db")) as connection:
        assert (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_embeddings'"
            ).fetchone()
            is None
        )
