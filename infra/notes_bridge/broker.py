from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import hashlib
import json
import secrets
import time
from typing import Any

from agent.config_models import NotesBridgeConfig

from .auth import InvalidBridgeSignature, request_hash, sign_message, verify_message
from .port import NotesBridgeOperation, NotesBridgeResult
from .store import NotesBridgeAuditStore


class _BridgeDisconnected(ConnectionError):
    pass


class _ProposalRejected(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass
class _PendingOperation:
    request_hash: str
    accepted: asyncio.Future[None]
    result: asyncio.Future[NotesBridgeResult]
    commit_sent: bool = False


class _ActiveConnection:
    def __init__(
        self,
        *,
        websocket: Any,
        bridge_id: str,
        epoch: str,
        token: str,
    ) -> None:
        self.websocket = websocket
        self.bridge_id = bridge_id
        self.epoch = epoch
        self.token = token
        self.last_heartbeat = time.monotonic()
        self.notes_ready = False
        self.permissions_ready = False
        self.closed = False
        self.recv_sequence = 0
        self.send_sequence = 0
        self.send_lock = asyncio.Lock()
        self.pending: dict[str, _PendingOperation] = {}

    async def send(self, message: dict[str, Any]) -> None:
        async with self.send_lock:
            if self.closed:
                raise _BridgeDisconnected("Mac Bridge 连接已经关闭")
            self.send_sequence += 1
            envelope = sign_message(
                {
                    **message,
                    "bridge_id": self.bridge_id,
                    "connection_epoch": self.epoch,
                    "sequence": self.send_sequence,
                },
                self.token,
            )
            try:
                await self.websocket.send_json(envelope)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.closed = True
                raise _BridgeDisconnected(
                    "Mac Bridge 发送期间断开"
                ) from error

    def fail_pending(self) -> None:
        self.closed = True
        for pending in self.pending.values():
            error = _BridgeDisconnected("Mac Bridge 在操作期间断开")
            target = pending.result if pending.commit_sent else pending.accepted
            if not target.done():
                target.set_exception(error)


class NotesBridgeBroker:
    """Own one authenticated Mac connection and the propose/commit boundary."""

    def __init__(
        self,
        config: NotesBridgeConfig,
        store: NotesBridgeAuditStore,
    ) -> None:
        self._config = config
        self._store = store
        self._startup_recovery = store.recover_incomplete()
        self._active: _ActiveConnection | None = None
        self._connection_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._closed = False

    def is_online(self) -> bool:
        connection = self._active
        return bool(
            not self._closed
            and connection is not None
            and not connection.closed
            and connection.notes_ready
            and connection.permissions_ready
            and time.monotonic() - connection.last_heartbeat
            <= self._config.offline_after_seconds
        )

    def connection_status(self) -> dict[str, object]:
        connection = self._active
        if connection is None:
            return {
                "online": False,
                "bridge_id": self._config.bridge_id,
                "connection_epoch": "",
                "heartbeat_age_seconds": None,
                "notes_ready": False,
                "permissions_ready": False,
                "startup_recovery": dict(self._startup_recovery),
            }
        return {
            "online": self.is_online(),
            "bridge_id": connection.bridge_id,
            "connection_epoch": connection.epoch,
            "heartbeat_age_seconds": max(
                0.0, time.monotonic() - connection.last_heartbeat
            ),
            "notes_ready": connection.notes_ready,
            "permissions_ready": connection.permissions_ready,
            "startup_recovery": dict(self._startup_recovery),
        }

    async def execute(self, operation: NotesBridgeOperation) -> NotesBridgeResult:
        _validate_operation(operation, self._config.max_message_bytes)
        payload = asdict(operation)
        operation_hash = request_hash(payload)
        document_hash = _sha256(operation.document_key)
        content_hash = _sha256(operation.html)
        async with self._operation_lock:
            connection = self._active
            if not self.is_online() or connection is None:
                result = NotesBridgeResult(
                    status="skipped_offline",
                    operation_id=operation.operation_id,
                    error_code="mac_notes_bridge_offline",
                    detail="Mac 当前没有经过认证且心跳新鲜的 Notes Bridge 连接",
                )
                await self._record(
                    operation,
                    operation_hash,
                    document_hash,
                    content_hash,
                    result,
                    connection=None,
                )
                return result

            loop = asyncio.get_running_loop()
            pending = _PendingOperation(
                request_hash=operation_hash,
                accepted=loop.create_future(),
                result=loop.create_future(),
            )
            connection.pending[operation.operation_id] = pending
            try:
                try:
                    await connection.send(
                        {
                            "type": "propose",
                            "operation": payload,
                            "request_hash": operation_hash,
                        }
                    )
                except _BridgeDisconnected:
                    return await self._terminal_before_commit(
                        operation,
                        operation_hash,
                        document_hash,
                        content_hash,
                        connection,
                    )
                await self._record_status(
                    operation,
                    operation_hash,
                    document_hash,
                    content_hash,
                    "proposed",
                    connection,
                )
                try:
                    await asyncio.wait_for(
                        pending.accepted,
                        timeout=self._config.proposal_timeout_seconds,
                    )
                except _ProposalRejected as error:
                    result = NotesBridgeResult(
                        status="operation_rejected",
                        operation_id=operation.operation_id,
                        error_code=error.code,
                        detail=error.detail,
                    )
                    await self._record(
                        operation,
                        operation_hash,
                        document_hash,
                        content_hash,
                        result,
                        connection=connection,
                    )
                    return result
                except (TimeoutError, _BridgeDisconnected):
                    return await self._terminal_before_commit(
                        operation,
                        operation_hash,
                        document_hash,
                        content_hash,
                        connection,
                    )

                await self._record_status(
                    operation,
                    operation_hash,
                    document_hash,
                    content_hash,
                    "proposal_accepted",
                    connection,
                )
                if (
                    connection.closed
                    or self._active is not connection
                    or not self.is_online()
                ):
                    return await self._terminal_before_commit(
                        operation,
                        operation_hash,
                        document_hash,
                        content_hash,
                        connection,
                    )
                pending.commit_sent = True
                await self._record_status(
                    operation,
                    operation_hash,
                    document_hash,
                    content_hash,
                    "commit_sent",
                    connection,
                )
                try:
                    await connection.send(
                        {
                            "type": "commit",
                            "operation_id": operation.operation_id,
                            "request_hash": operation_hash,
                        }
                    )
                    result = await asyncio.wait_for(
                        pending.result,
                        timeout=self._config.commit_timeout_seconds,
                    )
                except (TimeoutError, _BridgeDisconnected):
                    result = NotesBridgeResult(
                        status="outcome_unknown",
                        operation_id=operation.operation_id,
                        error_code="mac_bridge_commit_outcome_unknown",
                        detail=(
                            "commit 已发出但未收到可信回执；禁止自动重放，"
                            "只能按操作标识核对"
                        ),
                    )
                await self._record(
                    operation,
                    operation_hash,
                    document_hash,
                    content_hash,
                    result,
                    connection=connection,
                )
                return result
            finally:
                connection.pending.pop(operation.operation_id, None)

    async def handle_websocket(self, websocket: Any) -> None:
        """Authenticate one outbound Mac connection and route signed replies."""

        await websocket.accept()
        connection: _ActiveConnection | None = None
        try:
            hello = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            _require_message_size(hello, self._config.max_message_bytes)
            if (
                hello.get("type") != "hello"
                or hello.get("protocol") != 1
                or str(hello.get("bridge_id") or "") != self._config.bridge_id
            ):
                raise ValueError("Mac Notes Bridge hello 无效")
            client_nonce = str(hello.get("client_nonce") or "")
            if len(client_nonce) < 24:
                raise ValueError("Mac Notes Bridge client nonce 无效")
            challenge_id = secrets.token_urlsafe(24)
            server_nonce = secrets.token_urlsafe(32)
            await websocket.send_json(
                {
                    "type": "challenge",
                    "protocol": 1,
                    "bridge_id": self._config.bridge_id,
                    "client_nonce": client_nonce,
                    "challenge_id": challenge_id,
                    "server_nonce": server_nonce,
                }
            )
            proof = await asyncio.wait_for(websocket.receive_json(), timeout=10)
            _require_message_size(proof, self._config.max_message_bytes)
            verify_message(proof, self._config.token)
            if (
                proof.get("type") != "proof"
                or proof.get("bridge_id") != self._config.bridge_id
                or proof.get("client_nonce") != client_nonce
                or proof.get("challenge_id") != challenge_id
                or proof.get("server_nonce") != server_nonce
            ):
                raise ValueError("Mac Notes Bridge challenge proof 无效")

            connection = _ActiveConnection(
                websocket=websocket,
                bridge_id=self._config.bridge_id,
                epoch=secrets.token_urlsafe(24),
                token=self._config.token,
            )
            async with self._connection_lock:
                previous = self._active
                self._active = connection
            if previous is not None:
                previous.fail_pending()
                await _safe_close(previous.websocket, code=4001)
            await connection.send(
                {
                    "type": "ready",
                    "heartbeat_interval_seconds": (
                        self._config.heartbeat_interval_seconds
                    ),
                }
            )
            while not self._closed:
                message = await websocket.receive_json()
                _require_message_size(message, self._config.max_message_bytes)
                verify_message(message, self._config.token)
                self._handle_authenticated_message(connection, message)
        except asyncio.CancelledError:
            raise
        except (
            InvalidBridgeSignature,
            TimeoutError,
            ValueError,
            ConnectionError,
        ):
            pass
        except Exception:
            # Starlette raises WebSocketDisconnect here; transport closure is an
            # expected state transition and is classified through pending ops.
            pass
        finally:
            if connection is not None:
                connection.fail_pending()
                async with self._connection_lock:
                    if self._active is connection:
                        self._active = None
            await _safe_close(websocket)

    def _handle_authenticated_message(
        self,
        connection: _ActiveConnection,
        message: dict[str, Any],
    ) -> None:
        if (
            message.get("bridge_id") != connection.bridge_id
            or message.get("connection_epoch") != connection.epoch
        ):
            raise ValueError("Mac Notes Bridge connection epoch 不匹配")
        sequence = int(message.get("sequence") or 0)
        if sequence <= connection.recv_sequence:
            raise ValueError("Mac Notes Bridge 消息重放或乱序")
        connection.recv_sequence = sequence
        message_type = str(message.get("type") or "")
        if message_type == "heartbeat":
            connection.last_heartbeat = time.monotonic()
            connection.notes_ready = bool(message.get("notes_ready"))
            connection.permissions_ready = bool(message.get("permissions_ready"))
            return
        operation_id = str(message.get("operation_id") or "")
        pending = connection.pending.get(operation_id)
        if pending is None:
            raise ValueError("Mac Notes Bridge 回执引用未知操作")
        if str(message.get("request_hash") or "") != pending.request_hash:
            raise ValueError("Mac Notes Bridge 回执 request_hash 不匹配")
        if message_type == "proposal_accepted":
            if not pending.accepted.done():
                pending.accepted.set_result(None)
            return
        if message_type == "proposal_rejected":
            if not pending.accepted.done():
                pending.accepted.set_exception(
                    _ProposalRejected(
                        str(message.get("error_code") or "proposal_rejected")[:128],
                        str(message.get("detail") or "Mac 拒绝 proposal")[:2000],
                    )
                )
            return
        if message_type != "result" or not pending.commit_sent:
            raise ValueError("Mac Notes Bridge 操作消息顺序无效")
        status = str(message.get("status") or "")
        if status not in {
            "committed",
            "not_found",
            "operation_rejected",
            "unit_failed",
            "outcome_unknown",
        }:
            raise ValueError("Mac Notes Bridge result status 无效")
        if not pending.result.done():
            pending.result.set_result(
                NotesBridgeResult(
                    status=status,  # type: ignore[arg-type]
                    operation_id=operation_id,
                    note_id=str(message.get("note_id") or "")[:512],
                    folder_id=str(message.get("folder_id") or "")[:512],
                    error_code=str(message.get("error_code") or "")[:128],
                    detail=str(message.get("detail") or "")[:2000],
                )
            )

    async def close(self) -> None:
        self._closed = True
        connection = self._active
        self._active = None
        if connection is not None:
            connection.fail_pending()
            await _safe_close(connection.websocket, code=1001)

    async def _terminal_before_commit(
        self,
        operation: NotesBridgeOperation,
        operation_hash: str,
        document_hash: str,
        content_hash: str,
        connection: _ActiveConnection,
    ) -> NotesBridgeResult:
        if not connection.closed and self._active is connection:
            try:
                await connection.send(
                    {
                        "type": "abort",
                        "operation_id": operation.operation_id,
                        "request_hash": operation_hash,
                    }
                )
            except _BridgeDisconnected:
                # A closed transport also clears the Mac client's in-memory
                # proposal. No commit was emitted, so this remains a known
                # no-effect outcome.
                pass
        result = NotesBridgeResult(
            status="skipped_offline",
            operation_id=operation.operation_id,
            error_code="mac_bridge_disconnected_before_commit",
            detail="Mac 未确认 proposal；commit 未发送，确定没有执行本次写入",
        )
        await self._record(
            operation,
            operation_hash,
            document_hash,
            content_hash,
            result,
            connection=connection,
        )
        return result

    async def _record_status(
        self,
        operation: NotesBridgeOperation,
        operation_hash: str,
        document_hash: str,
        content_hash: str,
        status: str,
        connection: _ActiveConnection,
    ) -> None:
        await asyncio.to_thread(
            self._store.record,
            operation_id=operation.operation_id,
            bridge_id=self._config.bridge_id,
            connection_epoch=connection.epoch,
            action=operation.action,
            document_key_hash=document_hash,
            request_hash=operation_hash,
            content_hash=content_hash,
            status=status,
        )

    async def _record(
        self,
        operation: NotesBridgeOperation,
        operation_hash: str,
        document_hash: str,
        content_hash: str,
        result: NotesBridgeResult,
        *,
        connection: _ActiveConnection | None,
    ) -> None:
        await asyncio.to_thread(
            self._store.record,
            operation_id=operation.operation_id,
            bridge_id=self._config.bridge_id,
            connection_epoch=connection.epoch if connection is not None else "",
            action=operation.action,
            document_key_hash=document_hash,
            request_hash=operation_hash,
            content_hash=content_hash,
            status=result.status,
            error_code=result.error_code,
        )


def _validate_operation(operation: NotesBridgeOperation, max_bytes: int) -> None:
    if not operation.operation_id or len(operation.operation_id) > 128:
        raise ValueError("Notes Bridge operation_id 无效")
    if operation.action == "create" and (not operation.title or not operation.html):
        raise ValueError("Notes Bridge create 缺少 title/html")
    if operation.action == "append" and (not operation.note_id or not operation.html):
        raise ValueError("Notes Bridge append 缺少 note_id/html")
    if operation.action == "find" and not operation.marker:
        raise ValueError("Notes Bridge find 缺少 marker")
    _require_message_size({"operation": asdict(operation)}, max_bytes)


def _require_message_size(message: object, max_bytes: int) -> None:
    encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > max_bytes:
        raise ValueError("Mac Notes Bridge 消息超过配置上限")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


async def _safe_close(websocket: Any, *, code: int = 1000) -> None:
    try:
        await websocket.close(code=code)
    except Exception:
        return


__all__ = ["NotesBridgeBroker"]
