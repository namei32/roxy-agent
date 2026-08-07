from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

import pytest
import websockets

from agent.config_models import NotesBridgeConfig
from agent.config import _load_notes_bridge_config
from agent.plugins.context import PluginContext, PluginKVStore
from companion.mac_notes_bridge.executor import MacNotesExecutor
from companion.mac_notes_bridge.client import MacNotesBridgeClient
from companion.mac_notes_bridge.cli import _ssh_tunnel_program_arguments
from companion.mac_notes_bridge.store import MacNotesReceiptStore
from infra.notes_bridge.auth import request_hash, sign_message, verify_message
from infra.notes_bridge.broker import NotesBridgeBroker
from infra.notes_bridge.port import NotesBridgeOperation
from infra.notes_bridge.store import NotesBridgeAuditStore
from plugins.apple_notes.bridge import NotesMutationReceipt, NotesOperationRejected
from plugins.apple_notes.config import AppleNotesConfig

_TOKEN = "test-token-that-is-definitely-longer-than-thirty-two-characters"


class FakeWebSocket:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[dict[str, Any] | BaseException] = asyncio.Queue()
        self.outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.accepted = False
        self.closed = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict[str, Any]:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send_json(self, value: dict[str, Any]) -> None:
        await self.outgoing.put(value)

    async def close(self, *, code: int = 1000) -> None:
        del code
        self.closed = True


class ConnectedMac:
    def __init__(
        self,
        websocket: FakeWebSocket,
        task: asyncio.Task[None],
        epoch: str,
        server_sequence: int,
    ) -> None:
        self.websocket = websocket
        self.task = task
        self.epoch = epoch
        self.server_sequence = server_sequence
        self.client_sequence = 0

    async def receive(self) -> dict[str, Any]:
        message = await asyncio.wait_for(self.websocket.outgoing.get(), timeout=1)
        verify_message(message, _TOKEN)
        assert message["connection_epoch"] == self.epoch
        assert int(message["sequence"]) > self.server_sequence
        self.server_sequence = int(message["sequence"])
        return message

    async def send(self, message: dict[str, Any]) -> None:
        self.client_sequence += 1
        await self.websocket.incoming.put(
            sign_message(
                {
                    **message,
                    "bridge_id": "mac-primary",
                    "connection_epoch": self.epoch,
                    "sequence": self.client_sequence,
                },
                _TOKEN,
            )
        )

    async def disconnect(self) -> None:
        await self.websocket.incoming.put(ConnectionError("test disconnect"))
        await asyncio.wait_for(self.task, timeout=1)


def _config() -> NotesBridgeConfig:
    return NotesBridgeConfig(
        enabled=True,
        token=_TOKEN,
        heartbeat_interval_seconds=0.01,
        offline_after_seconds=1,
        proposal_timeout_seconds=0.2,
        commit_timeout_seconds=0.2,
    )


async def _connect(broker: NotesBridgeBroker) -> ConnectedMac:
    websocket = FakeWebSocket()
    task = asyncio.create_task(broker.handle_websocket(websocket))
    client_nonce = "client-nonce-that-is-long-enough"
    await websocket.incoming.put(
        {
            "type": "hello",
            "protocol": 1,
            "bridge_id": "mac-primary",
            "client_nonce": client_nonce,
        }
    )
    challenge = await asyncio.wait_for(websocket.outgoing.get(), timeout=1)
    await websocket.incoming.put(
        sign_message(
            {
                "type": "proof",
                "bridge_id": "mac-primary",
                "client_nonce": client_nonce,
                "challenge_id": challenge["challenge_id"],
                "server_nonce": challenge["server_nonce"],
            },
            _TOKEN,
        )
    )
    ready = await asyncio.wait_for(websocket.outgoing.get(), timeout=1)
    verify_message(ready, _TOKEN)
    connected = ConnectedMac(
        websocket,
        task,
        str(ready["connection_epoch"]),
        int(ready["sequence"]),
    )
    await connected.send(
        {
            "type": "heartbeat",
            "notes_ready": True,
            "permissions_ready": True,
        }
    )
    await asyncio.sleep(0)
    assert broker.is_online()
    return connected


def _operation(
    operation_id: str = "operation-1", *, body: str = "secret-body"
) -> NotesBridgeOperation:
    return NotesBridgeOperation(
        operation_id=operation_id,
        action="create",
        account="default",
        folder="Akashic",
        create_folder_if_missing=True,
        document_key="interview:batch-1",
        title="Interview",
        html=f"<p>{body}</p><p>AKASHIC_EXPORT:{operation_id}</p>",
    )


