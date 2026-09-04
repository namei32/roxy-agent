"""Prepare, commit, verify, and revert a first Akasha adoption.

The migration deliberately separates provider-facing work from formal workspace
mutation.  ``prepare`` operates on verified SQLite snapshots; ``apply`` is a
short, offline commit guarded by both runtime ownership locks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import struct
import tempfile
import tomllib
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO, cast
from uuid import uuid4

import tomlkit

from agent.config import Config
from agent.plugins.manifest import builtin_plugin_data_dir
from memory2.embedder import Embedder
from plugins.akasha.application.rebuild import rebuild_memory
from plugins.akasha.application.runtime import OnlineMemoryRuntime
from plugins.akasha.config import (
    AkashaConfig,
    load_akasha_config,
    resolve_workspace_path,
)
from plugins.akasha.infrastructure.loader import load_turns
from plugins.akasha.infrastructure.persistence import load_memory_state
from plugins.akasha.infrastructure.sparse_index import (
    BuildConfig,
    EmbeddingAudit,
    RequiredEmbeddingMessage,
    audit_source_embeddings,
    build_sparse_index,
    list_required_embedding_messages,
)
from plugins.akasha.infrastructure.sparse_index.schema import INDEX_VERSION
from plugins.default_memory.config import (
    load_default_memory_config,
    resolve_memory_db_path,
)

MIGRATION_SCHEMA_VERSION = 1
MIGRATION_ROOT = Path("backups/memory-engine-migrations")
SEND_HISTORY_CONFIRMATION = "SEND-HISTORY-TO-EMBEDDING-PROVIDER"
APPLY_CONFIRMATION = "APPLY-AKASHA"
REVERT_CONFIRMATION = "REVERT-AKASHA"
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_EMBEDDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_embeddings (
    message_id   TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model        TEXT NOT NULL,
    embedding    BLOB NOT NULL,
    dim          INTEGER NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (message_id, model)
);
CREATE INDEX IF NOT EXISTS ix_message_embeddings_hash
    ON message_embeddings (content_hash, model);
CREATE TABLE IF NOT EXISTS message_embedding_migrations (
    source_id      TEXT PRIMARY KEY,
    completed_at   TEXT NOT NULL,
    imported_count INTEGER NOT NULL
);
"""
_MARKDOWN_MEMORY_PATHS = (
    Path("memory/MEMORY.md"),
    Path("memory/SELF.md"),
    Path("memory/PENDING.md"),
)
_PROVIDER_BATCH_DELAY_SECONDS = 0.3


class MemoryEngineMigrationError(RuntimeError):
    """Fail loudly without presenting a partial switch as successful."""


class EmbeddingClient(Protocol):
    """Small provider boundary used by resumable prepare and deterministic tests."""

    @property
    def model_id(self) -> str: ...

    @property
    def cache_namespace(self) -> str: ...

    @property
    def stats(self) -> Mapping[str, int | str]: ...

    async def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    async def aclose(self) -> None: ...


FaultHook = Callable[[str], None]


