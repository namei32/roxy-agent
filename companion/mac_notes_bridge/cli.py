from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import plistlib
import secrets
import signal
import re
import subprocess
import sys
from typing import Sequence

from plugins.apple_notes.bridge import AppleNotesBridge
from plugins.apple_notes.config import AppleNotesConfig

from .client import MacNotesBridgeClient
from .executor import MacNotesExecutor
from .store import MacNotesReceiptStore

_KEYCHAIN_SERVICE = "io.akashic.notes-bridge"
_LAUNCHD_LABEL = "io.akashic.notes-bridge"
_SSH_TUNNEL_LABEL = "io.akashic.notes-bridge.ssh-tunnel"
_SSH_TARGET_RE = re.compile(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.:-]+")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "pair":
        token = str(args.token or secrets.token_urlsafe(48))
        if len(token) < 32:
            parser.error("token 至少需要 32 个字符")
        _store_keychain_token(args.bridge_id, token)
        print(
            json.dumps(
                {"bridge_id": args.bridge_id, "token": token}, ensure_ascii=False
            )
        )
        return 0
    if args.command == "revoke":
        _delete_keychain_token(args.bridge_id)
        print(f"已撤销 Mac Keychain 中的 Bridge token: {args.bridge_id}")
        return 0
    if args.command == "status":
        print(json.dumps(_status(Path(args.data_dir)), ensure_ascii=False, indent=2))
        return 0
    if args.command == "install-launchd":
        plist_path = _install_launchd(args)
        print(f"launchd 已安装并启动: {plist_path}")
        return 0
    if args.command == "uninstall-launchd":
        _uninstall_launchd()
        print("launchd 服务已停止并移除")
        return 0
    if args.command == "install-ssh-tunnel":
        plist_path = _install_ssh_tunnel(args)
        print(f"SSH 隧道 launchd 已安装并启动: {plist_path}")
        return 0
    if args.command == "uninstall-ssh-tunnel":
        _uninstall_launchd_service(_SSH_TUNNEL_LABEL)
        print("SSH 隧道 launchd 已停止并移除")
        return 0
    if args.command == "probe":
        return asyncio.run(_probe(args))
    if args.command == "reconcile":
        return asyncio.run(_reconcile(args))
    if args.command == "run":
        return asyncio.run(_run(args))
    parser.error("缺少命令")
    return 2


async def _run(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    config = _notes_config(args)
    token = os.environ.get("AKASHIC_NOTES_BRIDGE_TOKEN", "").strip()
    if not token:
        token = _load_keychain_token(args.bridge_id)
    bridge = AppleNotesBridge(
        config=config,
        script_path=_project_root()
        / "plugins"
        / "apple_notes"
        / "scripts"
        / "notes.applescript",
        temp_dir=data_dir / "tmp",
    )
    executor = MacNotesExecutor(
        config=config,
        bridge=bridge,
        receipts=MacNotesReceiptStore(data_dir / "receipts.sqlite3"),
    )
    client = MacNotesBridgeClient(
        url=args.url,
        bridge_id=args.bridge_id,
        token=token,
        executor=executor,
        status_path=data_dir / "runtime-status.json",
        max_message_bytes=args.max_message_bytes,
    )
    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signal_name, client.stop)
        except NotImplementedError:
            pass
    await client.run_forever()
    return 0


