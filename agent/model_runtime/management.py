"""模型管理的 Core owner；插件只消费脱敏状态和显式命令。"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, TypeAdapter

from agent.config_models import ModelRuntimeConfig
from agent.model_runtime.auth.codex import CODEX_API_BASE, CodexAuthDriver
from agent.model_runtime.auth.store import Credential, CredentialStore
from agent.model_runtime.catalog.codex import CodexModelCatalog
from agent.model_runtime.catalog.litellm_registry import resolve_catalog_capabilities
from agent.model_runtime.catalog.opencode_go import OpenCodeGoModelCatalog
from agent.model_runtime.errors import AuthenticationError, ModelRuntimeError
from agent.model_runtime.registry import ModelRegistry
from agent.model_runtime.store import (
    ModelRegistryStore,
    _INSERT_CONNECTION,
    _INSERT_MODEL,
    _normalize_runtime,
)
from agent.model_runtime.types import ModelRequest

MODEL_MANAGEMENT_SERVICE = "models.management.v1"


class ModelManagementError(ValueError):
    """可向用户展示、不会包含上游响应或凭据的错误。"""


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    expected_revision: int = Field(ge=0)
    model_id: str = Field(default="", max_length=128)
    replacement_id: str = Field(default="", max_length=128)
    enabled: bool = True
    connection_id: str = Field(default="", max_length=128)
    source_name: str = Field(default="", max_length=80)
    base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=8192, repr=False)
    model: str = Field(default="", max_length=200)
    context_window: int = Field(default=0, ge=0, le=100_000_000)
    max_output_tokens: int = Field(default=0, ge=0, le=100_000_000)
    input_modalities: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"]
    )
    reasoning_effort: str = Field(default="", max_length=32)


class ModelManagementStore(ModelRegistryStore):
    def state(self) -> dict[str, object]:
        """在一个读事务内投影全部模型，包括已停用项。"""
        with self._connect(read_only=True) as db:
            db.execute("BEGIN")
            revision = int(
                db.execute("SELECT revision FROM model_registry_meta").fetchone()[0]
            )
            connections = [
                dict(row)
                for row in db.execute(
                    "SELECT id, name, provider, base_url, enabled, auth_id, "
                    "CASE WHEN auth_payload != '' THEN 1 ELSE 0 END AS has_credential "
                    "FROM model_connections ORDER BY name, id"
                )
            ]
            models = [
                dict(row)
                for row in db.execute(
                    "SELECT id, connection_id, model, enabled, context_window, max_output_tokens, "
                    "reasoning_effort, input_modalities FROM model_definitions ORDER BY created_at, id"
                )
            ]
            roles = {
                str(row[0]): str(row[1])
                for row in db.execute("SELECT role, model_id FROM model_role_bindings")
            }
        return {
            "revision": revision,
            "connections": connections,
            "models": models,
            "roles": roles,
        }

    def mutate(
        self, method: str, command: Command, raw: dict[str, object] | None = None
    ) -> int:
        """以 revision 比较和单事务发布一个模型变更。"""
        # 1. 备份包含凭据，沿用 Core 的私有权限和 SQLite backup。
        self.backup_to(self.path.parent / "model-backups" / f"{uuid4().hex}.sqlite3")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            revision = int(
                db.execute("SELECT revision FROM model_registry_meta").fetchone()[0]
            )
            if revision != command.expected_revision:
                raise ModelManagementError("模型设置已变化，请刷新列表后重试")
            if method == "add":
                assert raw is not None
                source, model = _normalize_runtime(f"model-{uuid4().hex}", raw, {})
                exists = db.execute(
                    "SELECT 1 FROM model_connections WHERE id = ?", (source[0],)
                ).fetchone()
                if command.connection_id:
                    if not exists:
                        raise ModelManagementError("连接已不存在，请刷新")
                else:
                    credential = Credential(
                        driver="api_key", access_token=command.api_key.strip()
                    )
                    kind, payload = CredentialStore.encode(credential)
                    db.execute(_INSERT_CONNECTION, (*source, kind, payload))
                db.execute(_INSERT_MODEL, model)
                if not db.execute(
                    "SELECT 1 FROM model_role_bindings WHERE role = 'default'"
                ).fetchone():
                    for role in ("default", "fast", "agent", "vision"):
                        db.execute(
                            "INSERT INTO model_role_bindings(role, model_id) VALUES (?, ?)",
                            (role, model[0]),
                        )
            else:
                model = db.execute(
                    "SELECT * FROM model_definitions WHERE id = ?", (command.model_id,)
                ).fetchone()
                if model is None:
                    raise ModelManagementError("模型已不存在，请刷新")
                if method == "default":
                    self._require_enabled(db, command.model_id)
                    db.execute(
                        "UPDATE model_role_bindings SET model_id = ?, reasoning_effort = '' WHERE role = 'default'",
                        (command.model_id,),
                    )
                elif method in {"delete", "enable"}:
                    if method == "delete" or not command.enabled:
                        roles = db.execute(
                            "SELECT role FROM model_role_bindings WHERE model_id = ?",
                            (command.model_id,),
                        ).fetchall()
                        if roles:
                            if (
                                not command.replacement_id
                                or command.replacement_id == command.model_id
                            ):
                                raise ModelManagementError(
                                    "此模型被角色使用，请先指定替代模型"
                                )
                            replacement = self._require_enabled(
                                db, command.replacement_id
                            )
                            if (
                                any(row[0] == "vision" for row in roles)
                                and '"image"' in model["input_modalities"]
                                and '"image"' not in replacement["input_modalities"]
                            ):
                                raise ModelManagementError(
                                    "视觉角色的替代模型必须支持图片"
                                )
                            db.execute(
                                "UPDATE model_role_bindings SET model_id = ?, reasoning_effort = '' WHERE model_id = ?",
                                (command.replacement_id, command.model_id),
                            )
                    if method == "delete":
                        db.execute(
                            "DELETE FROM model_definitions WHERE id = ?",
                            (command.model_id,),
                        )
                    else:
                        if command.enabled:
                            connection = db.execute(
                                "SELECT enabled FROM model_connections WHERE id = ?",
                                (model["connection_id"],),
                            ).fetchone()
                            if not connection or not connection[0]:
                                raise ModelManagementError("所属连接已停用")
                        db.execute(
                            "UPDATE model_definitions SET enabled = ? WHERE id = ?",
                            (int(command.enabled), command.model_id),
                        )
                else:
                    raise ModelManagementError("未知模型操作")
            db.execute("UPDATE model_registry_meta SET revision = revision + 1")
            db.commit()
            return revision + 1

    @staticmethod
    def _require_enabled(db: sqlite3.Connection, model_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT m.* FROM model_definitions m JOIN model_connections c ON c.id=m.connection_id WHERE m.id=? AND m.enabled=1 AND c.enabled=1",
            (model_id,),
        ).fetchone()
        if row is None:
            raise ModelManagementError("请选择已启用的模型")
        return row


class ModelManagementService:
    def __init__(self, workspace: Path, registry: ModelRegistry) -> None:
        self.store = ModelManagementStore.for_workspace(workspace)
        self.credentials = CredentialStore.for_workspace(workspace)
        self.registry = registry
        self._lock = asyncio.Lock()

    def state(self) -> dict[str, object]:
        return self.store.state()

    async def execute(
        self, method: str, payload: dict[str, object]
    ) -> dict[str, object]:
        """只接受显式 action；参数错误不回显用户输入。"""
        try:
            command = Command.model_validate(payload)
        except ValidationError:
            raise ModelManagementError("模型参数格式无效，请检查必填项和数值") from None
        if method not in {"add", "delete", "enable", "default", "discover", "probe"}:
            raise ModelManagementError("未知模型操作")
        try:
            async with self._lock:
                if self.store.revision() != command.expected_revision:
                    raise ModelManagementError("模型设置已变化，请刷新列表后重试")
                if method == "discover":
                    return {"models": await self._discover(command)}
                if method == "probe":
                    await self._probe(command)
                    return {"message": "WSL／后端模型请求成功"}
                raw = await self._candidate(command) if method == "add" else None
                revision = self.store.mutate(method, command, raw)
                await self.registry.refresh()
                return {"revision": revision, "state": self.state()}
        except sqlite3.IntegrityError:
            raise ModelManagementError("该连接下已有此模型，或模型仍被引用") from None
        except ModelManagementError:
            raise
        except AuthenticationError:
            raise ModelManagementError(
                "认证失败，请检查 API Key 或重新登录连接"
            ) from None
        except (httpx.TimeoutException, TimeoutError):
            raise ModelManagementError("后端连接超时，请检查 WSL 网络和代理") from None
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            message = {
                401: "认证失败，请检查 API Key",
                403: "连接或模型无访问权限",
                404: "接口或模型不存在",
                429: "额度不足或请求受限",
            }.get(status, f"模型服务请求失败（HTTP {status}）")
            raise ModelManagementError(message) from None
        except httpx.HTTPError:
            raise ModelManagementError(
                "后端连接失败，请检查 WSL 网络、代理和服务地址"
            ) from None
        except ModelRuntimeError:
            raise ModelManagementError(
                "模型请求失败，请检查连接、模型权限和额度"
            ) from None

    def _connection(self, command: Command) -> dict[str, object]:
        if command.connection_id:
            connections = cast(list[dict[str, object]], self.state()["connections"])
            connection = next(
                (row for row in connections if row["id"] == command.connection_id), None
            )
            if connection is None or not connection["enabled"]:
                raise ModelManagementError("连接不存在或已停用")
            if command.api_key or command.base_url or command.source_name:
                raise ModelManagementError("复用连接时不能修改其凭据和地址")
            return connection
        parsed = urlsplit(command.base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ModelManagementError(
                "请填写有效的 HTTP(S) Base URL，不含凭据、查询或片段"
            )
        if not command.source_name.strip() or not command.api_key.strip():
            raise ModelManagementError("新连接需要名称和 API Key")
        source_id = f"connection-{uuid4().hex}"
        return {
            "id": source_id,
            "name": command.source_name.strip(),
            "provider": "openai",
            "auth_id": source_id,
            "base_url": command.base_url.strip().rstrip("/"),
        }

    def _key(self, connection: dict[str, object], command: Command) -> str:
        if command.connection_id:
            return self.credentials.get(str(connection["auth_id"])).access_token
        return command.api_key.strip()

    async def _discover(self, command: Command) -> list[dict[str, object]]:
        connection = self._connection(command)
        provider = str(connection["provider"])
        if provider == "codex":
            models = await CodexModelCatalog(
                CodexAuthDriver(self.credentials, str(connection["auth_id"])),
                base_url=str(connection["base_url"]) or CODEX_API_BASE,
            ).list_models()
            return [
                {
                    "id": item.slug,
                    "context_window": item.capabilities.context_window,
                    "max_output_tokens": item.capabilities.max_output_tokens,
                    "input_modalities": list(item.capabilities.input_modalities),
                }
                for item in models
            ]
        if provider == "opencode-go":
            models = await OpenCodeGoModelCatalog(
                self._key(connection, command), base_url=str(connection["base_url"])
            ).list_models()
            return [{"id": item.slug} for item in models]
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            response = await client.get(
                f"{str(connection['base_url']).rstrip('/')}/models",
                headers={"Authorization": f"Bearer {self._key(connection, command)}"},
            )
            response.raise_for_status()
            try:
                items = response.json()["data"]
                if not isinstance(items, list) or len(items) > 5000:
                    raise ValueError
                result = [
                    {"id": item["id"]}
                    for item in items
                    if isinstance(item, dict)
                    and isinstance(item.get("id"), str)
                    and 0 < len(item["id"]) <= 200
                ]
            except (ValueError, KeyError, TypeError):
                raise ModelManagementError(
                    "模型目录格式无效，可手动填写模型 ID"
                ) from None
        return result

    async def _candidate(self, command: Command) -> dict[str, object]:
        connection = self._connection(command)
        if not command.model.strip():
            raise ModelManagementError("请填写或选择模型 ID")
        provider = str(connection["provider"])
        raw: dict[str, object] = {
            "provider": provider,
            "model": command.model.strip(),
            "source_id": connection["id"],
            "source_name": connection["name"],
            "auth": connection["auth_id"],
            "base_url": connection["base_url"],
            "context_window": command.context_window,
            "max_output_tokens": command.max_output_tokens,
            "input_modalities": command.input_modalities,
            "reasoning_effort": command.reasoning_effort,
            "capability_source": "explicit",
        }
        if provider == "codex":
            entries = await CodexModelCatalog(
                CodexAuthDriver(self.credentials, str(connection["auth_id"])),
                base_url=str(connection["base_url"]) or CODEX_API_BASE,
            ).list_models()
            entry = next(
                (item for item in entries if item.slug == command.model.strip()), None
            )
            if entry is None:
                raise ModelManagementError("该登录连接没有此模型")
            caps = entry.capabilities
            raw.update(
                context_window=caps.context_window,
                max_output_tokens=caps.max_output_tokens,
                input_modalities=list(caps.input_modalities),
                supported_reasoning_efforts=list(caps.supported_reasoning_efforts),
                capability_source="provider_catalog",
                use_responses_lite=caps.use_responses_lite,
                supports_parallel_tool_calls=caps.supports_parallel_tool_calls,
            )
        else:
            caps = resolve_catalog_capabilities(
                provider, command.model.strip(), base_url=str(connection["base_url"])
            )
            if caps is not None:
                raw.update(
                    context_window=command.context_window or caps.context_window,
                    max_output_tokens=command.max_output_tokens,
                    input_modalities=list(caps.input_modalities),
                    supported_reasoning_efforts=list(caps.supported_reasoning_efforts),
                    capability_source=caps.source,
                    supports_parallel_tool_calls=caps.supports_parallel_tool_calls,
                )
        if not raw["context_window"]:
            raise ModelManagementError("未知模型需要填写上下文窗口大小")
        try:
            TypeAdapter(ModelRuntimeConfig).validate_python(
                {"runtime_id": "candidate", **raw}
            )
        except (ValueError, TypeError):
            raise ModelManagementError(
                "模型能力配置无效，请检查上下文、输出上限及思考强度"
            ) from None
        return raw

    async def _probe(self, command: Command) -> None:
        """用与正式推理相同的 transport 发起一个短请求。"""
        from agent.provider import ChatCompletionsRuntime
        from agent.model_runtime.transports.responses import CodexResponsesTransport

        raw = await self._candidate(command)
        request = ModelRequest(
            messages=[{"role": "user", "content": "Reply OK."}],
            tools=[],
            model=str(raw["model"]),
            max_output_tokens=32,
        )
        async with asyncio.timeout(25):
            if raw["provider"] == "codex":
                backend = CodexResponsesTransport(
                    CodexAuthDriver(self.credentials, str(raw["auth"])),
                    runtime_id="model-manager-probe",
                    base_url=str(raw["base_url"]) or CODEX_API_BASE,
                )
                await backend.send(request)
            else:
                backend = ChatCompletionsRuntime(
                    self._key(self._connection(command), command),
                    base_url=str(raw["base_url"]),
                    max_retries=0,
                    payload_snapshot_enabled=False,
                    provider_name=str(raw["provider"]),
                )
                try:
                    await backend.send(request)
                finally:
                    await backend._client.close()