@pytest.mark.asyncio
async def test_broker_requires_live_heartbeat_and_never_queues_offline(
    tmp_path: Path,
) -> None:
    store = NotesBridgeAuditStore(tmp_path / "notes-bridge.sqlite3")
    broker = NotesBridgeBroker(_config(), store)

    result = await broker.execute(_operation())

    assert result.status == "skipped_offline"
    assert result.error_code == "mac_notes_bridge_offline"
    assert store.get("operation-1").status == "skipped_offline"  # type: ignore[union-attr]
    assert b"secret-body" not in (tmp_path / "notes-bridge.sqlite3").read_bytes()


@pytest.mark.asyncio
async def test_broker_propose_commit_success(tmp_path: Path) -> None:
    store = NotesBridgeAuditStore(tmp_path / "notes-bridge.sqlite3")
    broker = NotesBridgeBroker(_config(), store)
    mac = await _connect(broker)
    operation = _operation()
    operation_hash = request_hash(asdict(operation))
    execute = asyncio.create_task(broker.execute(operation))

    proposed = await mac.receive()
    assert proposed["type"] == "propose"
    await mac.send(
        {
            "type": "proposal_accepted",
            "operation_id": operation.operation_id,
            "request_hash": operation_hash,
        }
    )
    commit = await mac.receive()
    assert commit["type"] == "commit"
    await mac.send(
        {
            "type": "result",
            "operation_id": operation.operation_id,
            "request_hash": operation_hash,
            "status": "committed",
            "note_id": "note-1",
            "folder_id": "folder-1",
        }
    )

    result = await execute
    assert result.status == "committed"
    assert result.note_id == "note-1"
    assert store.get(operation.operation_id).status == "committed"  # type: ignore[union-attr]
    await mac.disconnect()


@pytest.mark.asyncio
async def test_disconnect_before_commit_is_definitely_skipped(tmp_path: Path) -> None:
    store = NotesBridgeAuditStore(tmp_path / "notes-bridge.sqlite3")
    broker = NotesBridgeBroker(_config(), store)
    mac = await _connect(broker)
    execute = asyncio.create_task(broker.execute(_operation()))
    proposed = await mac.receive()
    assert proposed["type"] == "propose"

    await mac.disconnect()
    result = await execute

    assert result.status == "skipped_offline"
    assert result.error_code == "mac_bridge_disconnected_before_commit"


@pytest.mark.asyncio
async def test_proposal_timeout_aborts_mac_memory_and_never_sends_commit(
    tmp_path: Path,
) -> None:
    broker = NotesBridgeBroker(
        _config(), NotesBridgeAuditStore(tmp_path / "notes-bridge.sqlite3")
    )
    mac = await _connect(broker)
    execute = asyncio.create_task(broker.execute(_operation()))
    proposed = await mac.receive()
    assert proposed["type"] == "propose"

    aborted = await mac.receive()
    result = await execute

    assert aborted["type"] == "abort"
    assert aborted["operation_id"] == "operation-1"
    assert result.status == "skipped_offline"
    assert mac.websocket.outgoing.empty()
    await mac.disconnect()


@pytest.mark.asyncio
async def test_disconnect_after_commit_is_unknown_and_not_replayed(
    tmp_path: Path,
) -> None:
    store = NotesBridgeAuditStore(tmp_path / "notes-bridge.sqlite3")
    broker = NotesBridgeBroker(_config(), store)
    mac = await _connect(broker)
    operation = _operation()
    operation_hash = request_hash(asdict(operation))
    execute = asyncio.create_task(broker.execute(operation))
    _ = await mac.receive()
    await mac.send(
        {
            "type": "proposal_accepted",
            "operation_id": operation.operation_id,
            "request_hash": operation_hash,
        }
    )
    commit = await mac.receive()
    assert commit["type"] == "commit"

    await mac.disconnect()
    result = await execute

    assert result.status == "outcome_unknown"
    assert store.get(operation.operation_id).status == "outcome_unknown"  # type: ignore[union-attr]


