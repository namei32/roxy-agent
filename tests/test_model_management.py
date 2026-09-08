from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from agent.config_models import Config
from agent.model_runtime.auth.store import CredentialStore
from agent.model_runtime.management import (
    Command,
    ModelManagementError,
    ModelManagementService,
)
from agent.model_runtime.registry import ModelGeneration, ModelRegistry
from agent.model_runtime.store import ModelRegistryStore


@pytest.fixture
def service(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[agent]\nsystem_prompt="test"\n')
    store = ModelRegistryStore.for_workspace(tmp_path)
    store.replace_from_llm_config(
        {
            "main": "first",
            "runtimes": {
                "first": {
                    "provider": "openai",
                    "model": "first-model",
                    "context_window": 10000,
                    "base_url": "https://example.test/v1",
                }
            },
        }
    )

    def build(config, generation_id):
        return ModelGeneration(
            generation_id,
            "test",
            config.model_runtimes,
            {key: object() for key in config.model_runtimes},
            {
                "default": config.runtime_id,
                "fast": config.fast_runtime_id,
                "agent": config.agent_runtime_id,
                "vision": config.vl_runtime_id,
            },
            registry_revision=config.model_registry_revision,
        )

    registry = ModelRegistry(Config.load(config_path, workspace=tmp_path), build)
    return ModelManagementService(tmp_path, registry)


def new_model(service, **extra):
    return {
        "expected_revision": service.store.revision(),
        "source_name": "Private",
        "base_url": "https://example.test/v1",
        "api_key": "secret-never-return",
        "model": "new-unknown-model",
        "context_window": 64000,
        **extra,
    }


@pytest.mark.asyncio
async def test_add_appears_in_runtime_and_secret_stays_private(service):
    payload = new_model(service)
    result = await service.execute("add", payload)
    model = next(
        row for row in result["state"]["models"] if row["model"] == payload["model"]
    )
    assert service.registry.current.runtimes[model["id"]].model == payload["model"]
    assert "secret-never-return" not in json.dumps(result)
    assert (
        CredentialStore(service.store.path).api_key(model["connection_id"])
        == "secret-never-return"
    )
    assert service.store.path.stat().st_mode & 0o777 == 0o600
    backups = list((service.store.path.parent / "model-backups").glob("*.sqlite3"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_existing_connection_reuse_does_not_replace_credentials(service):
    result = await service.execute("add", new_model(service))
    connection = next(
        row for row in result["state"]["connections"] if row["name"] == "Private"
    )
    result = await service.execute(
        "add",
        {
            "expected_revision": 2,
            "connection_id": connection["id"],
            "model": "another-model",
            "context_window": 32000,
        },
    )
    assert len(result["state"]["connections"]) == 2
    assert (
        CredentialStore(service.store.path).api_key(connection["id"])
        == "secret-never-return"
    )
    with pytest.raises(ModelManagementError, match="不能修改"):
        await service.execute(
            "add",
            {
                "expected_revision": 3,
                "connection_id": connection["id"],
                "api_key": "oops",
                "model": "third",
            },
        )


@pytest.mark.asyncio
async def test_delete_roles_requires_atomic_replacement_and_active_lease_survives(
    service,
):
    result = await service.execute("add", new_model(service))
    added = next(row for row in result["state"]["models"] if row["id"] != "first")
    async with service.registry.execution_scope("first") as binding:
        before = service.store.state()
        with pytest.raises(ModelManagementError, match="替代"):
            await service.execute(
                "delete", {"expected_revision": 2, "model_id": "first"}
            )
        assert service.store.state() == before
        result = await service.execute(
            "delete",
            {
                "expected_revision": 2,
                "model_id": "first",
                "replacement_id": added["id"],
            },
        )
        assert set(result["state"]["roles"].values()) == {added["id"]}
        assert binding.describe("default")["model"] == "first-model"
    with pytest.raises(ValueError, match="不存在"):
        async with service.registry.execution_scope("first"):
            pass
    assert len(result["state"]["connections"]) == 2


@pytest.mark.asyncio
async def test_enable_disable_default_and_duplicate_protection(service):
    result = await service.execute("add", new_model(service))
    model = next(row for row in result["state"]["models"] if row["id"] != "first")
    await service.execute(
        "enable", {"expected_revision": 2, "model_id": model["id"], "enabled": False}
    )
    assert model["id"] not in service.registry.current.runtimes
    assert any(row["id"] == model["id"] for row in service.state()["models"])
    with pytest.raises(ModelManagementError, match="启用"):
        await service.execute(
            "default", {"expected_revision": 3, "model_id": model["id"]}
        )
    await service.execute("enable", {"expected_revision": 3, "model_id": model["id"]})
    await service.execute("default", {"expected_revision": 4, "model_id": model["id"]})
    assert service.registry.current.role_runtime_ids["default"] == model["id"]
    with pytest.raises(ModelManagementError, match="已有"):
        await service.execute(
            "add",
            {
                "expected_revision": 5,
                "connection_id": model["connection_id"],
                "model": model["model"],
                "context_window": 64000,
            },
        )
    assert service.store.revision() == 5


@pytest.mark.asyncio
async def test_concurrent_writes_cannot_overwrite_each_other(service):
    payload = new_model(service)
    results = await asyncio.gather(
        service.execute("add", payload),
        service.execute("add", payload),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ModelManagementError) for result in results) == 1
    assert service.store.revision() == 2


@pytest.mark.asyncio
async def test_invalid_input_and_unknown_capabilities_fail_without_leaking(service):
    for patch in (
        {"context_window": 0},
        {"context_window": 2, "max_output_tokens": 4},
        {"expected_revision": True},
        {"base_url": "https://user:password@example.test/v1"},
    ):
        with pytest.raises(ModelManagementError) as error:
            await service.execute("add", new_model(service, **patch))
        assert "secret-never-return" not in str(error.value)
        assert "password" not in str(error.value)
        assert service.store.revision() == 1


@pytest.mark.asyncio
async def test_discovery_and_network_failure_are_safe(service, monkeypatch):
    original = httpx.AsyncClient

    def factory(**kwargs):
        return original(
            **kwargs,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": [{"id": "new-model"}]}
                )
            ),
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    result = await service.execute("discover", new_model(service))
    assert result == {"models": [{"id": "new-model"}]}

    def failed(request):
        raise httpx.ConnectError("secret-never-return")

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(failed)),
    )
    with pytest.raises(ModelManagementError, match="后端连接失败") as error:
        await service.execute("discover", new_model(service))
    assert "secret-never-return" not in str(error.value)
    assert service.store.revision() == 1


@pytest.mark.asyncio
async def test_chat_http_action_requires_csrf_and_catalog_observes_committed_state(
    service, tmp_path
):
    from bootstrap.chat_api import create_chat_app

    class Provider:
        async def action(self, plugin_id, revision, method, payload):
            return await service.execute(method, payload)

    async def notify():
        return None
    channel = SimpleNamespace(bind_attachment_store=lambda store: None, name="web", notify_model_catalog_changed=notify)
    app = create_chat_app(
        workspace=tmp_path,
        channel=cast(Any, channel),
        plugin_ui_provider=cast(Any, Provider()),
        model_registry=service.registry,
    )
    body = {
        "plugin_id": "model-manager@local",
        "plugin_revision": "head",
        "slot": "dashboard.main",
        "method": "add",
        "payload": new_model(service),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post("/api/chat/plugin-ui/action", json=body)
        assert denied.status_code == 403 and service.store.revision() == 1
        response = await client.post(
            "/api/chat/plugin-ui/action",
            json=body,
            headers={"origin": "http://test", "x-roxy-csrf": "1"},
        )
        assert response.status_code == 200
        catalog = (await client.get("/api/chat/models")).json()
        assert any(item["model"] == "new-unknown-model" for item in catalog["runtimes"])