def new_operation_id() -> str:
    """Create a sortable, path-safe operation identity."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"akasha-{stamp}-{uuid4().hex[:12]}"


def assess_memory_migration(
    *,
    config_path: Path,
    workspace: Path,
    embedding_model_id: str = "",
) -> dict[str, object]:
    """Inspect one candidate switch without mutating the formal workspace."""

    config_path, workspace = _resolve_inputs(config_path, workspace)
    original = _read_required_file(config_path, "主配置")
    candidate = _candidate_config(original, embedding_model_id)
    with tempfile.TemporaryDirectory(prefix="roxy-akasha-assess-") as raw_temp:
        temporary = Path(raw_temp)
        candidate_path = temporary / "config.toml"
        candidate_path.write_bytes(candidate)
        host = Config.load(candidate_path, workspace=workspace)
        target = _target_identity(host)
        sessions = workspace / "sessions.db"
        if not sessions.is_file():
            raise MemoryEngineMigrationError(f"缺少权威会话库: {sessions}")
        snapshot = temporary / "sessions.db"
        _backup_sqlite(sessions, snapshot)
        _ensure_embedding_schema(snapshot)
        build_config = BuildConfig(
            embedding_model=host.memory.embedding.model,
            embedding_dimension=host.memory.embedding.output_dimensionality,
        )
        audit = audit_source_embeddings(snapshot, build_config)
        required = list_required_embedding_messages(snapshot)
    current = _memory_binding(original)
    plugin, plugin_identity = _load_plugin_identity(workspace)
    index_path, memory_path = _sidecar_paths(workspace, plugin)
    return {
        "status": "assessed",
        "workspace": str(workspace),
        "configPath": str(config_path),
        "current": current,
        "target": target,
        "source": {
            "digest": _session_source_digest(workspace / "sessions.db"),
            "requiredMessages": len(required),
            "eligibleTurns": audit.eligible_turns,
            "validEmbeddings": audit.valid_messages,
            "embeddingIssues": len(audit.issues),
            "issueReasons": _issue_counts(audit),
        },
        "sidecars": {
            "index": str(index_path),
            "memory": str(memory_path),
            "pluginConfigSha256": plugin_identity["effectiveSha256"],
            "indexVersion": INDEX_VERSION,
        },
        "protected": _protected_state(workspace),
        "next": "prepare",
    }


async def prepare_memory_migration(
    *,
    config_path: Path,
    workspace: Path,
    operation_id: str,
    embedding_model_id: str = "",
    confirmation: str,
    embedder: EmbeddingClient | None = None,
    fault_hook: FaultHook | None = None,
) -> dict[str, object]:
    """Build a complete migration candidate without mutating formal databases."""

    if confirmation != SEND_HISTORY_CONFIRMATION:
        raise MemoryEngineMigrationError(
            "prepare 会把合格历史消息发送给已配置的向量服务；"
            f"请显式提供确认值 {SEND_HISTORY_CONFIRMATION}"
        )
    config_path, workspace = _resolve_inputs(config_path, workspace)
    operation = _operation_path(workspace, operation_id)
    manifest_path = operation / "manifest.json"
    if manifest_path.exists():
        manifest = _load_manifest(operation, operation_id, workspace, config_path)
        if manifest["phase"] == "prepared":
            _verify_prepared_artifacts(operation, manifest)
            return manifest
        if manifest["phase"] not in {"preparing", "prepare_failed"}:
            raise MemoryEngineMigrationError(
                f"operation 当前阶段不能 prepare: {manifest['phase']}"
            )
        candidate_bytes = _read_required_file(
            operation / "config.candidate", "候选配置"
        )
        _verify_prepare_resume_state(operation, workspace, manifest)
        if embedding_model_id and embedding_model_id != str(
            cast(dict[str, object], manifest["target"]).get("modelRef") or ""
        ):
            raise MemoryEngineMigrationError("续跑 prepare 时不能改变 embedding model")
    else:
        _create_operation_directory(operation)
        original = _read_required_file(config_path, "主配置")
        candidate_bytes = _candidate_config(original, embedding_model_id)
        _atomic_write_bytes(operation / "config.before", original, 0o600)
        _atomic_write_bytes(operation / "config.candidate", candidate_bytes, 0o600)
        host = Config.load(operation / "config.candidate", workspace=workspace)
        plugin, plugin_identity = _load_plugin_identity(workspace)
        index_path, memory_path = _sidecar_paths(workspace, plugin)
        snapshot = operation / "source-sessions.db"
        _backup_sqlite(workspace / "sessions.db", snapshot)
        _ensure_embedding_schema(snapshot)
        required = list_required_embedding_messages(snapshot)
        manifest = {
            "schemaVersion": MIGRATION_SCHEMA_VERSION,
            "operationId": operation_id,
            "phase": "preparing",
            "createdAt": _utc_now(),
            "workspace": str(workspace),
            "configPath": str(config_path),
            "originalConfigSha256": _sha256_bytes(original),
            "candidateConfigSha256": _sha256_bytes(candidate_bytes),
            "source": {
                "messageDigest": _session_source_digest(snapshot),
                "snapshotSha256": _sha256_file(snapshot),
                "requiredMessages": len(required),
            },
            "target": {
                **_target_identity(host),
                "modelRef": host.memory.embedding.model_ref,
            },
            "plugin": plugin_identity,
            "paths": {
                "index": str(index_path),
                "memory": str(memory_path),
            },
            "progress": {
                "embeddedMessages": 0,
                "successfulProviderBatches": 0,
            },
        }
        _atomic_write_json(manifest_path, manifest)

    host = Config.load(operation / "config.candidate", workspace=workspace)
    target = cast(dict[str, object], manifest["target"])
    if _target_identity(host)["cacheNamespace"] != target["cacheNamespace"]:
        raise MemoryEngineMigrationError("候选配置解析出的 embedding identity 已漂移")
    snapshot = operation / "source-sessions.db"
    patch_path = operation / "embedding-patch.db"
    _ensure_embedding_schema(snapshot)
    _ensure_embedding_schema(patch_path)
    required = list_required_embedding_messages(snapshot)
    required_by_id = {item.message_id: item for item in required}
    _reconcile_patch(snapshot, patch_path, required_by_id, host.memory.embedding.model)

    owned_embedder = embedder is None
    client = embedder or _build_embedder(host)
    if client.model_id != host.memory.embedding.model:
        raise MemoryEngineMigrationError(
            "embedding client model 与候选配置不一致: "
            f"{client.model_id} != {host.memory.embedding.model}"
        )
    if client.cache_namespace != target["cacheNamespace"]:
        raise MemoryEngineMigrationError(
            "embedding client cache namespace 与收据不一致"
        )
    try:
        configured_dimension = host.memory.embedding.output_dimensionality
        resolved_dimension = _resolved_patch_dimension(
            snapshot,
            required,
            host.memory.embedding.model,
            configured_dimension,
        )
        while True:
            audit = audit_source_embeddings(
                snapshot,
                BuildConfig(
                    embedding_model=host.memory.embedding.model,
                    embedding_dimension=configured_dimension or resolved_dimension,
                ),
            )
            if audit.complete:
                if resolved_dimension is None:
                    resolved_dimension = audit.dimension
                break
            batch_issues = audit.issues[: Embedder.MAX_BATCH]
            batch = [required_by_id[issue.message_id] for issue in batch_issues]
            progress = cast(dict[str, object], manifest["progress"])
            successful_batches = int(
                cast(
                    int | str,
                    progress.get("successfulProviderBatches", 0),
                )
            )
            if successful_batches > 0:
                await asyncio.sleep(_PROVIDER_BATCH_DELAY_SECONDS)
            vectors = await client.embed_batch([item.content for item in batch])
            if len(vectors) != len(batch):
                raise MemoryEngineMigrationError(
                    "embedding provider 返回数量与请求不一致: "
                    f"expected={len(batch)} actual={len(vectors)}"
                )
            resolved_dimension = _validate_embedding_batch(
                batch,
                vectors,
                expected_dimension=configured_dimension or resolved_dimension,
            )
            # Patch first: a crash can be resumed by replaying it into the snapshot.
            _upsert_embeddings(
                patch_path,
                batch,
                vectors,
                model=host.memory.embedding.model,
            )
            _upsert_embeddings(
                snapshot,
                batch,
                vectors,
                model=host.memory.embedding.model,
            )
            progress["embeddedMessages"] = _patch_row_count(
                patch_path, host.memory.embedding.model
            )
            progress["successfulProviderBatches"] = successful_batches + 1
            manifest["phase"] = "preparing"
            manifest["updatedAt"] = _utc_now()
            _atomic_write_json(manifest_path, manifest)
            _fault(fault_hook, "after_embedding_batch")

        strict_dimension = configured_dimension or resolved_dimension
        strict_config = BuildConfig(
            embedding_model=host.memory.embedding.model,
            embedding_dimension=strict_dimension,
        )
        final_audit = audit_source_embeddings(snapshot, strict_config)
        if not final_audit.complete:
            raise MemoryEngineMigrationError(_format_audit_failure(final_audit))
        if required and strict_dimension is None:
            raise MemoryEngineMigrationError("无法确定历史向量维度")

        candidate_index = operation / "akasha-v2-index.candidate.db"
        candidate_memory = operation / "akasha.candidate.db"
        candidate_index.unlink(missing_ok=True)
        candidate_memory.unlink(missing_ok=True)
        candidate_memory.with_suffix(candidate_memory.suffix + ".tmp").unlink(
            missing_ok=True
        )
        build_result = build_sparse_index(snapshot, candidate_index, strict_config)
        turns = load_turns(candidate_index)
        plugin, current_plugin_identity = _load_plugin_identity(workspace)
        if (
            current_plugin_identity["effectiveSha256"]
            != cast(dict[str, object], manifest["plugin"])["effectiveSha256"]
        ):
            raise MemoryEngineMigrationError(
                "Akasha plugin config 在 prepare 期间已漂移"
            )
        graph_summary: dict[str, object] | None = None
        if turns:
            summary = rebuild_memory(
                candidate_index,
                candidate_memory,
                config=plugin.memory_config(),
                target_sequences=(),
            )
            graph_summary = {
                "turns": summary.turns,
                "hubs": summary.hubs,
                "relations": summary.relations,
                "logicalStateSha256": summary.logical_state_sha256,
            }
            _verify_akasha_pair(candidate_index, candidate_memory, plugin)
        else:
            _verify_akasha_pair(candidate_index, None, plugin)
        _verify_online_runtime_shadow(
            operation=operation,
            sessions=snapshot,
            index=candidate_index,
            memory=candidate_memory if turns else None,
            plugin=plugin,
            embedding_model=host.memory.embedding.model,
            embedding_dimension=strict_dimension,
        )
        _fault(fault_hook, "after_candidate_build")

        legacy = _export_legacy_memory(workspace, operation)
        artifacts: dict[str, object] = {
            "snapshot": _artifact_record(operation, snapshot),
            "embeddingPatch": _artifact_record(operation, patch_path),
            "candidateIndex": _artifact_record(operation, candidate_index),
            "candidateMemory": (
                _artifact_record(operation, candidate_memory) if turns else None
            ),
            "candidateConfig": _artifact_record(
                operation, operation / "config.candidate"
            ),
            "configBefore": _artifact_record(operation, operation / "config.before"),
            "legacyMemoryReview": _artifact_record(
                operation, operation / "legacy-memory-review.json"
            ),
        }
        if (operation / "legacy-memory2-snapshot.db").exists():
            artifacts["legacyMemorySnapshot"] = _artifact_record(
                operation, operation / "legacy-memory2-snapshot.db"
            )
        source = cast(dict[str, object], manifest["source"])
        source["snapshotSha256"] = _sha256_file(snapshot)
        source["frozenInputDigest"] = _frozen_embedding_digest(
            snapshot,
            required,
            host.memory.embedding.model,
            strict_dimension,
        )
        source["resolvedDimension"] = strict_dimension
        source["audit"] = _audit_payload(final_audit)
        target["resolvedDimension"] = strict_dimension
        manifest["build"] = {
            "discoveredTurns": build_result.discovered_turns,
            "indexedTurns": build_result.indexed_turns,
            "turnsMissingEmbeddings": build_result.turns_missing_embeddings,
            "graph": graph_summary,
        }
        manifest["legacyMemory"] = legacy
        manifest["artifacts"] = artifacts
        manifest["providerStats"] = dict(client.stats)
        manifest["phase"] = "prepared"
        manifest["preparedAt"] = _utc_now()
        manifest.pop("lastError", None)
        _atomic_write_json(manifest_path, manifest)
        _verify_prepared_artifacts(operation, manifest)
        return manifest
    except BaseException as exc:
        manifest["phase"] = "prepare_failed"
        manifest["lastError"] = _safe_error(exc)
        manifest["updatedAt"] = _utc_now()
        _atomic_write_json(manifest_path, manifest)
        raise
    finally:
        if owned_embedder:
            await client.aclose()


def apply_memory_migration(
    *,
    config_path: Path,
    workspace: Path,
    operation_id: str,
    confirmation: str,
    fault_hook: FaultHook | None = None,
) -> dict[str, object]:
    """Commit one prepared candidate while the formal runtime is offline."""

    if confirmation != APPLY_CONFIRMATION:
        raise MemoryEngineMigrationError(
            f"apply 会修改正式向量输入、sidecar 和配置；请显式提供 {APPLY_CONFIRMATION}"
        )
    config_path, workspace = _resolve_inputs(config_path, workspace)
    operation = _operation_path(workspace, operation_id)
    manifest = _load_manifest(operation, operation_id, workspace, config_path)
    if manifest["phase"] == "reverted":
        raise MemoryEngineMigrationError("operation 已回滚，不能再次 apply")
    with _workspace_maintenance_locks(workspace):
        if manifest["phase"] == "applied":
            return verify_memory_migration(
                config_path=config_path,
                workspace=workspace,
                operation_id=operation_id,
                _locks_already_held=True,
            )
        _verify_prepared_artifacts(operation, manifest)
        if _installed_candidate_matches(config_path, workspace, operation, manifest):
            manifest["phase"] = "applied"
            manifest["appliedAt"] = manifest.get("appliedAt") or _utc_now()
            manifest["recoveredAfterCommit"] = True
            _atomic_write_json(operation / "manifest.json", manifest)
            return manifest
        if manifest["phase"] not in {"prepared", "applying", "apply_failed"}:
            raise MemoryEngineMigrationError(
                f"operation 当前阶段不能 apply: {manifest['phase']}"
            )

        _verify_apply_preconditions(config_path, workspace, operation, manifest)
        rollback = _ensure_rollback_backups(config_path, workspace, operation, manifest)
        manifest["rollback"] = rollback
        manifest["phase"] = "applying"
        manifest["updatedAt"] = _utc_now()
        _atomic_write_json(operation / "manifest.json", manifest)
        protected_before = _protected_state(workspace)
        source_before = _session_source_digest(workspace / "sessions.db")
        formal_write_started = False
        try:
            formal_write_started = True
            imported = _import_embedding_patch(
                workspace / "sessions.db",
                operation / "embedding-patch.db",
                operation_id,
                model=str(cast(dict[str, object], manifest["target"])["model"]),
            )
            _fault(fault_hook, "after_embedding_import")
            _verify_fixed_input(workspace, operation, manifest)
            _publish_candidate_pair(workspace, operation, manifest)
            _fault(fault_hook, "after_sidecar_publish")
            plugin, _ = _load_plugin_identity(workspace)
            paths = cast(dict[str, object], manifest["paths"])
            index_path = Path(str(paths["index"]))
            memory_path = Path(str(paths["memory"]))
            turns = load_turns(index_path)
            _verify_akasha_pair(
                index_path,
                memory_path if turns else None,
                plugin,
            )
            if source_before != _session_source_digest(workspace / "sessions.db"):
                raise MemoryEngineMigrationError(
                    "apply 改变了 sessions/messages 权威内容，已拒绝提交配置"
                )
            protected_after_sidecars = _protected_state(workspace)
            _assert_protected_equal(protected_before, protected_after_sidecars)
            _atomic_publish_from(
                operation / "config.candidate",
                config_path,
                mode=0o600,
            )
            _fault(fault_hook, "after_config_publish")
            loaded = Config.load(config_path, workspace=workspace)
            if not loaded.memory.enabled or loaded.memory.engine != "akasha":
                raise MemoryEngineMigrationError("候选配置发布后未启用 Akasha")
            source_after = _session_source_digest(workspace / "sessions.db")
            protected_after = _protected_state(workspace)
            if source_after != source_before:
                raise MemoryEngineMigrationError("配置发布改变了权威会话内容")
            _assert_protected_equal(protected_before, protected_after)
            manifest["phase"] = "applied"
            manifest["appliedAt"] = _utc_now()
            manifest["apply"] = {
                "importedEmbeddingRows": imported,
                "sourceDigestBefore": source_before,
                "sourceDigestAfter": source_after,
                "protectedBefore": protected_before,
                "protectedAfter": protected_after,
            }
            manifest.pop("lastError", None)
            manifest.pop("recoveryError", None)
            _atomic_write_json(operation / "manifest.json", manifest)
            return manifest
        except BaseException as exc:
            recovery_error: BaseException | None = None
            if formal_write_started:
                try:
                    _restore_config_and_sidecars(
                        config_path, workspace, operation, rollback
                    )
                except BaseException as restore_exc:
                    recovery_error = restore_exc
            manifest["phase"] = "apply_failed"
            manifest["lastError"] = _safe_error(exc)
            if recovery_error is not None:
                manifest["recoveryError"] = _safe_error(recovery_error)
            manifest["updatedAt"] = _utc_now()
            _atomic_write_json(operation / "manifest.json", manifest)
            if recovery_error is not None:
                raise MemoryEngineMigrationError(
                    "apply 失败且自动恢复没有完成；保持 runtime 停止并按收据人工恢复"
                ) from recovery_error
            raise


def verify_memory_migration(
    *,
    config_path: Path,
    workspace: Path,
    operation_id: str,
    _locks_already_held: bool = False,
) -> dict[str, object]:
    """Exercise the installed Akasha read boundary and return a fresh report."""

    config_path, workspace = _resolve_inputs(config_path, workspace)
    operation = _operation_path(workspace, operation_id)
    manifest = _load_manifest(operation, operation_id, workspace, config_path)

    def run() -> dict[str, object]:
        if manifest["phase"] != "applied":
            raise MemoryEngineMigrationError(
                f"只有 applied operation 可以验证正式状态: {manifest['phase']}"
            )
        host = Config.load(config_path, workspace=workspace)
        if not host.memory.enabled or host.memory.engine != "akasha":
            raise MemoryEngineMigrationError("正式配置当前没有启用 Akasha")
        target = cast(dict[str, object], manifest["target"])
        if _target_identity(host)["cacheNamespace"] != target["cacheNamespace"]:
            raise MemoryEngineMigrationError("正式 embedding identity 与迁移收据不一致")
        plugin, plugin_identity = _load_plugin_identity(workspace)
        expected_plugin = cast(dict[str, object], manifest["plugin"])
        if plugin_identity["effectiveSha256"] != expected_plugin["effectiveSha256"]:
            raise MemoryEngineMigrationError("Akasha plugin config 与迁移收据不一致")
        paths = cast(dict[str, object], manifest["paths"])
        index_path = Path(str(paths["index"]))
        memory_path = Path(str(paths["memory"]))
        expected_index, expected_memory = _sidecar_paths(workspace, plugin)
        if (index_path, memory_path) != (expected_index, expected_memory):
            raise MemoryEngineMigrationError("正式 sidecar 路径与迁移收据不一致")
        audit = audit_source_embeddings(
            workspace / "sessions.db",
            BuildConfig(
                embedding_model=host.memory.embedding.model,
                embedding_dimension=host.memory.embedding.output_dimensionality,
            ),
        )
        if not audit.complete:
            raise MemoryEngineMigrationError(_format_audit_failure(audit))
        turns = load_turns(index_path)
        if len(turns) != audit.eligible_turns:
            raise MemoryEngineMigrationError(
                "Akasha index 没有覆盖当前全部 eligible turns: "
                f"indexed={len(turns)} eligible={audit.eligible_turns}"
            )
        _verify_akasha_pair(index_path, memory_path if turns else None, plugin)
        report = {
            "status": "verified",
            "operationId": operation_id,
            "phase": "applied",
            "workspace": str(workspace),
            "sourceDigest": _session_source_digest(workspace / "sessions.db"),
            "eligibleTurns": audit.eligible_turns,
            "validEmbeddings": audit.valid_messages,
            "indexedTurns": len(turns),
            "indexSha256": _sha256_file(index_path),
            "memorySha256": _sha256_file(memory_path) if turns else None,
            "verifiedAt": _utc_now(),
        }
        _atomic_write_json(operation / "verify-latest.json", report)
        return report

    if _locks_already_held:
        return run()
    return run()


def revert_memory_migration(
    *,
    config_path: Path,
    workspace: Path,
    operation_id: str,
    confirmation: str,
    fault_hook: FaultHook | None = None,
) -> dict[str, object]:
    """Restore the pre-switch config and sidecars without rolling back chats."""

    if confirmation != REVERT_CONFIRMATION:
        raise MemoryEngineMigrationError(
            f"revert 会恢复切换前配置和 sidecar；请显式提供 {REVERT_CONFIRMATION}"
        )
    config_path, workspace = _resolve_inputs(config_path, workspace)
    operation = _operation_path(workspace, operation_id)
    manifest = _load_manifest(operation, operation_id, workspace, config_path)
    if manifest["phase"] == "reverted":
        return manifest
    if manifest["phase"] != "applied":
        raise MemoryEngineMigrationError(
            f"只有 applied operation 可以回滚: {manifest['phase']}"
        )
    rollback = cast(dict[str, object], manifest.get("rollback"))
    if not rollback:
        raise MemoryEngineMigrationError("迁移收据缺少 rollback 信息")
    with _workspace_maintenance_locks(workspace):
        _verify_rollback_backups(operation, rollback)
        current_hash = _sha256_file(config_path)
        candidate_hash = str(manifest["candidateConfigSha256"])
        original_hash = str(manifest["originalConfigSha256"])
        if current_hash not in {candidate_hash, original_hash}:
            raise MemoryEngineMigrationError("正式配置在切换后已改变，拒绝覆盖回滚")
        source_before = _session_source_digest(workspace / "sessions.db")
        protected_before = _protected_state(workspace)
        # Configuration is restored first so any crash leaves the classic engine active.
        rollback_config = cast(dict[str, object], rollback["config"])
        _verify_recorded_file(operation, rollback_config)
        _atomic_publish_from(
            operation / str(rollback_config["backup"]), config_path, mode=0o600
        )
        _fault(fault_hook, "after_revert_config")
        _restore_sidecars(workspace, operation, rollback)
        _fault(fault_hook, "after_revert_sidecars")
        _ = Config.load(config_path, workspace=workspace)
        if _sha256_file(config_path) != original_hash:
            raise MemoryEngineMigrationError("回滚后的配置 hash 不匹配")
        source_after = _session_source_digest(workspace / "sessions.db")
        protected_after = _protected_state(workspace)
        if source_after != source_before:
            raise MemoryEngineMigrationError("revert 改变了 sessions/messages")
        _assert_protected_equal(protected_before, protected_after)
        manifest["phase"] = "reverted"
        manifest["revertedAt"] = _utc_now()
        manifest["revert"] = {
            "sourceDigestBefore": source_before,
            "sourceDigestAfter": source_after,
            "protectedBefore": protected_before,
            "protectedAfter": protected_after,
            "embeddingRowsPreserved": True,
        }
        _atomic_write_json(operation / "manifest.json", manifest)
        return manifest


def _resolve_inputs(config_path: Path, workspace: Path) -> tuple[Path, Path]:
    expanded_workspace = workspace.expanduser()
    if expanded_workspace.is_symlink():
        raise MemoryEngineMigrationError(
            f"workspace 不能是符号链接: {expanded_workspace}"
        )
    resolved_config = config_path.expanduser().resolve(strict=False)
    resolved_workspace = expanded_workspace.resolve(strict=False)
    if not resolved_workspace.is_dir():
        raise MemoryEngineMigrationError(f"workspace 不存在: {resolved_workspace}")
    return resolved_config, resolved_workspace


def _operation_path(workspace: Path, operation_id: str) -> Path:
    if _OPERATION_ID.fullmatch(operation_id) is None:
        raise MemoryEngineMigrationError(
            f"operation ID 不是安全路径段: {operation_id!r}"
        )
    root = workspace / MIGRATION_ROOT
    _reject_existing_symlinks(workspace, root)
    operation = root / operation_id
    if operation.resolve(strict=False).parent != root.resolve(strict=False):
        raise MemoryEngineMigrationError("operation path 越界")
    _reject_existing_symlinks(workspace, operation)
    if operation.exists() and not operation.is_dir():
        raise MemoryEngineMigrationError(f"operation path 不是目录: {operation}")
    return operation


def _create_operation_directory(operation: Path) -> None:
    operation.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _reject_existing_symlinks(operation.parents[2], operation.parent)
    operation.mkdir(mode=0o700, exist_ok=False)
    os.chmod(operation.parent, 0o700)
    os.chmod(operation, 0o700)


def _reject_existing_symlinks(root: Path, target: Path) -> None:
    current = root
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise MemoryEngineMigrationError(f"路径越界: {target}") from exc
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise MemoryEngineMigrationError(f"迁移路径不能穿过符号链接: {current}")


def _candidate_config(original: bytes, embedding_model_id: str) -> bytes:
    try:
        document = tomlkit.parse(original.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MemoryEngineMigrationError("主配置不是有效 UTF-8 TOML") from exc
    raw_memory = document.get("memory")
    if raw_memory is None:
        memory = tomlkit.table()
        document["memory"] = memory
    elif not isinstance(raw_memory, dict):
        raise MemoryEngineMigrationError("[memory] 必须是 TOML table")
    else:
        memory = raw_memory
    memory["enabled"] = True
    memory["engine"] = "akasha"
    raw_embedding = memory.get("embedding")
    if raw_embedding is None:
        embedding = tomlkit.table()
        memory["embedding"] = embedding
    elif not isinstance(raw_embedding, dict):
        raise MemoryEngineMigrationError("[memory.embedding] 必须是 TOML table")
    else:
        embedding = raw_embedding
    if embedding_model_id:
        embedding["model_ref"] = embedding_model_id
    if not str(embedding.get("model_ref") or embedding.get("model") or "").strip():
        raise MemoryEngineMigrationError(
            "没有可用的向量模型；请先在 Dashboard 添加并验证模型，"
            "再向 prepare 传 --embedding-model-id"
        )
    return tomlkit.dumps(document).encode("utf-8")


def _memory_binding(payload: bytes) -> dict[str, object]:
    parsed = tomllib.loads(payload.decode("utf-8"))
    memory = parsed.get("memory")
    raw = cast(dict[str, object], memory) if isinstance(memory, dict) else {}
    embedding = raw.get("embedding")
    embedding_table = (
        cast(dict[str, object], embedding) if isinstance(embedding, dict) else {}
    )
    return {
        "configured": bool(raw),
        "enabled": bool(raw.get("enabled", False)),
        "engine": str(raw.get("engine") or "default"),
        "modelRef": str(embedding_table.get("model_ref") or ""),
        "model": str(embedding_table.get("model") or ""),
    }


def _target_identity(host: Config) -> dict[str, object]:
    embedding = host.memory.embedding
    namespace_payload = json.dumps(
        {
            "url": (
                embedding.base_url or host.light_base_url or host.base_url or ""
            ).rstrip("/")
            + "/embeddings",
            "model": embedding.model,
            "dimensions": embedding.output_dimensionality,
            "max_text_len": Embedder.MAX_TEXT_LEN,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "engine": "akasha",
        "model": embedding.model,
        "configuredDimension": embedding.output_dimensionality,
        "cacheNamespace": hashlib.sha256(namespace_payload.encode("utf-8")).hexdigest(),
    }


def _build_embedder(host: Config) -> Embedder:
    embedding = host.memory.embedding
    return Embedder(
        base_url=embedding.base_url or host.light_base_url or host.base_url or "",
        api_key=embedding.api_key or host.light_api_key or host.api_key,
        model=embedding.model,
        output_dimensionality=embedding.output_dimensionality,
    )


def _load_plugin_identity(workspace: Path) -> tuple[AkashaConfig, dict[str, object]]:
    path = builtin_plugin_data_dir("akasha", workspace) / "config.local.toml"
    plugin = load_akasha_config(path)
    effective = json.dumps(
        asdict(plugin), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return plugin, {
        "path": str(path),
        "exists": path.is_file(),
        "fileSha256": _sha256_file(path) if path.is_file() else None,
        "effectiveSha256": _sha256_bytes(effective),
        "effective": asdict(plugin),
    }


def _sidecar_paths(workspace: Path, plugin: AkashaConfig) -> tuple[Path, Path]:
    index = resolve_workspace_path(workspace, plugin.index_path)
    memory = resolve_workspace_path(workspace, plugin.db_path)
    if index == memory:
        raise MemoryEngineMigrationError("Akasha index 与 graph 不能使用同一路径")
    reserved = {
        (workspace / "sessions.db").resolve(strict=False),
        *(path.resolve(strict=False) for path in _protected_paths(workspace)),
    }
    migration_root = (workspace / MIGRATION_ROOT).resolve(strict=False)
    for name, path in (("index", index), ("memory", memory)):
        resolved = path.resolve(strict=False)
        if resolved in reserved or resolved.is_relative_to(migration_root):
            raise MemoryEngineMigrationError(
                f"Akasha {name} 路径与权威/恢复状态冲突: {path}"
            )
        _assert_inside_workspace(workspace, path)
        if path.exists() and not path.is_file():
            raise MemoryEngineMigrationError(f"Akasha {name} 目标不是普通文件: {path}")
    return index, memory


def _legacy_memory_path(workspace: Path) -> Path:
    """Resolve the classic engine's configured canonical database path."""

    default_config = load_default_memory_config(workspace=workspace)
    return resolve_memory_db_path(
        workspace=workspace,
        default_config=default_config,
    )