async def _probe(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    config = _notes_config(args)
    bridge = AppleNotesBridge(
        config=config,
        script_path=_project_root()
        / "plugins"
        / "apple_notes"
        / "scripts"
        / "notes.applescript",
        temp_dir=data_dir / "tmp",
    )
    try:
        await bridge.probe()
    except Exception as error:
        print(json.dumps({"ready": False, "error": str(error)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {"ready": True, "account": config.account, "folder": config.folder},
            ensure_ascii=False,
        )
    )
    return 0


async def _reconcile(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    store = MacNotesReceiptStore(data_dir / "receipts.sqlite3")
    record = store.get(args.operation_id)
    if record is None:
        print(json.dumps({"status": "not_found"}, ensure_ascii=False))
        return 1
    if record.status == "committed":
        print(
            json.dumps(
                {
                    "status": "committed",
                    "operation_id": record.operation_id,
                    "note_id": record.note_id,
                    "folder_id": record.folder_id,
                },
                ensure_ascii=False,
            )
        )
        return 0
    if record.status not in {"executing", "outcome_unknown"}:
        print(
            json.dumps(
                {
                    "status": record.status,
                    "operation_id": record.operation_id,
                    "detail": "该状态不需要或不允许 marker 核对",
                },
                ensure_ascii=False,
            )
        )
        return 1
    config = _notes_config(args)
    bridge = AppleNotesBridge(
        config=config,
        script_path=_project_root()
        / "plugins"
        / "apple_notes"
        / "scripts"
        / "notes.applescript",
        temp_dir=data_dir / "tmp",
    )
    found = await bridge.find_marker(f"AKASHIC_EXPORT:{record.operation_id}")
    if found is None:
        print(
            json.dumps(
                {
                    "status": "outcome_unknown",
                    "operation_id": record.operation_id,
                    "detail": "未找到唯一 marker；保持不确定且不重放",
                },
                ensure_ascii=False,
            )
        )
        return 2
    committed = store.reconcile_committed(
        record.operation_id,
        note_id=found.note_id,
        folder_id=found.folder_id,
    )
    print(
        json.dumps(
            {
                "status": committed.status,
                "operation_id": committed.operation_id,
                "note_id": committed.note_id,
                "folder_id": committed.folder_id,
            },
            ensure_ascii=False,
        )
    )
    return 0


def _notes_config(args: argparse.Namespace) -> AppleNotesConfig:
    return AppleNotesConfig(
        account=args.account,
        folder=args.folder,
        create_folder_if_missing=not args.no_create_folder,
    )


def _status(data_dir: Path) -> dict[str, object]:
    status_path = data_dir.expanduser().resolve() / "runtime-status.json"
    raw: dict[str, object] = {}
    if status_path.is_file():
        value = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            raw = value
    raw_pid = raw.get("pid")
    pid = int(raw_pid) if isinstance(raw_pid, (str, int)) else 0
    process_alive = False
    if pid > 0:
        try:
            os.kill(pid, 0)
        except OSError:
            pass
        else:
            process_alive = True
    age_seconds: float | None = None
    updated_at = str(raw.get("updated_at") or "")
    if updated_at:
        try:
            age_seconds = max(
                0.0,
                (
                    datetime.now().astimezone() - datetime.fromisoformat(updated_at)
                ).total_seconds(),
            )
        except ValueError:
            pass
    receipts = MacNotesReceiptStore(
        data_dir.expanduser().resolve() / "receipts.sqlite3"
    )
    return {
        **raw,
        "process_alive": process_alive,
        "status_age_seconds": age_seconds,
        "locally_fresh": bool(
            raw.get("connected")
            and process_alive
            and age_seconds is not None
            and age_seconds <= 20
        ),
        "receipt_counts": receipts.summary(),
        "note": "最终在线判定以云端 Broker 的认证连接、心跳和本次 proposal ACK 为准",
    }


def _store_keychain_token(bridge_id: str, token: str) -> None:
    _ = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            bridge_id,
            "-s",
            _KEYCHAIN_SERVICE,
            "-w",
            token,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _load_keychain_token(bridge_id: str) -> str:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            bridge_id,
            "-s",
            _KEYCHAIN_SERVICE,
            "-w",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    token = result.stdout.strip()
    if len(token) < 32:
        raise RuntimeError("Mac Keychain 中的 Notes Bridge token 无效")
    return token


def _delete_keychain_token(bridge_id: str) -> None:
    _ = subprocess.run(
        [
            "/usr/bin/security",
            "delete-generic-password",
            "-a",
            bridge_id,
            "-s",
            _KEYCHAIN_SERVICE,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def _install_launchd(args: argparse.Namespace) -> Path:
    data_path = Path(args.data_dir).expanduser().resolve()
    data_path.mkdir(parents=True, exist_ok=True)
    data_dir = str(data_path)
    program_arguments: list[str] = [
        sys.executable,
        "-m",
        "companion.mac_notes_bridge",
        "run",
        "--url",
        str(args.url),
        "--bridge-id",
        str(args.bridge_id),
        "--data-dir",
        data_dir,
        "--account",
        str(args.account),
        "--folder",
        str(args.folder),
    ]
    if args.no_create_folder:
        program_arguments.append("--no-create-folder")
    payload = {
        "Label": _LAUNCHD_LABEL,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(_project_root()),
        "RunAtLoad": True,
        "KeepAlive": {"NetworkState": True, "SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(Path(data_dir) / "bridge.stdout.log"),
        "StandardErrorPath": str(Path(data_dir) / "bridge.stderr.log"),
    }
    return _install_launchd_payload(_LAUNCHD_LABEL, payload)


def _uninstall_launchd() -> None:
    _uninstall_launchd_service(_LAUNCHD_LABEL)


def _install_ssh_tunnel(args: argparse.Namespace) -> Path:
    target = str(args.ssh_target).strip()
    if _SSH_TARGET_RE.fullmatch(target) is None or target.startswith("-"):
        raise ValueError("ssh-target 必须是安全的 user@host")
    identity = Path(args.identity_file).expanduser().resolve(strict=True)
    identity_stat = identity.stat()
    if not identity.is_file() or identity_stat.st_uid != os.getuid():
        raise ValueError("SSH 私钥必须是当前用户拥有的普通文件")
    if identity_stat.st_mode & 0o077:
        raise ValueError("SSH 私钥权限必须不宽于 0600")
    local_port = int(args.local_port)
    remote_port = int(args.remote_port)
    if not 1 <= local_port <= 65_535 or not 1 <= remote_port <= 65_535:
        raise ValueError("SSH 隧道端口必须在 1..65535")

    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    program_arguments = _ssh_tunnel_program_arguments(
        target=target,
        identity=identity,
        local_port=local_port,
        remote_port=remote_port,
    )
    payload = {
        "Label": _SSH_TUNNEL_LABEL,
        "ProgramArguments": program_arguments,
        "RunAtLoad": True,
        "KeepAlive": {"NetworkState": True, "SuccessfulExit": False},
        "ProcessType": "Background",
        "StandardOutPath": str(data_dir / "ssh-tunnel.stdout.log"),
        "StandardErrorPath": str(data_dir / "ssh-tunnel.stderr.log"),
    }
    return _install_launchd_payload(_SSH_TUNNEL_LABEL, payload)


def _ssh_tunnel_program_arguments(
    *,
    target: str,
    identity: Path,
    local_port: int,
    remote_port: int,
) -> list[str]:
    return [
        "/usr/bin/ssh",
        "-N",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "StrictHostKeyChecking=yes",
        "-i",
        str(identity),
        "-L",
        f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
        target,
    ]


def _install_launchd_payload(label: str, payload: dict[str, object]) -> Path:
    agents_dir = Path.home() / "Library" / "LaunchAgents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    plist_path = agents_dir / f"{label}.plist"
    temporary = plist_path.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(payload, sort_keys=True))
    os.replace(temporary, plist_path)
    domain = f"gui/{os.getuid()}"
    _ = subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    _ = subprocess.run(
        ["/bin/launchctl", "bootstrap", domain, str(plist_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return plist_path


def _uninstall_launchd_service(label: str) -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    domain = f"gui/{os.getuid()}"
    _ = subprocess.run(
        ["/bin/launchctl", "bootout", domain, str(plist_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    plist_path.unlink(missing_ok=True)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _add_runtime_options(parser: argparse.ArgumentParser, *, include_url: bool) -> None:
    if include_url:
        parser.add_argument("--url", required=True)
    parser.add_argument("--bridge-id", default="mac-primary")
    parser.add_argument(
        "--data-dir",
        default="~/Library/Application Support/Akashic/NotesBridge",
    )
    parser.add_argument("--account", default="default")
    parser.add_argument("--folder", default="Akashic")
    parser.add_argument("--no-create-folder", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="akashic-mac-notes-bridge")
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser("pair", help="生成/保存共享 token 到 Mac Keychain")
    pair.add_argument("--bridge-id", default="mac-primary")
    pair.add_argument("--token", default="")
    revoke = commands.add_parser("revoke", help="撤销 Mac Keychain token")
    revoke.add_argument("--bridge-id", default="mac-primary")
    status = commands.add_parser("status", help="查看本机进程与心跳快照")
    status.add_argument(
        "--data-dir",
        default="~/Library/Application Support/Akashic/NotesBridge",
    )
    run = commands.add_parser("run", help="前台运行 Bridge")
    _add_runtime_options(run, include_url=True)
    run.add_argument("--max-message-bytes", type=int, default=1024 * 1024)
    probe = commands.add_parser("probe", help="验证 Notes 与自动化权限")
    _add_runtime_options(probe, include_url=False)
    reconcile = commands.add_parser(
        "reconcile", help="按 operation marker 核对不确定写入"
    )
    _add_runtime_options(reconcile, include_url=False)
    reconcile.add_argument("--operation-id", required=True)
    install = commands.add_parser("install-launchd", help="安装并启动用户级常驻服务")
    _add_runtime_options(install, include_url=True)
    _ = commands.add_parser("uninstall-launchd", help="停止并删除用户级常驻服务")
    tunnel = commands.add_parser(
        "install-ssh-tunnel",
        help="通过常驻 SSH 本地转发安全访问云端 loopback Broker",
    )
    tunnel.add_argument("--ssh-target", required=True)
    tunnel.add_argument("--identity-file", default="~/.ssh/id_ed25519")
    tunnel.add_argument("--local-port", type=int, default=6330)
    tunnel.add_argument("--remote-port", type=int, default=6330)
    tunnel.add_argument(
        "--data-dir",
        default="~/Library/Application Support/Akashic/NotesBridge",
    )
    _ = commands.add_parser(
        "uninstall-ssh-tunnel", help="停止并删除 Notes Bridge SSH 隧道"
    )
    return parser


__all__ = ["main"]
