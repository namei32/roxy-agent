"""Experiment policy, preflight validation, and reproducibility metadata."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

from agent.config_models import Config
from plugins.default_memory.config import DefaultMemoryConfig

PROTOCOL_VERSION = "roxy-longmemeval-s-v2"
_IMPLEMENTATION_ROOTS = (
    "agent",
    "bootstrap",
    "bus",
    "core",
    "infra",
    "memory2",
    "plugins/default_memory",
    "session",
    "eval/longmemeval",
)
_DEPENDENCY_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "uv.lock",
)


@dataclass(frozen=True)
class BenchmarkSettings:
    variant: str = "role-aware"
    require_full_dataset: bool = False
    strict_role_policy: bool = False
    expected_dataset_sha256: str = ""
    expected_model: str = ""
    expected_embedding_model: str = ""
    qa_effort: str = "max"
    consolidation_effort: str = "medium"
    query_effort: str = "medium"
    history_gate_effort: str = "low"
    compaction_effort: str = "none"
    consolidation_sessions_per_batch: int = 16
    post_response_invalidation: bool = False
    judge_effort: str = "xhigh"
    judge_max_output_tokens: int = 25_000
    judge_audit_effort: str = "max"
    judge_audit_size: int = 50


def _as_mapping(value: object, *, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a TOML table")
    return value


def _as_bool(value: object, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _as_positive_int(value: object, *, field: str, default: int) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _as_optional_sha256(value: object, *, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a SHA-256 string")
    normalized = value.strip().lower()
    if normalized and (
        len(normalized) != 64
        or any(character not in "0123456789abcdef" for character in normalized)
    ):
        raise ValueError(f"{field} must be 64 lowercase hexadecimal characters")
    return normalized


def load_benchmark_settings(path: Path | str) -> BenchmarkSettings:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    benchmark = _as_mapping(raw.get("benchmark"), field="benchmark")
    section = _as_mapping(benchmark.get("longmemeval"), field="benchmark.longmemeval")
    return BenchmarkSettings(
        variant=str(section.get("variant") or "role-aware").strip(),
        require_full_dataset=_as_bool(
            section.get("require_full_dataset"),
            field="benchmark.longmemeval.require_full_dataset",
            default=False,
        ),
        strict_role_policy=_as_bool(
            section.get("strict_role_policy"),
            field="benchmark.longmemeval.strict_role_policy",
            default=False,
        ),
        expected_dataset_sha256=_as_optional_sha256(
            section.get("expected_dataset_sha256"),
            field="benchmark.longmemeval.expected_dataset_sha256",
        ),
        expected_model=str(section.get("expected_model") or "").strip(),
        expected_embedding_model=str(
            section.get("expected_embedding_model") or ""
        ).strip(),
        qa_effort=str(section.get("qa_effort") or "max").strip(),
        consolidation_effort=str(
            section.get("consolidation_effort") or "medium"
        ).strip(),
        query_effort=str(section.get("query_effort") or "medium").strip(),
        history_gate_effort=str(section.get("history_gate_effort") or "low").strip(),
        compaction_effort=str(section.get("compaction_effort") or "none").strip(),
        consolidation_sessions_per_batch=_as_positive_int(
            section.get("consolidation_sessions_per_batch"),
            field=("benchmark.longmemeval.consolidation_sessions_per_batch"),
            default=16,
        ),
        post_response_invalidation=_as_bool(
            section.get("post_response_invalidation"),
            field="benchmark.longmemeval.post_response_invalidation",
            default=False,
        ),
        judge_effort=str(section.get("judge_effort") or "xhigh").strip(),
        judge_max_output_tokens=_as_positive_int(
            section.get("judge_max_output_tokens"),
            field="benchmark.longmemeval.judge_max_output_tokens",
            default=25_000,
        ),
        judge_audit_effort=str(section.get("judge_audit_effort") or "max").strip(),
        judge_audit_size=_as_positive_int(
            section.get("judge_audit_size"),
            field="benchmark.longmemeval.judge_audit_size",
            default=50,
        ),
    )


def validate_role_policy(
    config: Config,
    memory_config: DefaultMemoryConfig,
    settings: BenchmarkSettings,
) -> None:
    """Fail before spending API budget when the declared formal policy is not active."""

    if not settings.strict_role_policy:
        return
    runtime_ids = {
        "default": config.runtime_id,
        "fast": config.fast_runtime_id or config.runtime_id,
        "agent": config.agent_runtime_id or config.runtime_id,
    }
    expected_efforts = {
        "default": settings.consolidation_effort,
        "fast": settings.query_effort,
        "agent": settings.qa_effort,
    }
    errors: list[str] = []
    for role, runtime_id in runtime_ids.items():
        runtime = config.model_runtimes[runtime_id]
        if settings.expected_model and runtime.model != settings.expected_model:
            errors.append(
                f"{role} model={runtime.model!r}, expected {settings.expected_model!r}"
            )
        if runtime.reasoning_effort != expected_efforts[role]:
            errors.append(
                f"{role} effort={runtime.reasoning_effort!r}, "
                f"expected {expected_efforts[role]!r}"
            )
    if config.memory.consolidation_reasoning_effort != settings.consolidation_effort:
        errors.append(
            "memory consolidation effort="
            f"{config.memory.consolidation_reasoning_effort!r}, "
            f"expected {settings.consolidation_effort!r}"
        )
    if config.context_compaction.reasoning_effort != settings.compaction_effort:
        errors.append(
            f"context compaction effort={config.context_compaction.reasoning_effort!r}, "
            f"expected {settings.compaction_effort!r}"
        )
    if settings.expected_embedding_model and (
        config.memory.embedding.model != settings.expected_embedding_model
    ):
        errors.append(
            f"embedding model={config.memory.embedding.model!r}, "
            f"expected {settings.expected_embedding_model!r}"
        )
    if not memory_config.gate.enabled:
        errors.append("history gate is disabled")
    if memory_config.gate.reasoning_effort != settings.history_gate_effort:
        errors.append(
            f"history gate effort={memory_config.gate.reasoning_effort!r}, "
            f"expected {settings.history_gate_effort!r}"
        )
    if not memory_config.hyde.enabled:
        errors.append("HyDE is disabled")
    if not memory_config.query_rewrite.enabled:
        errors.append("query rewrite is disabled")
    if memory_config.query_rewrite.reasoning_effort != settings.query_effort:
        errors.append(
            "query rewrite effort="
            f"{memory_config.query_rewrite.reasoning_effort!r}, "
            f"expected {settings.query_effort!r}"
        )
    if memory_config.hyde.reasoning_effort != settings.query_effort:
        errors.append(
            f"HyDE effort={memory_config.hyde.reasoning_effort!r}, "
            f"expected {settings.query_effort!r}"
        )

    required_efforts = {
        "QA": ("agent", settings.qa_effort),
        "consolidation": ("default", settings.consolidation_effort),
        "history gate": ("fast", settings.history_gate_effort),
        "query rewrite": ("fast", settings.query_effort),
        "HyDE": ("fast", settings.query_effort),
        "context compaction": ("default", settings.compaction_effort),
        "Judge": ("default", settings.judge_effort),
        "Judge audit": ("default", settings.judge_audit_effort),
    }
    for stage, (role, effort) in required_efforts.items():
        runtime = config.model_runtimes[runtime_ids[role]]
        supported = runtime.supported_reasoning_efforts
        if effort and supported and effort not in supported:
            errors.append(
                f"{stage} effort={effort!r} is not declared by "
                f"runtime {runtime.runtime_id!r}; supported={list(supported)!r}"
            )
    if errors:
        raise ValueError("LongMemEval role policy mismatch:\n- " + "\n- ".join(errors))


def validate_runtime_credentials(
    config: Config,
    credential_workspace: Path | None,
) -> None:
    """Resolve model and embedding credentials before any paid model call."""

    credential_ids = sorted(
        {
            runtime.auth
            for runtime in config.model_runtimes.values()
            if runtime.provider == "codex" and runtime.auth
        }
    )
    if credential_ids:
        if credential_workspace is None:
            raise ValueError(
                "Codex runtimes require --credential-workspace so isolated question "
                "workspaces can share one credential owner"
            )

        from agent.model_runtime.auth.store import CredentialStore

        credential_store = CredentialStore.for_workspace(credential_workspace)
        for credential_id in credential_ids:
            credential_store.get(credential_id)

    embedding_key = config.memory.embedding.api_key.strip()
    if config.memory.enabled and (
        not embedding_key or re.fullmatch(r"\$\{\w+\}", embedding_key)
    ):
        raise ValueError(
            "memory embedding credential is missing or unresolved; set "
            "BENCH_EMBED_API_KEY before preflight"
        )


def runtime_policy_snapshot(
    config: Config,
    memory_config: DefaultMemoryConfig,
    settings: BenchmarkSettings,
) -> dict[str, object]:
    runtime_ids = {
        "default": config.runtime_id,
        "fast": config.fast_runtime_id or config.runtime_id,
        "agent": config.agent_runtime_id or config.runtime_id,
    }
    runtimes = {
        role: {
            "runtime_id": runtime_id,
            "provider": config.model_runtimes[runtime_id].provider,
            "model": config.model_runtimes[runtime_id].model,
            "reasoning_effort": config.model_runtimes[runtime_id].reasoning_effort,
            "max_output_tokens": config.model_runtimes[runtime_id].max_output_tokens,
        }
        for role, runtime_id in runtime_ids.items()
    }
    return {
        "runtimes": runtimes,
        "memory_consolidation": {
            "runtime_role": "default",
            "reasoning_effort": config.memory.consolidation_reasoning_effort,
        },
        "history_gate": {
            "runtime_role": "fast",
            "enabled": memory_config.gate.enabled,
            "reasoning_effort": memory_config.gate.reasoning_effort,
            "timeout_ms": memory_config.gate.llm_timeout_ms,
        },
        "query_rewrite": {
            "runtime_role": "fast",
            "enabled": memory_config.query_rewrite.enabled,
            "reasoning_effort": memory_config.query_rewrite.reasoning_effort,
            "timeout_ms": memory_config.query_rewrite.timeout_ms,
        },
        "hyde": {
            "runtime_role": "fast",
            "enabled": memory_config.hyde.enabled,
            "reasoning_effort": memory_config.hyde.reasoning_effort,
            "timeout_ms": memory_config.hyde.timeout_ms,
        },
        "context_compaction": {
            "runtime_role": "default",
            "reasoning_effort": config.context_compaction.reasoning_effort,
        },
        "ingestion": {
            "pipeline": (
                "session_store->markdown_consolidation->"
                "consolidation_event->semantic_memory->embedding"
            ),
            "sessions_per_batch": settings.consolidation_sessions_per_batch,
            "post_response_invalidation": settings.post_response_invalidation,
        },
        "judge": {
            "runtime_role": "default",
            "reasoning_effort": settings.judge_effort,
            "max_output_tokens": settings.judge_max_output_tokens,
        },
        "embedding": {
            "model": config.memory.embedding.model,
            "output_dimensionality": config.memory.embedding.output_dimensionality,
        },
    }


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _implementation_sha256(repo_root: Path) -> tuple[str, int]:
    """Hash executable benchmark/runtime sources so stale caches cannot cross code."""

    paths: list[Path] = []
    for relative_root in _IMPLEMENTATION_ROOTS:
        root = repo_root / relative_root
        if root.is_file():
            paths.append(root)
        elif root.is_dir():
            paths.extend(
                path for path in root.rglob("*.py") if "__pycache__" not in path.parts
            )
    digest = hashlib.sha256()
    unique_paths = sorted(
        set(paths), key=lambda path: path.relative_to(repo_root).as_posix()
    )
    for path in unique_paths:
        relative = path.relative_to(repo_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest(), len(unique_paths)


def _dependency_sha256(repo_root: Path) -> str:
    digest = hashlib.sha256()
    for relative in _DEPENDENCY_FILES:
        path = repo_root / relative
        if not path.is_file():
            continue
        encoded_path = relative.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(encoded_path).to_bytes(8, "big"))
        digest.update(encoded_path)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def build_experiment_manifest(
    *,
    config_path: Path,
    data_path: Path,
    selected_question_ids: list[str],
    config: Config,
    memory_config: DefaultMemoryConfig,
    settings: BenchmarkSettings,
    benchmark_prompt: str,
    question_timeout_s: float = 600.0,
) -> dict[str, object]:
    if question_timeout_s <= 0:
        raise ValueError("question_timeout_s must be positive")
    policy = runtime_policy_snapshot(config, memory_config, settings)
    repo_root = Path(__file__).resolve().parents[2]
    implementation_sha256, implementation_file_count = _implementation_sha256(repo_root)
    base = {
        "protocol_version": PROTOCOL_VERSION,
        "variant": settings.variant,
        "implementation_sha256": implementation_sha256,
        "implementation_file_count": implementation_file_count,
        "dependency_sha256": _dependency_sha256(repo_root),
        "config_sha256": _sha256_bytes(config_path.read_bytes()),
        "dataset_sha256": _sha256_bytes(data_path.read_bytes()),
        "benchmark_prompt_sha256": _sha256_bytes(benchmark_prompt.encode("utf-8")),
        "question_timeout_s": float(question_timeout_s),
        "settings": asdict(settings),
        "model_policy": policy,
    }
    artifact_fingerprint = _sha256_bytes(
        json.dumps(base, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    selection_sha256 = _sha256_bytes(
        json.dumps(selected_question_ids, ensure_ascii=False).encode("utf-8")
    )
    run_fingerprint = _sha256_bytes(
        f"{artifact_fingerprint}:{selection_sha256}".encode("utf-8")
    )
    return {
        **base,
        "artifact_fingerprint": artifact_fingerprint,
        "selection_sha256": selection_sha256,
        "run_fingerprint": run_fingerprint,
        "selected_count": len(selected_question_ids),
    }