def test_cloud_restart_classifies_incomplete_operations_without_payload_replay(
    tmp_path: Path,
) -> None:
    store = NotesBridgeAuditStore(tmp_path / "notes-bridge.sqlite3")
    operation = _operation()
    operation_hash = request_hash(asdict(operation))
    common = {
        "bridge_id": "mac-primary",
        "connection_epoch": "old-epoch",
        "action": "create",
        "document_key_hash": "document-hash",
        "request_hash": operation_hash,
        "content_hash": "content-hash",
    }
    store.record(operation_id="before", status="proposal_accepted", **common)
    store.record(operation_id="after", status="commit_sent", **common)

    broker = NotesBridgeBroker(_config(), store)

    assert store.get("before").status == "skipped_offline"  # type: ignore[union-attr]
    assert store.get("before").error_code == "cloud_restarted_before_commit"  # type: ignore[union-attr]
    assert store.get("after").status == "outcome_unknown"  # type: ignore[union-attr]
    assert store.get("after").error_code == "cloud_restarted_after_commit"  # type: ignore[union-attr]
    assert broker.connection_status()["startup_recovery"] == {
        "skipped_before_commit": 1,
        "unknown_after_commit": 1,
    }


@pytest.mark.asyncio
async def test_bad_handshake_signature_never_becomes_online(tmp_path: Path) -> None:
    broker = NotesBridgeBroker(
        _config(), NotesBridgeAuditStore(tmp_path / "notes-bridge.sqlite3")
    )
    websocket = FakeWebSocket()
    task = asyncio.create_task(broker.handle_websocket(websocket))
    await websocket.incoming.put(
        {
            "type": "hello",
            "protocol": 1,
            "bridge_id": "mac-primary",
            "client_nonce": "client-nonce-that-is-long-enough",
        }
    )
    challenge = await websocket.outgoing.get()
    await websocket.incoming.put(
        {
            "type": "proof",
            "bridge_id": "mac-primary",
            "client_nonce": "client-nonce-that-is-long-enough",
            "challenge_id": challenge["challenge_id"],
            "server_nonce": challenge["server_nonce"],
            "signature": "bad",
        }
    )

    await asyncio.wait_for(task, timeout=1)
    assert not broker.is_online()
    assert websocket.closed


class FakeLocalBridge:
    def __init__(self) -> None:
        self.create_calls = 0
        self.append_calls = 0

    async def probe(self) -> None:
        return None

    async def create(self, **_: object) -> NotesMutationReceipt:
        self.create_calls += 1
        return NotesMutationReceipt("note-1", "folder-1")

    async def append(self, **_: object) -> NotesMutationReceipt:
        self.append_calls += 1
        return NotesMutationReceipt("note-1", "folder-1")

    async def find_marker(self, marker: str) -> NotesMutationReceipt | None:
        del marker
        return None


@pytest.mark.asyncio
async def test_mac_executor_is_idempotent_and_ledger_omits_body(tmp_path: Path) -> None:
    store = MacNotesReceiptStore(tmp_path / "mac.sqlite3")
    bridge = FakeLocalBridge()
    executor = MacNotesExecutor(
        config=AppleNotesConfig(),
        bridge=bridge,  # type: ignore[arg-type]
        receipts=store,
    )
    operation = _operation(body="local-secret-body")
    operation_hash = request_hash(asdict(operation))
    _ = await executor.accept_proposal(operation, operation_hash)

    first = await executor.execute(operation, operation_hash)
    second = await executor.execute(operation, operation_hash)

    assert first.status == second.status == "committed"
    assert bridge.create_calls == 1
    assert b"local-secret-body" not in (tmp_path / "mac.sqlite3").read_bytes()


@pytest.mark.asyncio
async def test_mac_executor_never_replays_interrupted_commit(tmp_path: Path) -> None:
    store = MacNotesReceiptStore(tmp_path / "mac.sqlite3")
    bridge = FakeLocalBridge()
    executor = MacNotesExecutor(
        config=AppleNotesConfig(),
        bridge=bridge,  # type: ignore[arg-type]
        receipts=store,
    )
    operation = _operation()
    operation_hash = request_hash(asdict(operation))
    _ = await executor.accept_proposal(operation, operation_hash)
    _ = store.mark_executing(operation.operation_id)

    result = await executor.execute(operation, operation_hash)

    assert result.status == "outcome_unknown"
    assert bridge.create_calls == 0

    reconciled = store.reconcile_committed(
        operation.operation_id,
        note_id="note-1",
        folder_id="folder-1",
    )
    assert reconciled.status == "committed"
    assert store.get(operation.operation_id).note_id == "note-1"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mac_executor_rejects_cloud_target_expansion(tmp_path: Path) -> None:
    executor = MacNotesExecutor(
        config=AppleNotesConfig(account="default", folder="Akashic"),
        bridge=FakeLocalBridge(),  # type: ignore[arg-type]
        receipts=MacNotesReceiptStore(tmp_path / "mac.sqlite3"),
    )
    operation = NotesBridgeOperation(**{**asdict(_operation()), "folder": "Private"})

    with pytest.raises(NotesOperationRejected, match="未授权"):
        await executor.accept_proposal(operation, request_hash(asdict(operation)))


