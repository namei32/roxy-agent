from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
from typing import Any

import websockets

from infra.notes_bridge.auth import request_hash, sign_message, verify_message
from infra.notes_bridge.port import NotesBridgeOperation, NotesBridgeResult
from plugins.apple_notes.bridge import NotesBridgeError

from .executor import MacNotesExecutor


class MacNotesBridgeClient:
    def __init__(
        self,
        *,
        url: str,
        bridge_id: str,
        token: str,
        executor: MacNotesExecutor,
        status_path: Path,
        max_message_bytes: int = 1024 * 1024,
    ) -> None:
        if not url.startswith(("wss://", "ws://127.0.0.1", "ws://localhost")):
            raise ValueError("Mac Notes Bridge 远程地址必须使用 wss://")
        if len(token) < 32:
            raise ValueError("Mac Notes Bridge token 至少需要 32 个字符")
        self._url = url
        self._bridge_id = bridge_id
        self._token = token
        self._executor = executor
        self._status_path = status_path
        self._max_message_bytes = max_message_bytes
        self._stop = asyncio.Event()
        self._connection_epoch = ""
        self._send_sequence = 0
        self._recv_sequence = 0
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, tuple[NotesBridgeOperation, str]] = {}
        self._active_websocket: Any | None = None

    async def run_forever(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._run_connection()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._write_status(connected=False, error=str(error))
            self._pending.clear()
            self._connection_epoch = ""
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                delay = min(delay * 2, 30.0)

    def stop(self) -> None:
        self._stop.set()
        websocket = self._active_websocket
        if websocket is not None:
            try:
                _ = asyncio.get_running_loop().create_task(websocket.close())
            except RuntimeError:
                pass

    async def _run_connection(self) -> None:
        async with websockets.connect(
            self._url,
            max_size=self._max_message_bytes,
            ping_interval=20,
            ping_timeout=20,
        ) as websocket:
            self._active_websocket = websocket
            client_nonce = secrets.token_urlsafe(32)
            await websocket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "protocol": 1,
                        "bridge_id": self._bridge_id,
                        "client_nonce": client_nonce,
                    }
                )
            )
            challenge = await _receive_json(websocket)
            if (
                challenge.get("type") != "challenge"
                or challenge.get("bridge_id") != self._bridge_id
                or challenge.get("client_nonce") != client_nonce
            ):
                raise ValueError("云端 Notes Bridge challenge 无效")
            await websocket.send(
                json.dumps(
                    sign_message(
                        {
                            "type": "proof",
                            "bridge_id": self._bridge_id,
                            "client_nonce": client_nonce,
                            "challenge_id": challenge.get("challenge_id"),
                            "server_nonce": challenge.get("server_nonce"),
                        },
                        self._token,
                    )
                )
            )
            ready = await _receive_json(websocket)
            verify_message(ready, self._token)
            if (
                ready.get("type") != "ready"
                or ready.get("bridge_id") != self._bridge_id
            ):
                raise ValueError("云端 Notes Bridge ready 无效")
            self._connection_epoch = str(ready.get("connection_epoch") or "")
            self._recv_sequence = int(ready.get("sequence") or 0)
            self._send_sequence = 0
            heartbeat_interval = float(ready.get("heartbeat_interval_seconds") or 5)
            self._write_status(connected=True)
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(websocket, heartbeat_interval),
                name="mac_notes_bridge_heartbeat",
            )
            try:
                async for raw in websocket:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise ValueError("云端 Notes Bridge 消息必须是对象")
                    verify_message(message, self._token)
                    self._verify_server_envelope(message)
                    await self._handle_message(websocket, message)
            finally:
                _ = heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
                self._write_status(connected=False)
                if self._active_websocket is websocket:
                    self._active_websocket = None

    async def _heartbeat_loop(self, websocket: Any, interval: float) -> None:
        while True:
            notes_ready, permissions_ready = await self._executor.readiness()
            await self._send(
                websocket,
                {
                    "type": "heartbeat",
                    "notes_ready": notes_ready,
                    "permissions_ready": permissions_ready,
                    "sent_at": datetime.now().astimezone().isoformat(),
                },
            )
            self._write_status(
                connected=True,
                notes_ready=notes_ready,
                permissions_ready=permissions_ready,
            )
            await asyncio.sleep(interval)

    async def _handle_message(self, websocket: Any, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "")
        if message_type == "propose":
            raw_operation = message.get("operation")
            if not isinstance(raw_operation, dict):
                raise ValueError("Notes Bridge proposal 缺少 operation")
            operation = NotesBridgeOperation(**raw_operation)
            operation_hash = str(message.get("request_hash") or "")
            if request_hash(asdict(operation)) != operation_hash:
                raise ValueError("Notes Bridge proposal request_hash 无效")
            try:
                _ = await self._executor.accept_proposal(operation, operation_hash)
            except NotesBridgeError as error:
                await self._send(
                    websocket,
                    {
                        "type": "proposal_rejected",
                        "operation_id": operation.operation_id,
                        "request_hash": operation_hash,
                        "error_code": error.code,
                        "detail": error.detail,
                    },
                )
                return
            self._pending[operation.operation_id] = (operation, operation_hash)
            await self._send(
                websocket,
                {
                    "type": "proposal_accepted",
                    "operation_id": operation.operation_id,
                    "request_hash": operation_hash,
                },
            )
            return
        if message_type == "abort":
            operation_id = str(message.get("operation_id") or "")
            pending = self._pending.get(operation_id)
            if pending is None:
                return
            if pending[1] != str(message.get("request_hash") or ""):
                raise ValueError("Notes Bridge abort request_hash 不匹配")
            self._pending.pop(operation_id, None)
            return
        if message_type != "commit":
            raise ValueError(f"不支持的云端 Notes Bridge 消息: {message_type}")
        operation_id = str(message.get("operation_id") or "")
        pending = self._pending.get(operation_id)
        if pending is None or pending[1] != str(message.get("request_hash") or ""):
            raise ValueError("Notes Bridge commit 没有匹配的本地 proposal")
        operation, operation_hash = pending
        result = await self._executor.execute(operation, operation_hash)
        await self._send_result(websocket, result, operation_hash)
        self._pending.pop(operation_id, None)

    async def _send_result(
        self,
        websocket: Any,
        result: NotesBridgeResult,
        operation_hash: str,
    ) -> None:
        await self._send(
            websocket,
            {
                "type": "result",
                **asdict(result),
                "request_hash": operation_hash,
            },
        )

    async def _send(self, websocket: Any, message: dict[str, Any]) -> None:
        async with self._send_lock:
            self._send_sequence += 1
            envelope = sign_message(
                {
                    **message,
                    "bridge_id": self._bridge_id,
                    "connection_epoch": self._connection_epoch,
                    "sequence": self._send_sequence,
                },
                self._token,
            )
            await websocket.send(json.dumps(envelope, ensure_ascii=False))

    def _verify_server_envelope(self, message: dict[str, Any]) -> None:
        if (
            message.get("bridge_id") != self._bridge_id
            or message.get("connection_epoch") != self._connection_epoch
        ):
            raise ValueError("云端 Notes Bridge epoch 不匹配")
        sequence = int(message.get("sequence") or 0)
        if sequence <= self._recv_sequence:
            raise ValueError("云端 Notes Bridge 消息重放或乱序")
        self._recv_sequence = sequence

    def _write_status(
        self,
        *,
        connected: bool,
        notes_ready: bool = False,
        permissions_ready: bool = False,
        error: str = "",
    ) -> None:
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "connected": connected,
            "bridge_id": self._bridge_id,
            "connection_epoch": self._connection_epoch,
            "notes_ready": notes_ready,
            "permissions_ready": permissions_ready,
            "updated_at": datetime.now().astimezone().isoformat(),
            "error": error[:1000],
        }
        temporary = self._status_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self._status_path)


async def _receive_json(websocket: Any) -> dict[str, Any]:
    raw = await websocket.recv()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Notes Bridge 消息必须是 JSON 对象")
    return value


__all__ = ["MacNotesBridgeClient"]
