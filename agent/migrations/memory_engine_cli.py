"""Command-line boundary for the recoverable Akasha first-adoption workflow."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tomllib
from pathlib import Path
from typing import cast

from agent.identity import roxy_env
from agent.migrations.memory_engine import (
    APPLY_CONFIRMATION,
    REVERT_CONFIRMATION,
    SEND_HISTORY_CONFIRMATION,
    apply_memory_migration,
    assess_memory_migration,
    migration_error_summary,
    new_operation_id,
    prepare_memory_migration,
    revert_memory_migration,
    verify_memory_migration,
)
from core.net.http import SharedHttpResources


def run_memory_migration_cli(arguments: list[str]) -> int:
    parser = _parser()
    parsed = parser.parse_args(arguments)
    config_path = parsed.config.expanduser().resolve(strict=False)
    workspace = _workspace(config_path, parsed.workspace)
    operation_id = getattr(parsed, "operation_id", "")
    try:
        if parsed.action == "assess":
            result = assess_memory_migration(
                config_path=config_path,
                workspace=workspace,
                embedding_model_id=parsed.embedding_model_id,
            )
        elif parsed.action == "prepare":
            operation_id = operation_id or new_operation_id()
            result = asyncio.run(
                _prepare_with_http_resources(
                    config_path=config_path,
                    workspace=workspace,
                    operation_id=operation_id,
                    embedding_model_id=parsed.embedding_model_id,
                    confirmation=parsed.confirm,
                )
            )
        elif parsed.action == "apply":
            result = apply_memory_migration(
                config_path=config_path,
                workspace=workspace,
                operation_id=operation_id,
                confirmation=parsed.confirm,
            )
        elif parsed.action == "verify":
            result = verify_memory_migration(
                config_path=config_path,
                workspace=workspace,
                operation_id=operation_id,
            )
        elif parsed.action == "revert":
            result = revert_memory_migration(
                config_path=config_path,
                workspace=workspace,
                operation_id=operation_id,
                confirmation=parsed.confirm,
            )
        else:  # pragma: no cover - argparse owns the closed action set.
            parser.error(f"unsupported action: {parsed.action}")
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        raise SystemExit(
            f"Akasha 记忆迁移失败: {migration_error_summary(exc)}"
        ) from None

    payload = dict(result)
    if operation_id:
        payload["operationDir"] = str(
            workspace / "backups/memory-engine-migrations" / operation_id
        )
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


async def _prepare_with_http_resources(
    *,
    config_path: Path,
    workspace: Path,
    operation_id: str,
    embedding_model_id: str,
    confirmation: str,
) -> dict[str, object]:
    """Own the HTTP pool required by the standalone migration command."""

    resources = SharedHttpResources()
    try:
        return await prepare_memory_migration(
            config_path=config_path,
            workspace=workspace,
            operation_id=operation_id,
            embedding_model_id=embedding_model_id,
            confirmation=confirmation,
            http_requester=resources.external_default,
        )
    finally:
        await resources.aclose()


def _workspace(config_path: Path, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve(strict=False)
    configured_env = roxy_env("WORKSPACE").strip()
    if configured_env:
        return Path(configured_env).expanduser().resolve(strict=False)
    try:
        with config_path.open("rb") as stream:
            payload = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit(f"无法从配置解析 workspace: {config_path}: {exc}") from exc
    runtime = payload.get("runtime")
    value = (
        cast(dict[str, object], runtime).get("workspace")
        if isinstance(runtime, dict)
        else None
    )
    if not isinstance(value, str) or not value.strip():
        raise SystemExit("请通过 --workspace 或 [runtime].workspace 指定 workspace")
    return Path(os.path.expandvars(value)).expanduser().resolve(strict=False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python main.py memory-migrate",
        description=(
            "把已有 workspace 可恢复地切换到 Akasha。prepare 只写隔离副本；"
            "apply/revert 要求 runtime 已停止。"
        ),
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    def common(name: str, help_text: str) -> argparse.ArgumentParser:
        item = subparsers.add_parser(name, help=help_text)
        item.add_argument("--config", type=Path, default=Path("config.toml"))
        item.add_argument("--workspace", type=Path)
        return item

    assess = common("assess", "只读盘点历史和向量缺口")
    assess.add_argument("--embedding-model-id", default="")

    prepare = common("prepare", "在隔离快照补向量并构建候选双库")
    prepare.add_argument("--operation-id", default="")
    prepare.add_argument("--embedding-model-id", default="")
    prepare.add_argument(
        "--confirm",
        required=True,
        help=f"必须精确填写 {SEND_HISTORY_CONFIRMATION}",
    )

    apply = common("apply", "停机、持锁并原子提交候选")
    apply.add_argument("--operation-id", required=True)
    apply.add_argument(
        "--confirm", required=True, help=f"必须精确填写 {APPLY_CONFIRMATION}"
    )

    verify = common("verify", "验证已提交的正式 Akasha 读取路径")
    verify.add_argument("--operation-id", required=True)

    revert = common("revert", "停机恢复切换前配置和 sidecar")
    revert.add_argument("--operation-id", required=True)
    revert.add_argument(
        "--confirm", required=True, help=f"必须精确填写 {REVERT_CONFIRMATION}"
    )
    return parser