@pytest.mark.asyncio
async def test_real_websocket_client_and_broker_complete_one_commit(
    tmp_path: Path,
) -> None:
    broker = NotesBridgeBroker(
        _config(), NotesBridgeAuditStore(tmp_path / "cloud.sqlite3")
    )
    local_bridge = FakeLocalBridge()
    executor = MacNotesExecutor(
        config=AppleNotesConfig(),
        bridge=local_bridge,  # type: ignore[arg-type]
        receipts=MacNotesReceiptStore(tmp_path / "mac.sqlite3"),
    )

    class WebSocketAdapter:
        def __init__(self, websocket: Any) -> None:
            self.websocket = websocket

        async def accept(self) -> None:
            return None

        async def receive_json(self) -> dict[str, Any]:
            value = json.loads(await self.websocket.recv())
            assert isinstance(value, dict)
            return value

        async def send_json(self, value: dict[str, Any]) -> None:
            await self.websocket.send(json.dumps(value, ensure_ascii=False))

        async def close(self, *, code: int = 1000) -> None:
            await self.websocket.close(code=code)

    async def accept(websocket: Any) -> None:
        await broker.handle_websocket(WebSocketAdapter(websocket))

    server = await websockets.serve(accept, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    client = MacNotesBridgeClient(
        url=f"ws://127.0.0.1:{port}/ws",
        bridge_id="mac-primary",
        token=_TOKEN,
        executor=executor,
        status_path=tmp_path / "status.json",
    )
    client_task = asyncio.create_task(client.run_forever())
    try:
        for _ in range(100):
            if broker.is_online():
                break
            await asyncio.sleep(0.01)
        assert broker.is_online()

        result = await broker.execute(_operation("network-operation"))

        assert result.status == "committed"
        assert local_bridge.create_calls == 1
        assert json.loads((tmp_path / "status.json").read_text())["connected"]
    finally:
        client.stop()
        await asyncio.wait_for(client_task, timeout=2)
        await broker.close()
        server.close()
        await server.wait_closed()


def test_notes_bridge_config_fails_closed_and_resolves_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="至少需要 32"):
        _load_notes_bridge_config(
            {"notes_bridge": {"enabled": True, "token": "short"}},
            tmp_path,
        )
    with pytest.raises(ValueError, match="只能监听 loopback"):
        _load_notes_bridge_config(
            {
                "notes_bridge": {
                    "enabled": True,
                    "host": "0.0.0.0",
                    "token": _TOKEN,
                }
            },
            tmp_path,
        )
    monkeypatch.setenv("TEST_NOTES_BRIDGE_TOKEN", _TOKEN)

    config = _load_notes_bridge_config(
        {
            "notes_bridge": {
                "enabled": True,
                "token": "${TEST_NOTES_BRIDGE_TOKEN}",
            }
        },
        tmp_path,
    )

    assert config.enabled
    assert config.token == _TOKEN


def test_plugin_runtime_service_is_unavailable_during_prepare(tmp_path: Path) -> None:
    context = PluginContext(
        event_bus=None,  # type: ignore[arg-type]
        tool_registry=None,
        plugin_id="test",
        plugin_dir=tmp_path,
        data_dir=None,
        kv_store=PluginKVStore(tmp_path / "kv.json", writable=False),
        runtime_services=None,
    )
    with pytest.raises(RuntimeError, match="prepare"):
        context.require_runtime_service("notes_bridge.broker.v1")

    service = object()
    context.runtime_services = {"notes_bridge.broker.v1": service}
    assert context.require_runtime_service("notes_bridge.broker.v1") is service


def test_ssh_tunnel_is_loopback_only_and_fail_closed(tmp_path: Path) -> None:
    identity = tmp_path / "id_ed25519"
    identity.write_text("fixture", encoding="utf-8")

    arguments = _ssh_tunnel_program_arguments(
        target="ubuntu@101.32.194.251",
        identity=identity,
        local_port=6330,
        remote_port=6330,
    )

    assert "BatchMode=yes" in arguments
    assert "ExitOnForwardFailure=yes" in arguments
    assert "StrictHostKeyChecking=yes" in arguments
    assert "127.0.0.1:6330:127.0.0.1:6330" in arguments
    assert not any("token" in value.lower() for value in arguments)
