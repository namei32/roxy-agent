from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from agent.host_bridge.client import HostBridgeShellProcessManager
from agent.identity import roxy_env

_MONITOR_INTERVAL_S = 2.0


async def claim_host_bridge_boot() -> dict[str, Any] | None:
    """Claim host execution ownership before Core initializes mutable runtime state."""

    identity = _configured_bridge_identity()
    if identity is None:
        return None
    manager = HostBridgeShellProcessManager(*identity)
    try:
        return await manager.claim_boot()
    finally:
        await manager.close_transport()


def build_host_bridge_monitor() -> Coroutine[Any, Any, None] | None:
    """Build the required Core liveness monitor for host-bridge mode."""

    identity = _configured_bridge_identity()
    if identity is None:
        return None
    return _monitor(*identity)


def _configured_bridge_identity() -> tuple[Path, str, str, str, str] | None:
    """Load the complete bridge identity or fail at the environment boundary."""

    mode = roxy_env("EXECUTION_MODE", "local")
    if mode == "local":
        return None
    if mode != "host-bridge":
        raise RuntimeError("ROXY_EXECUTION_MODE 只能是 local 或 host-bridge")
    socket_text = roxy_env("HOST_BRIDGE_SOCKET")
    token = roxy_env("HOST_BRIDGE_TOKEN")
    boot_id = roxy_env("BOOT_ID")
    release_commit = roxy_env("RUNTIME_COMMIT")
    toolchain_digest = roxy_env("HOST_TOOLCHAIN_DIGEST")
    if not all((socket_text, token, boot_id, release_commit, toolchain_digest)):
        raise RuntimeError(
            "host-bridge monitor 缺少 socket/token/boot/release identity"
        )
    socket_path = Path(socket_text)
    if not socket_path.is_absolute():
        raise RuntimeError("ROXY_HOST_BRIDGE_SOCKET 必须是绝对路径")
    return socket_path, boot_id, token, release_commit, toolchain_digest


async def _monitor(
    socket_path: Path,
    boot_id: str,
    token: str,
    release_commit: str,
    toolchain_digest: str,
) -> None:
    manager = HostBridgeShellProcessManager(
        socket_path,
        boot_id,
        token,
        release_commit,
        toolchain_digest,
    )
    try:
        while True:
            await manager.probe()
            await asyncio.sleep(_MONITOR_INTERVAL_S)
    finally:
        await manager.close_transport()