def _protected_paths(workspace: Path) -> tuple[Path, ...]:
    return (
        _legacy_memory_path(workspace),
        *(workspace / relative for relative in _MARKDOWN_MEMORY_PATHS),
    )


def _backup_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise MemoryEngineMigrationError(f"SQLite 源不存在或不是普通文件: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_db:
        _load_known_sqlite_extensions(source_db)
        source_db.execute("PRAGMA query_only = ON")
        _assert_sqlite_integrity_connection(source_db, source)
        with closing(sqlite3.connect(destination)) as destination_db:
            source_db.backup(destination_db, pages=256, sleep=0.05)
            destination_db.commit()
            _load_known_sqlite_extensions(destination_db)
            _assert_sqlite_integrity_connection(destination_db, destination)
    os.chmod(destination, 0o600)
    _fsync_directory(destination.parent)


def _ensure_embedding_schema(path: Path) -> None:
    if path.is_symlink():
        raise MemoryEngineMigrationError(f"embedding database 不能是符号链接: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(_EMBEDDING_SCHEMA)
        connection.commit()
    os.chmod(path, 0o600)


def _resolved_patch_dimension(
    snapshot: Path,
    required: Sequence[RequiredEmbeddingMessage],
    model: str,
    configured: int | None,
) -> int | None:
    if configured is not None:
        return configured
    required_ids = {item.message_id for item in required}
    if not required_ids:
        return None
    with closing(
        sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
    ) as db:
        dimensions = {
            int(row[1])
            for row in db.execute(
                "SELECT message_id, dim FROM message_embeddings WHERE model = ?",
                (model,),
            )
            if str(row[0]) in required_ids and int(row[1]) > 0
        }
    return next(iter(dimensions)) if len(dimensions) == 1 else None


def _validate_embedding_batch(
    messages: Sequence[RequiredEmbeddingMessage],
    vectors: Sequence[Sequence[float]],
    *,
    expected_dimension: int | None,
) -> int:
    resolved = expected_dimension
    for message, vector in zip(messages, vectors, strict=True):
        if not vector:
            raise MemoryEngineMigrationError(
                f"embedding provider 返回空向量: {message.message_id}"
            )
        if any(not math.isfinite(float(value)) for value in vector):
            raise MemoryEngineMigrationError(
                f"embedding provider 返回非有限数字: {message.message_id}"
            )
        if math.sqrt(sum(float(value) ** 2 for value in vector)) == 0.0:
            raise MemoryEngineMigrationError(
                f"embedding provider 返回零向量: {message.message_id}"
            )
        if resolved is None:
            resolved = len(vector)
        if len(vector) != resolved:
            raise MemoryEngineMigrationError(
                f"embedding 维度不一致: message={message.message_id} "
                f"expected={resolved} actual={len(vector)}"
            )
        try:
            encoded = struct.pack(
                f"<{len(vector)}f", *[float(value) for value in vector]
            )
        except (OverflowError, struct.error) as exc:
            raise MemoryEngineMigrationError(
                f"embedding 无法表示为 float32: {message.message_id}"
            ) from exc
        _validate_embedding_blob(encoded, len(vector), message.message_id)
    if resolved is None:
        raise MemoryEngineMigrationError("空 batch 无法确定 embedding 维度")
    return resolved


def _upsert_embeddings(
    path: Path,
    messages: Sequence[RequiredEmbeddingMessage],
    vectors: Sequence[Sequence[float]],
    *,
    model: str,
) -> None:
    now = _utc_now()
    rows = [
        (
            message.message_id,
            _content_hash(message.content),
            model,
            struct.pack(f"<{len(vector)}f", *[float(value) for value in vector]),
            len(vector),
            now,
            now,
        )
        for message, vector in zip(messages, vectors, strict=True)
    ]
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.executemany(
                """
                INSERT INTO message_embeddings
                    (message_id, content_hash, model, embedding, dim, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, model) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    embedding = excluded.embedding,
                    dim = excluded.dim,
                    updated_at = excluded.updated_at
                """,
                rows,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def _reconcile_patch(
    snapshot: Path,
    patch: Path,
    required: Mapping[str, RequiredEmbeddingMessage],
    model: str,
) -> None:
    with closing(
        sqlite3.connect(f"{patch.resolve().as_uri()}?mode=ro", uri=True)
    ) as db:
        rows = db.execute(
            """
            SELECT message_id, content_hash, model, embedding, dim, created_at, updated_at
            FROM message_embeddings WHERE model = ? ORDER BY message_id
            """,
            (model,),
        ).fetchall()
    valid = []
    for row in rows:
        message = required.get(str(row[0]))
        if message is None or str(row[1]) != _content_hash(message.content):
            continue
        _validate_embedding_blob(bytes(row[3]), int(row[4]), str(row[0]))
        valid.append(row)
    if not valid:
        return
    with closing(sqlite3.connect(snapshot)) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany(
                """
                INSERT INTO message_embeddings
                    (message_id, content_hash, model, embedding, dim, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, model) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    embedding=excluded.embedding,
                    dim=excluded.dim,
                    updated_at=excluded.updated_at
                """,
                valid,
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _validate_embedding_blob(blob: bytes, dimension: int, message_id: str) -> None:
    if dimension <= 0 or len(blob) != dimension * 4:
        raise MemoryEngineMigrationError(f"向量 blob 维度损坏: {message_id}")
    values = struct.unpack(f"<{dimension}f", blob)
    if any(not math.isfinite(value) for value in values):
        raise MemoryEngineMigrationError(f"向量包含非有限数字: {message_id}")
    if math.sqrt(sum(value * value for value in values)) == 0.0:
        raise MemoryEngineMigrationError(f"向量为零: {message_id}")


def _patch_row_count(path: Path, model: str) -> int:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        row = db.execute(
            "SELECT COUNT(*) FROM message_embeddings WHERE model = ?", (model,)
        ).fetchone()
    return int(row[0]) if row else 0


def _session_source_digest(path: Path) -> str:
    """Hash complete sessions/messages schemas and rows, excluding allowed caches."""

    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.execute("BEGIN")
        _require_columns(db, "sessions", {"key", "metadata"})
        _require_columns(
            db,
            "messages",
            {"id", "session_key", "seq", "role", "content", "extra", "ts"},
        )
        digest = hashlib.sha256()
        _update_canonical_digest(digest, {"databaseSchema": 2})
        for name in ("sessions", "messages"):
            _update_table_digest(db, name, digest)
    return digest.hexdigest()


def _frozen_embedding_digest(
    path: Path,
    required: Sequence[RequiredEmbeddingMessage],
    model: str,
    expected_dimension: int | None,
    *,
    overlay: Mapping[str, tuple[str, bytes, int]] | None = None,
) -> str:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_embeddings'"
        ).fetchone()
        rows = (
            db.execute(
                """
                SELECT message_id, content_hash, embedding, dim
                FROM message_embeddings WHERE model = ? ORDER BY message_id
                """,
                (model,),
            ).fetchall()
            if table is not None
            else []
        )
    by_id: dict[str, tuple[str, bytes, int]] = {
        str(row[0]): (str(row[1]), bytes(row[2]), int(row[3])) for row in rows
    }
    if overlay:
        by_id.update(overlay)
    payload = []
    for message in sorted(required, key=lambda item: item.message_id.encode("utf-8")):
        row = by_id.get(message.message_id)
        if row is None:
            raise MemoryEngineMigrationError(
                f"固定输入缺少 embedding: {message.message_id}"
            )
        content_hash, blob, dimension = row
        if content_hash != _content_hash(message.content):
            raise MemoryEngineMigrationError(
                f"固定输入正文 hash 不匹配: {message.message_id}"
            )
        if expected_dimension is not None and dimension != expected_dimension:
            raise MemoryEngineMigrationError(
                f"固定输入维度不匹配: {message.message_id}"
            )
        _validate_embedding_blob(blob, dimension, message.message_id)
        payload.append(
            {
                "messageId": message.message_id,
                "contentHash": content_hash,
                "dimension": dimension,
                "embedding": blob.hex(),
            }
        )
    return _canonical_digest({"schema": 1, "model": model, "rows": payload})


def _projected_fixed_input_digest(
    sessions: Path,
    patch: Path,
    required: Sequence[RequiredEmbeddingMessage],
    model: str,
    expected_dimension: int | None,
) -> str:
    required_by_id = {item.message_id: item for item in required}
    overlay: dict[str, tuple[str, bytes, int]] = {}
    with closing(
        sqlite3.connect(f"{patch.resolve().as_uri()}?mode=ro", uri=True)
    ) as db:
        for row in db.execute(
            """
            SELECT message_id, content_hash, embedding, dim
            FROM message_embeddings WHERE model = ? ORDER BY message_id
            """,
            (model,),
        ):
            message_id = str(row[0])
            message = required_by_id.get(message_id)
            if message is None:
                raise MemoryEngineMigrationError(
                    f"embedding patch 包含非 eligible 消息: {message_id}"
                )
            content_hash = str(row[1])
            blob = bytes(row[2])
            dimension = int(row[3])
            if content_hash != _content_hash(message.content):
                raise MemoryEngineMigrationError(
                    f"embedding patch 正文 hash 不匹配: {message_id}"
                )
            if expected_dimension is not None and dimension != expected_dimension:
                raise MemoryEngineMigrationError(
                    f"embedding patch 维度不匹配: {message_id}"
                )
            _validate_embedding_blob(blob, dimension, message_id)
            overlay[message_id] = (content_hash, blob, dimension)
    return _frozen_embedding_digest(
        sessions,
        required,
        model,
        expected_dimension,
        overlay=overlay,
    )


def _verify_prepare_resume_state(
    operation: Path,
    workspace: Path,
    manifest: dict[str, object],
) -> None:
    """Reject mutation of a resumable operation before another provider call."""

    before = _read_required_file(operation / "config.before", "原配置快照")
    candidate = _read_required_file(operation / "config.candidate", "候选配置")
    if _sha256_bytes(before) != manifest.get("originalConfigSha256"):
        raise MemoryEngineMigrationError("续跑 operation 的原配置快照已漂移")
    if _sha256_bytes(candidate) != manifest.get("candidateConfigSha256"):
        raise MemoryEngineMigrationError("续跑 operation 的候选配置已漂移")

    snapshot = operation / "source-sessions.db"
    if not snapshot.is_file() or snapshot.is_symlink():
        raise MemoryEngineMigrationError("续跑 operation 缺少可信的 SessionDB 快照")
    _assert_sqlite_integrity(snapshot)
    raw_source = manifest.get("source")
    if not isinstance(raw_source, dict):
        raise MemoryEngineMigrationError("续跑 operation 缺少 source 收据")
    source = cast(dict[str, object], raw_source)
    if _session_source_digest(snapshot) != source.get("messageDigest"):
        raise MemoryEngineMigrationError("续跑 operation 的 sessions/messages 已漂移")
    required = list_required_embedding_messages(snapshot)
    if len(required) != int(cast(int, source.get("requiredMessages", -1))):
        raise MemoryEngineMigrationError(
            "续跑 operation 的 eligible message 集合已漂移"
        )

    _, plugin_identity = _load_plugin_identity(workspace)
    raw_plugin = manifest.get("plugin")
    if not isinstance(raw_plugin, dict):
        raise MemoryEngineMigrationError("续跑 operation 缺少 plugin 收据")
    expected_plugin = cast(dict[str, object], raw_plugin)
    if plugin_identity["effectiveSha256"] != expected_plugin.get("effectiveSha256"):
        raise MemoryEngineMigrationError("Akasha plugin config 在 prepare 续跑前已漂移")


def _verify_apply_preconditions(
    config_path: Path,
    workspace: Path,
    operation: Path,
    manifest: dict[str, object],
) -> None:
    if _sha256_file(config_path) != manifest["originalConfigSha256"]:
        raise MemoryEngineMigrationError("主配置在 prepare 后已漂移；未写入正式数据")
    if (
        _session_source_digest(workspace / "sessions.db")
        != cast(dict[str, object], manifest["source"])["messageDigest"]
    ):
        raise MemoryEngineMigrationError(
            "prepare 后 sessions/messages 的 schema 或内容发生变化（包括新消息或"
            " session metadata）；请停机后重新 prepare"
        )
    _, plugin_identity = _load_plugin_identity(workspace)
    if (
        plugin_identity["effectiveSha256"]
        != cast(dict[str, object], manifest["plugin"])["effectiveSha256"]
    ):
        raise MemoryEngineMigrationError("Akasha plugin config 在 prepare 后已漂移")
    plugin = load_akasha_config(
        Path(str(cast(dict[str, object], manifest["plugin"])["path"]))
    )
    actual_index, actual_memory = _sidecar_paths(workspace, plugin)
    recorded_paths = cast(dict[str, object], manifest["paths"])
    if (str(actual_index), str(actual_memory)) != (
        str(recorded_paths["index"]),
        str(recorded_paths["memory"]),
    ):
        raise MemoryEngineMigrationError("迁移收据中的 Akasha sidecar 路径已漂移")
    host = Config.load(operation / "config.candidate", workspace=workspace)
    target = cast(dict[str, object], manifest["target"])
    if _target_identity(host)["cacheNamespace"] != target["cacheNamespace"]:
        raise MemoryEngineMigrationError("目标 embedding identity 已漂移")
    required = list_required_embedding_messages(operation / "source-sessions.db")
    expected_digest = str(
        cast(dict[str, object], manifest["source"])["frozenInputDigest"]
    )
    projected = _projected_fixed_input_digest(
        workspace / "sessions.db",
        operation / "embedding-patch.db",
        required,
        host.memory.embedding.model,
        cast(int | None, target.get("resolvedDimension")),
    )
    if projected != expected_digest:
        raise MemoryEngineMigrationError(
            "正式库加上 embedding patch 后仍不等于 prepared 固定输入；未执行写入"
        )


def _verify_fixed_input(
    workspace: Path,
    operation: Path,
    manifest: dict[str, object],
) -> None:
    host = Config.load(operation / "config.candidate", workspace=workspace)
    target = cast(dict[str, object], manifest["target"])
    required = list_required_embedding_messages(operation / "source-sessions.db")
    actual = _frozen_embedding_digest(
        workspace / "sessions.db",
        required,
        host.memory.embedding.model,
        cast(int | None, target.get("resolvedDimension")),
    )
    expected = str(cast(dict[str, object], manifest["source"])["frozenInputDigest"])
    if actual != expected:
        raise MemoryEngineMigrationError(
            "正式 embedding 固定输入与 prepared 收据不一致"
        )
    audit = audit_source_embeddings(
        workspace / "sessions.db",
        BuildConfig(
            embedding_model=host.memory.embedding.model,
            embedding_dimension=cast(int | None, target.get("resolvedDimension")),
        ),
    )
    if not audit.complete:
        raise MemoryEngineMigrationError(_format_audit_failure(audit))


def _import_embedding_patch(
    sessions: Path,
    patch: Path,
    operation_id: str,
    *,
    model: str,
) -> int:
    with closing(
        sqlite3.connect(f"{patch.resolve().as_uri()}?mode=ro", uri=True)
    ) as source:
        rows = source.execute(
            """
            SELECT message_id, content_hash, model, embedding, dim, created_at, updated_at
            FROM message_embeddings WHERE model = ? ORDER BY message_id, model
            """,
            (model,),
        ).fetchall()
    with closing(sqlite3.connect(sessions)) as target:
        target.executescript(_EMBEDDING_SCHEMA)
        target.commit()
        target.execute("BEGIN IMMEDIATE")
        try:
            target.executemany(
                """
                INSERT INTO message_embeddings
                    (message_id, content_hash, model, embedding, dim, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, model) DO UPDATE SET
                    content_hash=excluded.content_hash,
                    embedding=excluded.embedding,
                    dim=excluded.dim,
                    updated_at=excluded.updated_at
                """,
                rows,
            )
            target.execute(
                """
                INSERT INTO message_embedding_migrations
                    (source_id, completed_at, imported_count)
                VALUES (?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    completed_at=excluded.completed_at,
                    imported_count=excluded.imported_count
                """,
                (f"akasha-first-adoption:{operation_id}", _utc_now(), len(rows)),
            )
            target.commit()
        except BaseException:
            target.rollback()
            raise
    return len(rows)


def _publish_candidate_pair(
    workspace: Path,
    operation: Path,
    manifest: dict[str, object],
) -> None:
    paths = cast(dict[str, object], manifest["paths"])
    index_target = Path(str(paths["index"]))
    memory_target = Path(str(paths["memory"]))
    raw_effective = cast(dict[str, object], manifest["plugin"]).get("effective")
    if not isinstance(raw_effective, dict):
        raise MemoryEngineMigrationError("迁移收据缺少 Akasha effective config")
    plugin = AkashaConfig(**cast(dict[str, Any], raw_effective))
    expected_index, expected_memory = _sidecar_paths(workspace, plugin)
    if (index_target, memory_target) != (expected_index, expected_memory):
        raise MemoryEngineMigrationError("收据中的 sidecar 路径不等于当前 Akasha 配置")
    index_target.parent.mkdir(parents=True, exist_ok=True)
    memory_target.parent.mkdir(parents=True, exist_ok=True)
    candidate_index = operation / "akasha-v2-index.candidate.db"
    candidate_memory = operation / "akasha.candidate.db"
    if candidate_memory.exists():
        _atomic_publish_from(candidate_memory, memory_target, mode=0o600)
    else:
        memory_target.unlink(missing_ok=True)
        _fsync_directory(memory_target.parent)
    _atomic_publish_from(candidate_index, index_target, mode=0o600)


def _verify_akasha_pair(
    index: Path,
    memory: Path | None,
    plugin: AkashaConfig,
) -> None:
    _assert_sqlite_integrity(index)
    turns = load_turns(index)
    if not turns:
        if memory is not None and memory.exists():
            raise MemoryEngineMigrationError("空 Akasha index 不应存在 graph snapshot")
        return
    if memory is None or not memory.is_file():
        raise MemoryEngineMigrationError("非空 Akasha index 缺少 graph snapshot")
    _assert_sqlite_integrity(memory)
    _ = load_memory_state(
        memory,
        turns=turns,
        config=plugin.memory_config(),
        source_index_sha256=_sha256_file(index),
    )


def _verify_online_runtime_shadow(
    *,
    operation: Path,
    sessions: Path,
    index: Path,
    memory: Path | None,
    plugin: AkashaConfig,
    embedding_model: str,
    embedding_dimension: int | None,
) -> None:
    """Load copied candidates through the exact production runtime boundary."""

    expected_turns = load_turns(index)
    with tempfile.TemporaryDirectory(
        prefix="runtime-shadow-",
        dir=operation,
    ) as raw_shadow:
        shadow = Path(raw_shadow)
        shadow_index = shadow / index.name
        shadow_memory = shadow / "akasha.db"
        _atomic_publish_from(index, shadow_index, mode=0o600)
        if memory is not None:
            _atomic_publish_from(memory, shadow_memory, mode=0o600)
        runtime = OnlineMemoryRuntime(
            sessions_path=sessions,
            index_path=shadow_index,
            memory_path=shadow_memory,
            embedding_model=embedding_model,
            embedding_dimension=embedding_dimension,
            config=plugin.memory_config(),
        )
        try:
            if len(runtime.cycle.turns) != len(expected_turns):
                raise MemoryEngineMigrationError(
                    "生产 Akasha runtime 加载后的 turn 数与候选不一致"
                )
            if expected_turns:
                dense = expected_turns[0].user_dense
                if dense is None:
                    raise MemoryEngineMigrationError(
                        "候选 Akasha 首个 turn 缺少 user embedding"
                    )
                _ = runtime.query_turn(
                    text="Akasha migration retrieval smoke",
                    dense=dense,
                    session_key="migration:shadow",
                    timestamp=datetime.now(timezone.utc),
                    capture_paths=False,
                )
        finally:
            runtime.close()


def _ensure_rollback_backups(
    config_path: Path,
    workspace: Path,
    operation: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    existing = manifest.get("rollback")
    if isinstance(existing, dict):
        _verify_rollback_backups(operation, cast(dict[str, object], existing))
        return cast(dict[str, object], existing)
    rollback_dir = operation / "rollback"
    rollback_manifest = rollback_dir / "manifest.json"
    if rollback_manifest.is_file():
        recovered = _load_json(rollback_manifest)
        _verify_rollback_backups(operation, recovered)
        return recovered
    if rollback_dir.exists():
        raise MemoryEngineMigrationError(
            f"存在没有完整收据的 rollback 目录，未执行正式写入: {rollback_dir}"
        )
    staging_dir = operation / f".rollback-{uuid4().hex}.staging"
    staging_dir.mkdir(mode=0o700, exist_ok=False)
    os.chmod(staging_dir, 0o700)
    config_backup = staging_dir / "config.before"
    _atomic_write_bytes(config_backup, config_path.read_bytes(), 0o600)
    paths = cast(dict[str, object], manifest["paths"])
    sidecars: dict[str, object] = {}
    for name in ("index", "memory"):
        source = Path(str(paths[name]))
        _assert_inside_workspace(workspace, source)
        if source.exists():
            backup = staging_dir / f"{name}.before.db"
            _backup_sqlite(source, backup)
            sidecars[name] = {
                "existed": True,
                "backup": f"rollback/{backup.name}",
                "sha256": _sha256_file(backup),
            }
        else:
            sidecars[name] = {"existed": False, "backup": None, "sha256": None}
    protected_dir = staging_dir / "protected"
    protected_dir.mkdir(mode=0o700)
    protected: dict[str, object] = {}
    for source in _protected_paths(workspace):
        relative = source.relative_to(workspace)
        if not source.exists():
            protected[relative.as_posix()] = {"existed": False}
            continue
        if source.is_symlink() or not source.is_file():
            raise MemoryEngineMigrationError(f"受保护路径不是普通文件: {source}")
        destination = protected_dir / relative
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if source.suffix == ".db":
            _backup_sqlite(source, destination)
        else:
            _atomic_write_bytes(destination, source.read_bytes(), 0o600)
        protected[relative.as_posix()] = {
            "existed": True,
            "backup": str(Path("rollback/protected") / relative),
            "sha256": _sha256_file(destination),
        }
    rollback = {
        "createdAt": _utc_now(),
        "config": {
            "backup": "rollback/config.before",
            "sha256": _sha256_file(config_backup),
        },
        "sidecars": sidecars,
        "protected": protected,
    }
    _atomic_write_json(staging_dir / "manifest.json", rollback)
    _fsync_directory(staging_dir)
    os.replace(staging_dir, rollback_dir)
    _fsync_directory(operation)
    _verify_rollback_backups(operation, rollback)
    return rollback


def _verify_rollback_backups(operation: Path, rollback: dict[str, object]) -> None:
    config = cast(dict[str, object], rollback["config"])
    _verify_recorded_file(operation, config)
    sidecars = cast(dict[str, object], rollback["sidecars"])
    for raw in sidecars.values():
        record = cast(dict[str, object], raw)
        if record.get("existed"):
            _verify_recorded_file(operation, record)
            _assert_sqlite_integrity(operation / str(record["backup"]))
    protected = cast(dict[str, object], rollback["protected"])
    for name, raw in protected.items():
        record = cast(dict[str, object], raw)
        if not record.get("existed"):
            continue
        _verify_recorded_file(operation, record)
        if str(name).endswith(".db"):
            _assert_sqlite_integrity(operation / str(record["backup"]))


def _restore_config_and_sidecars(
    config_path: Path,
    workspace: Path,
    operation: Path,
    rollback: dict[str, object],
) -> None:
    _verify_rollback_backups(operation, rollback)
    # Restore config first so a subsequent accidental start selects the old engine.
    config = cast(dict[str, object], rollback["config"])
    _atomic_publish_from(operation / str(config["backup"]), config_path, mode=0o600)
    _restore_sidecars(workspace, operation, rollback)


def _restore_sidecars(
    workspace: Path,
    operation: Path,
    rollback: dict[str, object],
) -> None:
    manifest = _load_json(operation / "manifest.json")
    paths = cast(dict[str, object], manifest["paths"])
    raw_effective = cast(dict[str, object], manifest["plugin"]).get("effective")
    if not isinstance(raw_effective, dict):
        raise MemoryEngineMigrationError("回滚收据缺少 Akasha effective config")
    plugin = AkashaConfig(**cast(dict[str, Any], raw_effective))
    expected_index, expected_memory = _sidecar_paths(workspace, plugin)
    if (str(paths["index"]), str(paths["memory"])) != (
        str(expected_index),
        str(expected_memory),
    ):
        raise MemoryEngineMigrationError("回滚收据中的 sidecar 路径已漂移")
    sidecars = cast(dict[str, object], rollback["sidecars"])
    for name in ("memory", "index"):
        target = Path(str(paths[name]))
        _assert_inside_workspace(workspace, target)
        record = cast(dict[str, object], sidecars[name])
        if not record.get("existed"):
            target.unlink(missing_ok=True)
            _fsync_directory(target.parent)
            continue
        backup = operation / str(record["backup"])
        _atomic_publish_from(backup, target, mode=0o600)
        if _sha256_file(target) != record["sha256"]:
            raise MemoryEngineMigrationError(f"恢复后的 {name} sidecar hash 不匹配")


def _installed_candidate_matches(
    config_path: Path,
    workspace: Path,
    operation: Path,
    manifest: dict[str, object],
) -> bool:
    if not config_path.is_file() or _sha256_file(config_path) != manifest.get(
        "candidateConfigSha256"
    ):
        return False
    try:
        _verify_fixed_input(workspace, operation, manifest)
        plugin, _ = _load_plugin_identity(workspace)
        paths = cast(dict[str, object], manifest["paths"])
        index = Path(str(paths["index"]))
        memory = Path(str(paths["memory"]))
        turns = load_turns(index)
        _verify_akasha_pair(index, memory if turns else None, plugin)
        host = Config.load(config_path, workspace=workspace)
        audit = audit_source_embeddings(
            workspace / "sessions.db",
            BuildConfig(
                embedding_model=host.memory.embedding.model,
                embedding_dimension=host.memory.embedding.output_dimensionality,
            ),
        )
        if not audit.complete or len(turns) != audit.eligible_turns:
            return False
    except (OSError, sqlite3.Error, ValueError, RuntimeError):
        return False
    return True


def _verify_prepared_artifacts(
    operation: Path,
    manifest: dict[str, object],
) -> None:
    if manifest.get("phase") not in {
        "prepared",
        "applying",
        "apply_failed",
        "applied",
    }:
        raise MemoryEngineMigrationError(
            f"operation 尚未 prepared: {manifest.get('phase')}"
        )
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict):
        raise MemoryEngineMigrationError("prepared 收据缺少 artifacts")
    for raw in artifacts.values():
        if raw is None:
            continue
        _verify_recorded_file(operation, cast(dict[str, object], raw))
    _assert_sqlite_integrity(operation / "source-sessions.db")
    _assert_sqlite_integrity(operation / "embedding-patch.db")
    raw_effective = cast(dict[str, object], manifest["plugin"]).get("effective")
    if not isinstance(raw_effective, dict):
        raise MemoryEngineMigrationError("prepared 收据缺少 Akasha effective config")
    plugin = AkashaConfig(**cast(dict[str, Any], raw_effective))
    plugin.validate()
    candidate_index = operation / "akasha-v2-index.candidate.db"
    candidate_memory = operation / "akasha.candidate.db"
    turns = load_turns(candidate_index)
    _verify_akasha_pair(
        candidate_index,
        candidate_memory if turns else None,
        plugin,
    )


def _artifact_record(operation: Path, path: Path) -> dict[str, object]:
    return {
        "path": str(path.relative_to(operation)),
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _verify_recorded_file(operation: Path, record: dict[str, object]) -> None:
    raw_path = record.get("path", record.get("backup"))
    if not isinstance(raw_path, str):
        raise MemoryEngineMigrationError("artifact record 缺少 path")
    unresolved = operation / raw_path
    try:
        _reject_existing_symlinks(operation, unresolved)
    except MemoryEngineMigrationError:
        raise
    path = unresolved.resolve(strict=False)
    if not path.is_relative_to(operation.resolve(strict=False)):
        raise MemoryEngineMigrationError(f"artifact path 越界: {raw_path}")
    if not path.is_file() or path.is_symlink():
        raise MemoryEngineMigrationError(f"artifact 不存在或不是普通文件: {path}")
    expected_size = record.get("size")
    if expected_size is not None and path.stat().st_size != int(
        cast(int, expected_size)
    ):
        raise MemoryEngineMigrationError(f"artifact size 已漂移: {path}")
    if _sha256_file(path) != record.get("sha256"):
        raise MemoryEngineMigrationError(f"artifact hash 已漂移: {path}")


def _export_legacy_memory(workspace: Path, operation: Path) -> dict[str, object]:
    source = _legacy_memory_path(workspace)
    report_path = operation / "legacy-memory-review.json"
    if not source.exists():
        payload: dict[str, object] = {"status": "absent", "count": 0, "items": []}
        _atomic_write_json(report_path, payload)
        return {"status": "absent", "count": 0}
    snapshot = operation / "legacy-memory2-snapshot.db"
    if not snapshot.exists():
        _backup_sqlite(source, snapshot)
    with closing(
        sqlite3.connect(f"{snapshot.resolve().as_uri()}?mode=ro", uri=True)
    ) as db:
        table = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_items'"
        ).fetchone()
        if table is None:
            payload = {"status": "schema_not_applicable", "count": 0, "items": []}
        else:
            columns = [
                "id",
                "memory_type",
                "summary",
                "reinforcement",
                "emotional_weight",
                "extra_json",
                "source_ref",
                "happened_at",
                "status",
                "created_at",
                "updated_at",
            ]
            actual = {
                str(row[1]) for row in db.execute("PRAGMA table_info(memory_items)")
            }
            selected = [column for column in columns if column in actual]
            rows = db.execute(
                f"SELECT {', '.join(selected)} FROM memory_items ORDER BY id"
            ).fetchall()
            items = [dict(zip(selected, row, strict=True)) for row in rows]
            payload = {"status": "exported", "count": len(items), "items": items}
    _atomic_write_json(report_path, payload)
    return {"status": payload["status"], "count": payload["count"]}


def _protected_state(workspace: Path) -> dict[str, object]:
    state: dict[str, object] = {}
    for path in _protected_paths(workspace):
        relative = path.relative_to(workspace)
        key = relative.as_posix()
        if not path.exists():
            state[key] = {"exists": False}
            continue
        if path.is_symlink() or not path.is_file():
            raise MemoryEngineMigrationError(f"受保护路径不是普通文件: {path}")
        record: dict[str, object] = {
            "exists": True,
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        }
        if path.suffix == ".db":
            record["logicalSha256"] = _sqlite_logical_digest(path)
            record["auxiliary"] = {
                suffix: _optional_file_state(Path(f"{path}{suffix}"))
                for suffix in ("-wal", "-shm")
            }
        state[key] = record
    return state


def _assert_protected_equal(
    before: dict[str, object], after: dict[str, object]
) -> None:
    if before != after:
        changed = sorted(
            key for key in set(before) | set(after) if before.get(key) != after.get(key)
        )
        raise MemoryEngineMigrationError(
            f"迁移触碰了受保护的经典/Markdown 记忆: {changed}"
        )


def _sqlite_logical_digest(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        _load_known_sqlite_extensions(db)
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        _assert_sqlite_integrity_connection(db, path)
        schema = db.execute("""
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name
            """).fetchall()
        digest = hashlib.sha256()
        _update_canonical_digest(digest, {"databaseSchema": 1})
        for row in schema:
            _update_canonical_digest(
                digest, {"schema": [_json_value(value) for value in row]}
            )
        table_names = [str(row[1]) for row in schema if row[0] == "table"]
        for table in table_names:
            _update_table_digest(db, table, digest)
    return digest.hexdigest()


def _update_table_digest(
    connection: sqlite3.Connection,
    table: str,
    digest: Any,
) -> None:
    quoted = table.replace('"', '""')
    info = connection.execute(f'PRAGMA table_info("{quoted}")').fetchall()
    columns = [str(row[1]) for row in info]
    primary = [
        str(row[1])
        for row in sorted(
            info,
            key=lambda item: int(item[5]) if int(item[5]) > 0 else len(info) + 1,
        )
        if int(row[5]) > 0
    ]
    order_columns = primary or columns
    order = ", ".join(
        f'"{column.replace(chr(34), chr(34) * 2)}"' for column in order_columns
    )
    query = f'SELECT * FROM "{quoted}"' + (f" ORDER BY {order}" if order else "")
    _update_canonical_digest(
        digest,
        {
            "table": table,
            "columns": [[_json_value(value) for value in row] for row in info],
        },
    )
    row_count = 0
    for row in connection.execute(query):
        _update_canonical_digest(digest, [_json_value(value) for value in row])
        row_count += 1
    _update_canonical_digest(digest, {"endTable": table, "rowCount": row_count})


def _optional_file_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"exists": False}
    if path.is_symlink() or not path.is_file():
        raise MemoryEngineMigrationError(f"SQLite auxiliary 不是普通文件: {path}")
    return {
        "exists": True,
        "size": path.stat().st_size,
        "sha256": _sha256_file(path),
    }


def _json_value(value: object) -> object:
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"float": repr(value)}
        return {"float": value.hex()}
    return value


def _load_manifest(
    operation: Path,
    operation_id: str,
    workspace: Path,
    config_path: Path,
) -> dict[str, object]:
    manifest = _load_json(operation / "manifest.json")
    if manifest.get("schemaVersion") != MIGRATION_SCHEMA_VERSION:
        raise MemoryEngineMigrationError("不支持的 memory migration 收据版本")
    if manifest.get("operationId") != operation_id:
        raise MemoryEngineMigrationError("operation ID 与收据不一致")
    if manifest.get("workspace") != str(workspace):
        raise MemoryEngineMigrationError("workspace 与迁移收据不一致")
    if manifest.get("configPath") != str(config_path):
        raise MemoryEngineMigrationError("config path 与迁移收据不一致")
    return manifest


def _load_json(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryEngineMigrationError(f"无法读取迁移收据: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MemoryEngineMigrationError(f"迁移收据必须是 JSON object: {path}")
    return cast(dict[str, object], payload)


@contextmanager
def _workspace_maintenance_locks(workspace: Path) -> Generator[None, None, None]:
    """Prove that neither supervisor nor runtime currently owns the workspace."""

    streams: list[TextIO] = []
    try:
        for name in (".supervisor.lock", ".instance.lock"):
            path = workspace / name
            stream = _open_regular_lock_file(path)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise MemoryEngineMigrationError(
                    f"workspace 仍在运行，未执行正式迁移: {path}"
                ) from exc
            streams.append(stream)
        yield
    finally:
        for stream in reversed(streams):
            try:
                if os.name == "nt":
                    import msvcrt

                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()


def _open_regular_lock_file(path: Path) -> TextIO:
    """Open a workspace lock without following a symlink or special file."""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise MemoryEngineMigrationError("当前平台不支持安全打开 workspace 维护锁")
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | no_follow, 0o600)
    except OSError as exc:
        raise MemoryEngineMigrationError(
            f"workspace 维护锁不是安全的普通文件: {path}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise MemoryEngineMigrationError(f"workspace 维护锁必须是普通文件: {path}")
        return os.fdopen(descriptor, "a+", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


def _atomic_publish_from(source: Path, target: Path, *, mode: int) -> None:
    if not source.is_file() or source.is_symlink():
        raise MemoryEngineMigrationError(f"发布源不是普通文件: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".candidate", dir=target.parent
    )
    temporary = Path(raw_temporary)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output:
            shutil.copyfileobj(input_stream, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        if _sha256_file(temporary) != _sha256_file(source):
            raise MemoryEngineMigrationError(f"staging copy hash 不匹配: {target}")
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(path: Path, payload: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(raw_temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    encoded = (
        json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    _atomic_write_bytes(path, encoded, 0o600)


def _assert_sqlite_integrity(path: Path) -> None:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        _load_known_sqlite_extensions(db)
        _assert_sqlite_integrity_connection(db, path)


def _assert_sqlite_integrity_connection(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    if rows != [("ok",)]:
        raise MemoryEngineMigrationError(
            f"SQLite integrity_check 失败: {path}: {rows[:3]}"
        )


def _load_known_sqlite_extensions(connection: sqlite3.Connection) -> None:
    """Load only the repository-owned vec module needed by Memory2 backups."""

    needs_vec = connection.execute("""
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND sql LIKE '%USING vec0(%'
        LIMIT 1
        """).fetchone()
    if needs_vec is None:
        return
    try:
        import sqlite_vec
    except ImportError as exc:
        raise MemoryEngineMigrationError(
            "数据库包含 sqlite-vec 表，但当前运行环境没有 sqlite_vec，无法验证备份"
        ) from exc
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
    finally:
        connection.enable_load_extension(False)


def _require_columns(
    connection: sqlite3.Connection,
    table: str,
    required: set[str],
) -> None:
    columns = {
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')
    }
    missing = required - columns
    if missing:
        raise MemoryEngineMigrationError(f"{table} 缺少必要列: {sorted(missing)}")


def _assert_inside_workspace(workspace: Path, path: Path) -> None:
    if not path.resolve(strict=False).is_relative_to(workspace.resolve(strict=False)):
        raise MemoryEngineMigrationError(f"目标路径越出 workspace: {path}")
    _reject_existing_symlinks(workspace, path.parent)
    if path.is_symlink():
        raise MemoryEngineMigrationError(f"目标路径不能是符号链接: {path}")


def _issue_counts(audit: EmbeddingAudit) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in audit.issues:
        counts[issue.reason] = counts.get(issue.reason, 0) + 1
    return dict(sorted(counts.items()))


def _audit_payload(audit: EmbeddingAudit) -> dict[str, object]:
    return {
        "eligibleTurns": audit.eligible_turns,
        "eligibleMessages": audit.eligible_messages,
        "validMessages": audit.valid_messages,
        "excludedInterruptedTurns": audit.excluded_interrupted_turns,
        "excludedMemoryTurns": audit.excluded_memory_turns,
        "dimension": audit.dimension,
        "issueCount": len(audit.issues),
    }


def _format_audit_failure(audit: EmbeddingAudit) -> str:
    examples = [
        {
            "messageId": issue.message_id,
            "sessionKey": issue.session_key,
            "seq": issue.seq,
            "reason": issue.reason,
        }
        for issue in audit.issues[:5]
    ]
    return "Akasha embedding 严格审计失败: " + json.dumps(
        {
            "eligibleMessages": audit.eligible_messages,
            "validMessages": audit.valid_messages,
            "issueCount": len(audit.issues),
            "examples": examples,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _update_canonical_digest(digest: Any, payload: object) -> None:
    digest.update(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )
    digest.update(b"\n")


def _read_required_file(path: Path, label: str) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise MemoryEngineMigrationError(f"{label}不存在或不是普通文件: {path}")
    return path.read_bytes()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_error(exc: BaseException) -> dict[str, str]:
    text = str(exc)
    text = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)(api[_ -]?key[=:]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", text)
    # Provider exceptions can include request material; keep only a bounded class/name.
    return {"type": type(exc).__name__, "message": text[:1000]}


def migration_error_summary(exc: BaseException) -> str:
    """Return one bounded, secret-scrubbed CLI error description."""

    payload = _safe_error(exc)
    return f"{payload['type']}: {payload['message']}"


def _fault(hook: FaultHook | None, point: str) -> None:
    if hook is not None:
        hook(point)
