from __future__ import annotations

from pathlib import Path
from typing import Protocol

from agent.host_bridge.client import HostBridgeShellProcessManager
from agent.host_bridge.client import HostBridgeSkillCapabilityChecker
from agent.identity import roxy_env
from agent.tools.unified_exec import ExecutionCleanupReport
from agent.tools.unified_exec import ExecutionResult
from agent.tools.unified_exec import ShellProcessManager

_SOCKET_ENV = "HOST_BRIDGE_SOCKET"
_TOKEN_ENV = "HOST_BRIDGE_TOKEN"
_BOOT_ID_ENV = "BOOT_ID"
_MODE_ENV = "EXECUTION_MODE"
_RELEASE_COMMIT_ENV = "RUNTIME_COMMIT"
_TOOLCHAIN_DIGEST_ENV = "HOST_TOOLCHAIN_DIGEST"


class ShellProcessManagerProtocol(Protocol):
    async def exec_command(
        self,
        *,
        command: str,
        argv: list[str],
        cwd: Path | None,
        env: dict[str, str],
        tty: bool,
        yield_time_ms: int,
        max_output_tokens: int,
        hard_timeout_s: int,
        owner_session_key: str,
    ) -> ExecutionResult: ...
    async def write_stdin(
        self,
        *,
        execution_id: int,
        chars: str,
        yield_time_ms: int,
        max_output_tokens: int,
        owner_session_key: str,
    ) -> ExecutionResult: ...
    async def terminate_execution(
        self, execution_id: int, *, owner_session_key: str
    ) -> bool: ...
    async def terminate_owner(
        self, owner_session_key: str
    ) -> ExecutionCleanupReport: ...
    async def shutdown(self) -> ExecutionCleanupReport: ...
    async def active_execution_ids(self) -> list[int]: ...


def build_shell_process_manager() -> ShellProcessManagerProtocol:
    """Select the explicit local or host-bridge execution backend."""

    mode = roxy_env(_MODE_ENV, "local")
    if mode == "local":
        return ShellProcessManager()
    if mode != "host-bridge":
        raise RuntimeError("ROXY_EXECUTION_MODE 只能是 local 或 host-bridge")
    socket_path, boot_id, token, release_commit, toolchain_digest = _bridge_identity()
    return HostBridgeShellProcessManager(
        socket_path,
        boot_id,
        token,
        release_commit,
        toolchain_digest,
    )


def _bridge_identity() -> tuple[Path, str, str, str, str]:
    """Load and validate the configured Host Bridge identity."""

    socket_text = roxy_env(_SOCKET_ENV)
    if not socket_text:
        raise RuntimeError("host-bridge 模式缺少 ROXY_HOST_BRIDGE_SOCKET")
    token = roxy_env(_TOKEN_ENV)
    boot_id = roxy_env(_BOOT_ID_ENV)
    release_commit = roxy_env(_RELEASE_COMMIT_ENV)
    toolchain_digest = roxy_env(_TOOLCHAIN_DIGEST_ENV)
    if not token or not boot_id or not release_commit or not toolchain_digest:
        raise RuntimeError(
            "配置 ROXY_HOST_BRIDGE_SOCKET 时必须同时提供 "
            "token、boot 和 release identity"
        )
    socket_path = Path(socket_text)
    if not socket_path.is_absolute():
        raise RuntimeError("ROXY_HOST_BRIDGE_SOCKET 必须是绝对路径")
    return socket_path, boot_id, token, release_commit, toolchain_digest


def build_file_bridge() -> HostBridgeShellProcessManager | None:
    """Build a file RPC client only in explicit host-bridge mode."""

    manager = build_shell_process_manager()
    if isinstance(manager, HostBridgeShellProcessManager):
        return manager
    return None


def build_skill_capability_checker() -> HostBridgeSkillCapabilityChecker | None:
    """Build the host requirement checker only in explicit bridge mode."""

    mode = roxy_env(_MODE_ENV, "local")
    if mode == "local":
        return None
    if mode != "host-bridge":
        raise RuntimeError("ROXY_EXECUTION_MODE 只能是 local 或 host-bridge")
    socket_path, boot_id, token, release_commit, toolchain_digest = _bridge_identity()
    return HostBridgeSkillCapabilityChecker(
        socket_path, boot_id, token, release_commit, toolchain_digest
    )
